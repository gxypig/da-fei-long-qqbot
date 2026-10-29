import asyncio
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.record_store import AsyncRecordStore
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger,install_delivery_transport,DeliveryUnconfirmed
from data.plugins.astrbot_plugin_proactive_agent.session_fence import get_fence
from aiocqhttp import CQHttp


class ProviderInitialization(unittest.IsolatedAsyncioTestCase):
    async def test_missing_provider_is_reschedulable_and_runtime_error_compatible(self):
        from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import ModelNotReady
        plugin=SimpleNamespace(_cfg={}.get,context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=None)))
        with self.assertRaises(ModelNotReady) as caught:
            await Main._generate_json(plugin,'background analysis',{},'qq:GroupMessage:1')
        self.assertIsInstance(caught.exception,RuntimeError)


class PersistenceRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=AsyncRecordStore(Path(self.temp.name)/'rows.db',limit=2)
        await self.store.start();self.addAsyncCleanup(self.store.close)

    async def test_full_queue_recovers_before_recording_statistics(self):
        original=self.store._write;attempts=[]
        def flaky(batch):
            attempts.append(1)
            if len(attempts)==1:raise OSError('temporary')
            original(batch)
        with patch.object(self.store,'_write',flaky):
            self.store.put('a',{'value':1});self.store.put('b',{'value':2})
            await self.store.flush('b')
        self.assertFalse(self.store.task.done());self.assertIsNotNone(self.store.stats['recovered_at'])
        self.assertEqual(self.store.persisted['a'],1)

    async def test_telemetry_failure_cannot_kill_writer(self):
        original=self.store.put
        def put(key,body):
            if key=='__persistence_stats__':raise RuntimeError('injected statistics fault')
            return original(key,body)
        self.store.stats['last_error']='old failure'
        with patch.object(self.store,'put',put):
            self.store.put('a',{'value':1});await self.store.flush('a')
            self.store.put('b',{'value':2});await self.store.flush('b')
            self.assertFalse(self.store.task.done())

    async def test_dead_worker_flush_fails_promptly_and_close_spools(self):
        self.store.task.cancel();await asyncio.gather(self.store.task,return_exceptions=True)
        self.store.put('a',{'value':1});start=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'worker unavailable'):await self.store.flush('a')
        self.assertLess(time.monotonic()-start,.2)
        await self.store.close();self.assertTrue(self.store.path.with_suffix('.pending.json').exists())


