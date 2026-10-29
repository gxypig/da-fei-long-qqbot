"""Keep an async stream's contexts in one worker across consumer tasks.

The native Runner creates a fresh task for each chunk. This adapter adds no
model admission and prefetches nothing; actual wire attempts retain their lane.
"""
import asyncio

_END=object()


async def owned_stream(factory,*,is_final=None):
    requests=asyncio.Queue(maxsize=1);pending=None

    async def worker():
        iterator=None
        try:
            iterator=factory()
            while True:
                reply=await requests.get()
                try:item=await anext(iterator)
                except StopAsyncIteration:
                    if not reply.done():reply.set_result(_END)
                    return
                complete=bool(is_final and is_final(item))
                if complete:
                    await iterator.aclose();iterator=None
                if not reply.done():reply.set_result(item)
                if complete:return
        finally:
            if iterator is not None:await iterator.aclose()

    def finished(task):
        error=asyncio.CancelledError() if task.cancelled() else task.exception()
        if pending is not None and not pending.done():
            if error is not None:pending.set_exception(error)
            else:pending.set_result(_END)

    task=asyncio.create_task(worker(),name='qqbot-native-stream-context')
    task.add_done_callback(finished)
    try:
        while True:
            if task.done():
                if task.cancelled():raise asyncio.CancelledError()
                task.result();return
            pending=asyncio.get_running_loop().create_future()
            requests.put_nowait(pending)
            item=await pending
            if item is _END:return
            yield item
    finally:
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True)
