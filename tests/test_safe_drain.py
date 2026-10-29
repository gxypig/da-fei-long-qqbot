import asyncio,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from test_maintenance import ROOT
from data.plugins.astrbot_plugin_proactive_agent.safe_drain import drain_project

class DrainTests(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        store=SimpleNamespace(pending={},closed=False,flush=AsyncMock())
        return SimpleNamespace(context=SimpleNamespace(),_delivery=SimpleNamespace(store=store,receipt_tasks=set()),
            _reply_queue=SimpleNamespace(drain=AsyncMock(),active={},inflight={}))
    async def test_success_requires_positive_persistence_results(self):
        p=self.plugin();p._learning=SimpleNamespace(_persistence=SimpleNamespace(flush=AsyncMock(return_value=True)))
        r=await drain_project(p,1);self.assertTrue(r['drained'],r);self.assertTrue(r['durable'])
    async def test_false_checkpoint_is_not_ignored(self):
        p=self.plugin();p._learning=SimpleNamespace(_persistence=SimpleNamespace(flush=AsyncMock(return_value=False)))
        r=await drain_project(p,.01);self.assertFalse(r['drained']);self.assertFalse(r['durable'])
    async def test_persistence_exception_aborts_stop(self):
        p=self.plugin();p._delivery.store.pending={'x':1};p._delivery.store.flush=AsyncMock(side_effect=OSError('disk full'))
        r=await drain_project(p,.01);self.assertFalse(r['drained']);self.assertEqual(r['pending_persistence'],1)
    async def test_late_receipt_must_complete_before_stop(self):
        p=self.plugin();gate=asyncio.Event();task=asyncio.create_task(gate.wait());p._delivery.receipt_tasks.add(task)
        try:
            r=await drain_project(p,.01);self.assertFalse(r['drained']);self.assertFalse(task.cancelled())
            gate.set();await task;r=await drain_project(p,1);self.assertTrue(r['drained'],r)
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
    async def test_suspended_reply_is_still_inflight(self):
        p=self.plugin();p._reply_queue.inflight['id']=object()
        r=await drain_project(p,.01);self.assertFalse(r['drained'])
    async def test_timeout_cannot_look_like_success(self):
        p=self.plugin();gate=asyncio.Event();p._learning=SimpleNamespace(_persistence=SimpleNamespace(flush=gate.wait))
        r=await drain_project(p,.01);self.assertFalse(r['drained']);self.assertTrue(r['errors'])

if __name__=='__main__':unittest.main()