class TransportBoundary(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.ledger=DeliveryLedger(Path(self.temp.name)/'ledger.db');await self.ledger.start()
        self.addAsyncCleanup(self.ledger.close)
        self.cid='old'
        async def current(origin):return self.cid
        async def reset(origin,cid):self.cid=cid
        self.manager=SimpleNamespace(get_curr_conversation_id=current,switch_conversation=reset)
        self.bot=object()
        self.plugin=SimpleNamespace(_delivery=self.ledger,_platform=lambda:SimpleNamespace(bot=self.bot),
            _cfg={'platform_id':'p'}.get,context=SimpleNamespace(conversation_manager=self.manager))

    async def test_receipt_updates_when_new_key_capacity_fills_during_transport(self):
        async def transport(*args,**kwargs):
            self.ledger.store.limit=len(self.ledger.store.pending)+1
            self.ledger.store.put('unrelated-capacity-filler',{'at':time.time()})
            return {'message_id':7}
        with patch.object(CQHttp,'call_action',transport):
            restore=install_delivery_transport(self.plugin)
            try:self.assertEqual(await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='accepted'),{'message_id':7})
            finally:restore()
        self.assertEqual(self.ledger.health()['counts']['confirmed'],1)
        self.assertEqual(self.ledger.health()['targets'][0]['last_state'],'confirmed')

    async def test_reset_during_durable_wait_cancels_old_reply(self):
        original=self.ledger.store.flush
        async def flush(key,timeout=3):
            await original(key,timeout);await self.manager.switch_conversation('p:FriendMessage:1','new')
        transport=AsyncMock(return_value={'message_id':1})
        with patch.object(CQHttp,'call_action',transport),patch.object(self.ledger.store,'flush',flush):
            restore=install_delivery_transport(self.plugin)
            try:
                with self.assertRaises(DeliveryUnconfirmed):await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='old draft')
            finally:restore()
        self.assertEqual(transport.await_count,0)
        self.assertEqual(next(r for r in self.ledger.store.rows.values() if r.get('state'))['state'],'failed')

    async def test_reset_waits_for_committed_transport(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def transport(*a,**k):entered.set();await release.wait();return {'message_id':1}
        with patch.object(CQHttp,'call_action',transport):
            restore=install_delivery_transport(self.plugin)
            try:
                sending=asyncio.create_task(CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='valid'))
                await entered.wait()
                reset=asyncio.create_task(self.manager.switch_conversation('p:FriendMessage:1','new'))
                await asyncio.sleep(.01);self.assertFalse(reset.done())
                release.set();await sending;await reset
            finally:release.set();restore()

    async def test_interleaved_unload_is_transparent_after_store_closed(self):
        transport=AsyncMock(return_value={'message_id':1})
        with patch.object(CQHttp,'call_action',transport):
            restore=install_delivery_transport(self.plugin);inner=CQHttp.call_action
            async def other(bot,action,**params):return await inner(bot,action,**params)
            CQHttp.call_action=other;restore();await self.ledger.close()
            await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='unrelated plugin')
        self.assertEqual(transport.await_count,1)

    async def test_repeated_install_records_once(self):
        transport=AsyncMock(return_value={'message_id':1})
        with patch.object(CQHttp,'call_action',transport):
            first=install_delivery_transport(self.plugin);second=install_delivery_transport(self.plugin)
            try:await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='once')
            finally:first();second()
        self.assertEqual(self.ledger.stats['confirmed'],1)

    async def test_clear_same_conversation_history_invalidates_old_epoch(self):
        self.manager.update_conversation=AsyncMock()
        original=self.ledger.store.flush
        async def flush(key,timeout=3):
            await original(key,timeout);await self.manager.update_conversation('p:FriendMessage:1','old',history=[])
        transport=AsyncMock(return_value={'message_id':1})
        with patch.object(CQHttp,'call_action',transport),patch.object(self.ledger.store,'flush',flush):
            restore=install_delivery_transport(self.plugin)
            try:
                with self.assertRaises(DeliveryUnconfirmed):await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='stale')
            finally:restore()
        transport.assert_not_awaited()

    async def test_idle_check_runs_after_outbox_flush_before_transport(self):
        from data.plugins.astrbot_plugin_proactive_agent.conversation_review import delivery_policy
        from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import BackgroundPaused
        busy=False;original=self.ledger.store.flush
        async def flush(key,timeout=3):
            nonlocal busy
            await original(key,timeout);busy=True
        async def check():
            if busy:raise BackgroundPaused('new foreground')
        transport=AsyncMock(return_value={'message_id':1})
        with patch.object(CQHttp,'call_action',transport),patch.object(self.ledger.store,'flush',flush),delivery_policy(check):
            restore=install_delivery_transport(self.plugin)
            try:
                with self.assertRaises(BackgroundPaused):await CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='idle')
            finally:restore()
        transport.assert_not_awaited()

    async def test_inflight_unload_requires_receipt_before_closing_store(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def transport(*a,**k):entered.set();await release.wait();return {'message_id':1}
        with patch.object(CQHttp,'call_action',transport):
            restore=install_delivery_transport(self.plugin)
            sending=asyncio.create_task(CQHttp.call_action(self.bot,'send_private_msg',user_id=1,message='already submitted'))
            await entered.wait();restore()
            with self.assertRaises(RuntimeError):await self.ledger.close(timeout=.01)
            self.assertFalse(self.ledger.store.closed)
            release.set()
            self.assertEqual(await sending,{'message_id':1})
            await self.ledger.close()
            self.assertTrue(self.ledger.store.closed)
