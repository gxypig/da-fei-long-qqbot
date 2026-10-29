"""Native SDK transport failures, no real model or QQ traffic."""
import asyncio,json,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import test_native_wire_admission as native
from test_reply_pipeline import plugin
from test_work_scheduler import Event
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer,ReviewUnavailable
from data.plugins.astrbot_plugin_proactive_agent.persona_authority import REVIEW_FUNCTIONAL


class ReviewTransportRecovery(unittest.IsolatedAsyncioTestCase):
    provider=native.NativeWireTests.provider
    answer=native.NativeWireTests.answer
    asyncTearDown=native.NativeWireTests.asyncTearDown

    async def asyncSetUp(self):
        await native.NativeWireTests.asyncSetUp(self)
        self.cfg['reply_hedge_after_seconds']=15
        self.payloads=[]

    def reviewer(self,handler):
        async def record(request):
            self.payloads.append(json.loads(request.content))
            return await handler(request)
        p=plugin();p.config['review_low_latency']=False;p._work=self.runtime
        p.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=self.provider(record)))
        return p,ReplyReviewer(p._generate_json,lambda *_:None)

    async def run_review(self,reviewer,*,used=False):
        event=Event()
        with self.runtime.reply_work(event):
            self.runtime._trace(event).hedge_used=used
            return await reviewer.review('奶茶店先笑了',origin='offline',character={'persona_status':'selected'})

    async def test_early_connection_failure_uses_one_identical_recovery(self):
        async def handler(request):
            if len(self.payloads)==1:raise httpx.ReadError('synthetic disconnect',request=request)
            return self.answer('{"pass":true,"natural":true,"chat_fit":true}')
        _,reviewer=self.reviewer(handler)
        self.assertEqual(await asyncio.wait_for(self.run_review(reviewer),5),'奶茶店先笑了')
        self.assertEqual(len(self.payloads),2);self.assertEqual(self.payloads[0],self.payloads[1])
        self.assertEqual(self.runtime.status()['hedge']['won'],1)
        self.assertFalse(self.runtime._hedge_tasks)
        self.assertEqual(self.runtime.scheduler.status()['running'],0)

    async def test_persistent_connection_failure_is_bounded_to_two_attempts(self):
        async def handler(request):raise httpx.ConnectError('synthetic unavailable',request=request)
        _,reviewer=self.reviewer(handler)
        with self.assertRaises(ReviewUnavailable):await asyncio.wait_for(self.run_review(reviewer),5)
        self.assertEqual(len(self.payloads),2)

    async def test_already_used_turn_budget_does_not_recover_again(self):
        async def handler(request):raise httpx.ConnectError('synthetic unavailable',request=request)
        _,reviewer=self.reviewer(handler)
        with self.assertRaises(ReviewUnavailable):await self.run_review(reviewer,used=True)
        self.assertEqual(len(self.payloads),1)

    async def test_authentication_error_does_not_start_recovery(self):
        async def handler(request):return httpx.Response(401,json={'error':{'message':'synthetic denied','type':'invalid_api_key'}})
        _,reviewer=self.reviewer(handler)
        with self.assertRaises(ReviewUnavailable):await self.run_review(reviewer)
        self.assertEqual(len(self.payloads),1)

    async def test_malformed_review_is_a_verdict_not_a_transport_failure(self):
        async def handler(request):return self.answer('not json')
        _,reviewer=self.reviewer(handler)
        with self.assertRaises(ReviewUnavailable):await self.run_review(reviewer)
        self.assertEqual(len(self.payloads),1)

    async def test_disable_during_failure_prevents_recovery(self):
        async def handler(request):
            self.cfg['reply_hedge_after_seconds']=0
            raise httpx.ReadError('synthetic disconnect',request=request)
        _,reviewer=self.reviewer(handler)
        with self.assertRaises(ReviewUnavailable):await self.run_review(reviewer)
        self.assertEqual(len(self.payloads),1)

    async def test_cancel_recovery_joins_transport_and_releases_admission(self):
        entered=asyncio.Event();closed=asyncio.Event()
        async def handler(request):
            if len(self.payloads)==1:raise httpx.ReadError('synthetic disconnect',request=request)
            entered.set()
            try:await asyncio.Future()
            finally:closed.set()
        _,reviewer=self.reviewer(handler)
        task=asyncio.create_task(self.run_review(reviewer))
        try:
            await asyncio.wait_for(entered.wait(),5);task.cancel()
            result=await asyncio.gather(task,return_exceptions=True)
            self.assertIsInstance(result[0],asyncio.CancelledError)
            self.assertTrue(closed.is_set());self.assertFalse(self.runtime._hedge_tasks)
            self.assertEqual(self.runtime.scheduler.status()['running'],0)
        finally:task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_original_json_deadline_also_covers_recovery(self):
        closed=asyncio.Event()
        async def handler(request):
            if len(self.payloads)==1:raise httpx.ReadError('synthetic disconnect',request=request)
            try:await asyncio.Future()
            finally:closed.set()
        p,_=self.reviewer(handler)
        with self.runtime.reply_work(Event()):
            with self.assertRaises(asyncio.TimeoutError):
                await p._generate_json(REVIEW_FUNCTIONAL,{'text':'合成输入'},'offline',timeout=.5)
        self.assertEqual(len(self.payloads),2);self.assertTrue(closed.is_set())
        self.assertFalse(self.runtime._hedge_tasks)
        self.assertEqual(self.runtime.scheduler.status()['running'],0)
