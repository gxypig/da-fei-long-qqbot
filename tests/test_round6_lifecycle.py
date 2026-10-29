import asyncio,copy,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from test_fullchain_foundation import event
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue,current_job
from data.plugins.astrbot_plugin_proactive_agent.session_fence import get_fence
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger,install_delivery_transport,DeliveryUnconfirmed,delivery_purpose
from aiocqhttp import CQHttp


class QueueLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_physical_group_serializes_distinct_internal_sessions(self):
        q=ReplyQueue({'reply_queue_concurrency':4}.get)
        jobs=[q.create(event(origin=o,id_=str(i))) for i,o in enumerate(['p:GroupMessage:a_1','p:GroupMessage:b_1','p:GroupMessage:2'])]
        for job in jobs:q.submit(job)
        self.assertEqual([j.state for j in jobs],['running','pending','running'])
        q.finish(jobs[0],'confirmed');await q.wait(jobs[1]);q.close()

    async def test_wait_releases_execution_but_not_physical_conversation(self):
        q=ReplyQueue({'reply_queue_concurrency':1}.get)
        a=q.create(event());same=q.create(event(id_='2'));other=q.create(event(origin='p:GroupMessage:2'))
        for j in [a,same,other]:q.submit(j)
        async with q.parked(a,'工具读取'):
            self.assertEqual(other.state,'running');self.assertEqual(same.state,'pending')
            self.assertEqual(a.state,'suspended');self.assertEqual(len(q.active),1)
            q.finish(other,'confirmed')
        self.assertEqual(a.state,'running');q.finish(a,'confirmed');await q.wait(same);q.close()

    async def test_terminal_ingress_gets_new_execution(self):
        class Event:
            unified_msg_origin='p:FriendMessage:1'
            message_obj=SimpleNamespace(message_id='1')
            def __init__(self):self._extras={}
            def get_extra(self,k):return self._extras.get(k)
            def set_extra(self,k,v):self._extras[k]=v
            def get_sender_id(self):return '1'
            def get_message_str(self):return '续接内容'
        q=ReplyQueue({}.get);original=Event();old=q.create(original);q.finish(old,'ignored')
        copied=copy.copy(original);new=q.create(copied)
        self.assertNotEqual(new.id,old.id);self.assertIs(original.get_extra('qqbot_reply_job'),old)
        q.submit(new);self.assertEqual(new.state,'running');q.close()

    async def test_suspended_jobs_keep_inflight_limit(self):
        q=ReplyQueue({'reply_queue_concurrency':1,'reply_inflight_limit':1}.get)
        a=q.create(event());b=q.create(event(origin='p:GroupMessage:2'))
        q.submit(a);q.submit(b)
        async with q.parked(a,'wait'):
            self.assertEqual(b.state,'pending')
        q.finish(a,'confirmed');await q.wait(b);q.close()


