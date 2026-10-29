import asyncio,unittest
from contextvars import ContextVar
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent.stream_context import owned_stream

class StreamContext(unittest.IsolatedAsyncioTestCase):
    async def test_context_stays_in_one_worker_without_prefetch(self):
        marker=ContextVar('synthetic_stream_context',default='outside');pulled=[];closed=[]
        async def source():
            token=marker.set('inside')
            try:
                for i in range(3):pulled.append(i);self.assertEqual(marker.get(),'inside');yield i
            finally:marker.reset(token);closed.append(True)
        stream=owned_stream(source)
        try:
            self.assertEqual(await asyncio.create_task(anext(stream)),0)
            await asyncio.sleep(.01);self.assertEqual(pulled,[0]);self.assertEqual(marker.get(),'outside')
            self.assertEqual(await asyncio.create_task(anext(stream)),1)
        finally:await stream.aclose()
        self.assertEqual(closed,[True]);self.assertEqual(marker.get(),'outside')

    async def test_source_error_is_forwarded_and_cleanup_finishes(self):
        closed=[]
        async def source():
            try:yield 1;raise ValueError('synthetic stream failure')
            finally:closed.append(True)
        stream=owned_stream(source)
        self.assertEqual(await asyncio.create_task(anext(stream)),1)
        with self.assertRaisesRegex(ValueError,'synthetic stream failure'):
            await asyncio.create_task(anext(stream))
        self.assertEqual(closed,[True])

    async def test_cancel_waiting_chunk_joins_worker(self):
        entered=asyncio.Event();closed=asyncio.Event()
        async def source():
            try:entered.set();await asyncio.Future();yield 1
            finally:closed.set()
        stream=owned_stream(source);pending=asyncio.create_task(anext(stream))
        await entered.wait();pending.cancel();await asyncio.gather(pending,return_exceptions=True)
        self.assertTrue(closed.is_set())
        self.assertFalse([t for t in asyncio.all_tasks() if t.get_name()=='qqbot-native-stream-context' and not t.done()])

    async def test_final_value_releases_source_before_consumer_receives_it(self):
        closed=[]
        async def source():
            try:yield 'last';await asyncio.Future()
            finally:closed.append(True)
        stream=owned_stream(source,is_final=lambda value:value=='last')
        try:
            self.assertEqual(await asyncio.create_task(anext(stream)),'last');self.assertEqual(closed,[True])
            with self.assertRaises(StopAsyncIteration):await asyncio.create_task(anext(stream))
        finally:await stream.aclose()
