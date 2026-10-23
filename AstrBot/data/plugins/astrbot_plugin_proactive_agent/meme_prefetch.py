"""Overlap unchanged meme enrichment with session queueing and agent preparation."""
import asyncio


class MemePrefetch:
    def __init__(self, memory, limit=2):
        self.memory, self.limit = memory, limit
        self.tasks = set()
        self.stats = {"started": 0, "reused": 0, "changed": 0, "fallback": 0}

    def start(self, event, text, recent):
        if len(self.tasks) >= self.limit or not text.strip():
            return
        args = (text, tuple(recent), event.unified_msg_origin)
        task = asyncio.create_task(self.memory.enrich(*args), name="meme-prefetch")
        self.stats["started"] += 1
        self.tasks.add(task)
        event.set_extra("qqbot_meme_prefetch", (args, task))
        owner = asyncio.current_task()

        def owner_done(_):
            if not task.done():
                task.cancel()

        def done(completed):
            self.tasks.discard(completed)
            if owner:
                owner.remove_done_callback(owner_done)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        if owner:
            owner.add_done_callback(owner_done)

    async def get(self, event, text, recent):
        spec = event.get_extra("qqbot_meme_prefetch")
        event.set_extra("qqbot_meme_prefetch", None)
        if spec:
            args, task = spec
            if args == (text, tuple(recent), event.unified_msg_origin) and not task.cancelled():
                self.stats["reused"] += 1
                return await task
            self.stats["changed"] += 1
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.stats["fallback"] += 1
        return await self.memory.enrich(text, recent, event.unified_msg_origin)

    async def close(self):
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
