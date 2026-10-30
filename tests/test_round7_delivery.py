import asyncio,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from aiocqhttp import CQHttp
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger,DeliveryUnconfirmed
from data.plugins.astrbot_plugin_proactive_agent.external_delivery import deliver,qzone_receipt
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import install_outbound_guard,OutboundGuard,bind_request,request_scope
from data.plugins.astrbot_plugin_proactive_agent.send_checks import record,snapshot,own_style
from data.plugins.astrbot_plugin_proactive_agent.review_modes import configured
from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job

class DeliveryChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.ledger=DeliveryLedger(Path(self.temp.name)/'ledger.db');await self.ledger.start()
    async def asyncTearDown(self):
        await asyncio.gather(*self.ledger.receipt_tasks,return_exceptions=True);await self.ledger.store.close();self.temp.cleanup()

    async def test_qzone_off_still_records_exact_body_and_confirmed_receipt(self):
        bot=object.__new__(CQHttp);p=SimpleNamespace(_platform=lambda:SimpleNamespace(bot=bot),_cfg={}.get,
            review_context=lambda o:[],review_mode=lambda o:'chat',_delivery=self.ledger)
        reviewer=SimpleNamespace(review=AsyncMock(side_effect=AssertionError('off reviewed')))
        transport=AsyncMock(return_value={'tid':'confirmed-qzone'})
        with patch.object(CQHttp,'call_action',transport):
            restore=install_outbound_guard(p,reviewer)
            try:await bot.call_action('send_qzone_msg',content='今天歇会儿。')
            finally:restore()
        rows=self.ledger.store.select(origin='qzone',states={'confirmed'})
        self.assertEqual(len(rows),1);row=rows[0]
        self.assertEqual(row['text'],'今天歇会儿');self.assertEqual(row['send_check']['path'],'local_repair')
        self.assertEqual(row['send_check']['delivery_state'],'confirmed');reviewer.review.assert_not_awaited()

    async def test_qzone_unknown_is_not_success_and_late_receipt_reconciles(self):
        record('qzone','文字',configured({}),path='review_off')
        with self.assertRaises(DeliveryUnconfirmed):
            await deliver(self.ledger,'qzone','文字',[],AsyncMock(return_value={}),qzone_receipt)
        gate=asyncio.Event();entered=asyncio.Event()
        async def send():entered.set();await gate.wait();return {'tid':'late'}
        task=asyncio.create_task(deliver(self.ledger,'qzone','文字',[],send,qzone_receipt));await entered.wait();task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(len(self.ledger.store.select(origin='qzone',states={'unknown'})),2)
        gate.set();await asyncio.gather(*self.ledger.receipt_tasks)
        self.assertEqual(len(self.ledger.store.select(origin='qzone',states={'confirmed'})),1)

    async def test_physical_group_receipt_uses_pinned_logical_session(self):
        origin='qq:GroupMessage:sender_100';job=SimpleNamespace(origin=origin)
        token=current_job.set(job)
        try:
            bind_request(origin,['本任务上下文'],False,{'persona_version':'v1'},configured({}))
            self.assertEqual(request_scope('qq:GroupMessage:100')['character']['persona_version'],'v1')
            self.assertIsNone(request_scope('qq:GroupMessage:200'))
        finally:current_job.reset(token)
        record('qq:GroupMessage:100','文字',configured({}))
        self.assertEqual(snapshot(origin,'文字')['target'],origin)
        self.assertIsNone(snapshot('qq:GroupMessage:200','文字'))

    async def test_media_only_check_has_no_fake_body_review(self):
        p=SimpleNamespace(_cfg={}.get,review_context=lambda o:[],review_mode=lambda o:'chat')
        r=SimpleNamespace(review=AsyncMock())
        await OutboundGuard(p,r).prepare('send_private_msg',{'user_id':1,'message':[{'type':'image','data':{'file':'image'}}]})
        check=snapshot('aiocqhttp:FriendMessage:1','')
        self.assertEqual(check['path'],'attachment_only');self.assertFalse(check['final_body_reviewed'])

    def test_quote_bytes_survive_emoji_and_address_limits(self):
        quoted='“爸爸。🙂🙂🙂”'
        self.assertIn(quoted,own_style('他说'+quoted+'，我知道了。',{'emoji_allowed':False,'father_address_allowed':False}))

    def test_fresh_bili_round_does_not_reuse_previous_send_approval(self):
        from data.plugins.astrbot_plugin_proactive_agent.review_modes import scope
        record('bilibili:123','一样的正文',configured({}))
        with scope({}.get,fresh=True):
            self.assertIsNone(snapshot('bilibili:123','一样的正文'))
            record('bilibili:123','一样的正文',configured({}))
            self.assertIsNotNone(snapshot('bilibili:123','一样的正文'))
        with scope({}.get,fresh=True):self.assertIsNone(snapshot('bilibili:123','一样的正文'))
