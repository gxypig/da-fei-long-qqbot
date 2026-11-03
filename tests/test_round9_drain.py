"""A safe stop must include late links for evicted review-history records."""
import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'AstrBot/data/plugins/astrbot_plugin_proactive_agent/safe_drain.py'
spec=importlib.util.spec_from_file_location('round9_drain_under_test',SOURCE)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class LateReviewDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tasks=[];self.saved={}
        store=SimpleNamespace(pending={},closed=False)
        async def flush(key):self.saved[key]=store.pending.pop(key)
        store.flush=flush
        self.history=SimpleNamespace(store=store,cold={})
        async def drain(timeout):pass
        self.plugin=SimpleNamespace(context=SimpleNamespace(),_reply_queue=SimpleNamespace(drain=drain,active={},inflight={}),_review_history=self.history)

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)

    def receipt(self,key,gate,then=None):
        async def work():
            await gate.wait()
            self.history.store.pending[key]='confirmed'
            self.history.cold.pop(key,None)
            if then:then()
        task=asyncio.create_task(work());self.tasks.append(task)
        self.history.cold[key]={'task':task};return task

    async def test_late_receipt_finishes_before_durability_is_reported(self):
        gate=asyncio.Event();self.receipt('old-review',gate)
        drain=asyncio.create_task(module.drain_project(self.plugin,timeout=.5));self.tasks.append(drain)
        await asyncio.sleep(.01)
        self.assertFalse(drain.done(),'must not claim durable while the cold receipt is still loading')
        gate.set();result=await drain
        self.assertTrue(result['durable']);self.assertEqual(self.saved,{'old-review':'confirmed'})
        self.assertEqual(result['pending_history_links'],0)

    async def test_timeout_blocks_stop_without_cancelling_receipt_then_retry_succeeds(self):
        gate=asyncio.Event();receipt=self.receipt('old-review',gate)
        result=await module.drain_project(self.plugin,timeout=.01)
        self.assertFalse(result['drained']);self.assertEqual(result['pending_history_links'],1)
        self.assertFalse(receipt.done());self.assertTrue(result['errors'])
        gate.set();await receipt
        result=await module.drain_project(self.plugin,timeout=.1)
        self.assertTrue(result['durable']);self.assertEqual(self.saved,{'old-review':'confirmed'})

    async def test_link_created_while_waiting_is_also_included(self):
        first=asyncio.Event();second=asyncio.Event()
        self.receipt('first',first,lambda:self.receipt('second',second))
        drain=asyncio.create_task(module.drain_project(self.plugin,timeout=.5));self.tasks.append(drain)
        first.set();await asyncio.sleep(.01)
        self.assertFalse(drain.done())
        second.set();result=await drain
        self.assertTrue(result['durable']);self.assertEqual(self.saved,{'first':'confirmed','second':'confirmed'})

if __name__=='__main__':unittest.main()
