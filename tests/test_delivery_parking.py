import asyncio
from unittest.mock import patch
import test_round6_lifecycle as lifecycle
from test_fullchain_foundation import event
from aiocqhttp import CQHttp

class DeliveryParking(lifecycle.FirstSessionAndReceipt):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.q.cfg={'reply_queue_concurrency':1,'reply_inflight_limit':3}.get
        self.plugin._reply_queue=self.q

    async def test_flush_and_receipt_release_execution_but_keep_same_recipient(self):
        from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import install_delivery_transport
        self.job.owner=asyncio.current_task()
        same=self.q.create(event(origin=self.job.origin,id_='same'));self.q.submit(same)
        original=self.ledger.store.flush;observed=[]
        async def flush(*args,**kwargs):
            # First flush runs in owner; intent flush runs in receipt child.
            observed.append(self.job.state)
            self.assertEqual(self.job.state,'suspended')
            self.assertIn(self.job.id,self.q.inflight)
            self.assertEqual(same.state,'pending')
            return await original(*args,**kwargs)
        async def send(*args,**kwargs):
            self.assertEqual(self.job.state,'suspended')
            other=self.q.create(event(origin='p:GroupMessage:2',id_='other'));self.q.submit(other)
            self.assertEqual(other.state,'running');self.assertEqual(same.state,'pending')
            self.q.finish(other,'confirmed')
            return {'message_id':999}
        with patch.object(CQHttp,'call_action',send),patch.object(self.ledger.store,'flush',flush):
            restore=install_delivery_transport(self.plugin)
            try:await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='正常回复')
            finally:restore()
        self.assertGreaterEqual(len(observed),2);self.assertEqual(self.job.state,'running')
        self.q.finish(self.job,'confirmed');await self.q.wait(same);self.q.finish(same,'confirmed')

    async def test_cancel_parked_submitted_send_preserves_late_receipt(self):
        from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import install_delivery_transport
        entered=asyncio.Event();release=asyncio.Event()
        async def send(*args,**kwargs):entered.set();await release.wait();return {'message_id':990}
        async def owner():
            self.job.owner=asyncio.current_task()
            await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='已提交')
        with patch.object(CQHttp,'call_action',send):
            restore=install_delivery_transport(self.plugin)
            try:
                task=asyncio.create_task(owner());await entered.wait()
                self.assertEqual(self.job.state,'suspended');task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                self.assertEqual(self.job.state,'cancelled');self.assertEqual(len(self.ledger.receipt_tasks),1)
                release.set();await asyncio.gather(*list(self.ledger.receipt_tasks))
            finally:release.set();restore()
        records=[r for r in self.ledger.store.rows.values() if r.get('text')=='已提交']
        self.assertEqual(len(records),1);self.assertEqual(records[0]['state'],'confirmed')
        self.assertFalse(self.ledger.receipt_tasks)

    async def test_cancel_durable_wait_never_submits(self):
        from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import install_delivery_transport
        from unittest.mock import AsyncMock
        entered=asyncio.Event();never=asyncio.Event();send=AsyncMock(return_value={'message_id':99})
        async def flush(*args,**kwargs):entered.set();await never.wait()
        async def owner():
            self.job.owner=asyncio.current_task()
            await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='尚未提交')
        with patch.object(CQHttp,'call_action',send),patch.object(self.ledger.store,'flush',flush):
            restore=install_delivery_transport(self.plugin)
            try:
                task=asyncio.create_task(owner());await entered.wait()
                self.assertEqual(self.job.state,'suspended');task.cancel();await asyncio.gather(task,return_exceptions=True)
            finally:restore()
        send.assert_not_awaited()
        record=next(r for r in self.ledger.store.rows.values() if r.get('text')=='尚未提交')
        self.assertEqual(record['delivery_result'],'not_sent')