class FirstSessionAndReceipt(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ledger=DeliveryLedger(Path(self.temp.name)/'rows.db');await self.ledger.start()
        self.cid=None
        async def current(origin):return self.cid
        async def create(origin,*args,**kwargs):self.cid='new';return self.cid
        self.manager=SimpleNamespace(get_curr_conversation_id=current,new_conversation=create)
        self.bot=object();self.plugin=SimpleNamespace(_delivery=self.ledger,_cfg={'platform_id':'p'}.get,
            _platform=lambda:SimpleNamespace(bot=self.bot),context=SimpleNamespace(conversation_manager=self.manager))
        self.q=ReplyQueue({}.get);self.job=self.q.create(event(origin='p:FriendMessage:1'));self.q.submit(self.job)
        self.job.session_epoch=0
        self.token=current_job.set(self.job);self.addCleanup(current_job.reset,self.token)

    async def asyncTearDown(self):
        self.q.close()
        await self.ledger.close()
        self.assertFalse(self.ledger.receipt_tasks)
        self.assertTrue(self.ledger.store.closed)
        self.assertFalse(self.ledger.store.pending)
        self.assertTrue(self.ledger.store.task.done())
        self.assertTrue(self.ledger.store._io is None or self.ledger.store._io.done())

    async def test_own_first_session_binds_new_epoch_and_sends(self):
        self.job.owner=asyncio.current_task()
        send=AsyncMock(return_value={'message_id':1})
        with patch.object(CQHttp,'call_action',send):
            restore=install_delivery_transport(self.plugin)
            try:
                await self.manager.new_conversation(self.job.origin)
                self.assertEqual(self.job.cid,'new');self.assertEqual(self.job.session_epoch,1)
                await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='首次回复')
            finally:restore()
        self.assertEqual(send.await_count,1);self.assertEqual(self.job.delivery_result,'confirmed')

    async def test_external_task_creation_does_not_adopt_waiting_reply(self):
        self.job.owner=asyncio.current_task()
        send=AsyncMock(return_value={'message_id':1})
        with patch.object(CQHttp,'call_action',send):
            restore=install_delivery_transport(self.plugin)
            try:
                await asyncio.create_task(self.manager.new_conversation(self.job.origin))
                self.assertIsNone(self.job.cid)
                with self.assertRaises(DeliveryUnconfirmed):await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='old')
            finally:restore()
        send.assert_not_awaited();self.assertEqual(self.job.delivery_result,'not_sent')

    async def test_error_notification_receipt_is_not_normal_answer_success(self):
        with patch.object(CQHttp,'call_action',AsyncMock(return_value={'message_id':1})):
            restore=install_delivery_transport(self.plugin)
            try:
                with delivery_purpose('error_notification'):
                    await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='接口暂时不可用')
            finally:restore()
        self.assertEqual(self.job.delivery_result,'confirmed');self.assertEqual(self.job.execution_result,'failed')
        self.assertEqual(self.ledger.state(self.job),'failed')

    async def test_unused_prepared_record_is_not_unknown_after_restart(self):
        key=self.ledger.begin('p:FriendMessage:1',None,'未提交',[],prepared=True)
        await self.ledger.store.flush(key);await self.ledger.close()
        other=DeliveryLedger(self.ledger.store.path);await other.start()
        try:self.assertEqual(other.store.rows[key]['delivery_result'],'not_sent')
        finally:await other.close()

    async def test_cancel_after_submission_keeps_receipt_observer(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def send(*args,**kwargs):entered.set();await release.wait();return {'message_id':77}
        with patch.object(CQHttp,'call_action',send):
            restore=install_delivery_transport(self.plugin)
            try:
                task=asyncio.create_task(CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='已提交'))
                await entered.wait();task.cancel();await asyncio.gather(task,return_exceptions=True)
                row=self.ledger.store.rows[self.job.sends[-1]]
                self.assertEqual(row['state'],'unknown');self.assertTrue(self.ledger.receipt_tasks)
                release.set();await asyncio.gather(*self.ledger.receipt_tasks)
                row=self.ledger.store.rows[self.job.sends[-1]]
                self.assertEqual(row['state'],'confirmed');self.assertEqual(row['message_id'],'77')
                self.assertEqual(self.ledger.stats['unknown'],0)
            finally:release.set();restore()

    async def test_cancel_during_first_persistence_is_not_sent(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def flush(*args,**kwargs):entered.set();await release.wait()
        send=AsyncMock(return_value={'message_id':9})
        with patch.object(CQHttp,'call_action',send),patch.object(self.ledger.store,'flush',flush):
            restore=install_delivery_transport(self.plugin)
            try:
                task=asyncio.create_task(CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='未提交'))
                await entered.wait();task.cancel();await asyncio.gather(task,return_exceptions=True)
                row=self.ledger.store.rows[self.job.sends[-1]]
                self.assertEqual(row['delivery_result'],'not_sent');send.assert_not_awaited()
            finally:release.set();restore()
