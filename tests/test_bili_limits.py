import asyncio
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_bili_scroll.work_limits import SharedCopy,SessionLock,total_budget,remaining
from data.plugins.astrbot_plugin_bili_scroll.client import BiliClient,BiliUnavailable
from data.plugins.astrbot_plugin_bili_scroll.receipts import install,observe
from data.plugins.astrbot_plugin_bili_scroll.actions import Ledger
from aiocqhttp import CQHttp

class Limits(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_after_confirmed_send_does_not_report_not_found(self):
        from data.plugins.astrbot_plugin_bili_scroll.main import Main as BiliMain
        plugin=object.__new__(BiliMain)
        async def sent(origin,keyword,event,check,mode,completion):
            completion['confirmed']=True
            raise TimeoutError('optional persistence exceeded deadline')
        plugin._share=sent
        result=await plugin.share('p:GroupMessage:1')
        self.assertIn('已向当前会话分享',result)
        async def unsent(*args):raise TimeoutError('search deadline')
        plugin._share=unsent
        with self.assertRaises(TimeoutError):await plugin.share('p:GroupMessage:1')

    async def test_shared_caption_single_generation_separate_waiters(self):
        copy=SharedCopy();started=asyncio.Event();release=asyncio.Event()
        async def factory():started.set();await release.wait();return 'public caption'
        make=AsyncMock(side_effect=factory)
        first=asyncio.create_task(copy.get('public-video',make));await started.wait()
        second=asyncio.create_task(copy.get('public-video',make));await asyncio.sleep(0)
        first.cancel();await asyncio.gather(first,return_exceptions=True);release.set()
        self.assertEqual(await second,'public caption');make.assert_awaited_once()
        self.assertEqual(await copy.get('public-video',make),'public caption');make.assert_awaited_once();await copy.close()

    async def test_all_caption_waiters_cancel_pure_generation(self):
        copies=SharedCopy();blocked=asyncio.Event()
        task=asyncio.create_task(copies.get('x',blocked.wait));await asyncio.sleep(0)
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertFalse(copies.flights);await copies.close()

    async def test_session_lock_keeps_waiters_visible_between_owners(self):
        lock=SessionLock();await lock.__aenter__()
        task=asyncio.create_task(lock.__aenter__());await asyncio.sleep(0)
        await lock.__aexit__();self.assertTrue(lock.locked())
        await task;await lock.__aexit__();self.assertFalse(lock.locked())

    async def test_failure_cache_expires_quickly_normal_empty_does_not(self):
        client=BiliClient();key=('normal',client._read_identity(),'x')
        build=AsyncMock(return_value=(time.time(),[],[],{'read_state':'failed'}))
        await client._cached_scan(key,build)
        with patch('data.plugins.astrbot_plugin_bili_scroll.client.time.time',return_value=time.time()+9):await client._cached_scan(key,build)
        self.assertEqual(build.await_count,2)
        client.cache[key]=(time.time(),[],[],{'read_state':'empty'});build.reset_mock()
        with patch('data.plugins.astrbot_plugin_bili_scroll.client.time.time',return_value=time.time()+9):await client._cached_scan(key,build)
        build.assert_not_awaited();await client.close()

    async def test_total_budget_includes_all_stages(self):
        with self.assertRaises(TimeoutError):
            async with total_budget(.03):
                await asyncio.sleep(.02)
                self.assertLess(remaining(10),.02)
                await asyncio.sleep(.02)

    async def test_receipt_observer_rejects_empty_and_ignores_other_target(self):
        previous=AsyncMock(side_effect=[{'message_id':0},{'message_id':18},{'message_id':19}])
        plugin=SimpleNamespace(values=lambda:{'platform_id':'p'})
        with patch.object(CQHttp,'call_action',previous):
            restore=install(plugin)
            try:
                with observe('p:FriendMessage:1') as rows:
                    await CQHttp.call_action(None,'send_private_msg',user_id=1,message='x')
                    await CQHttp.call_action(None,'send_private_msg',user_id=2,message='x')
                    await CQHttp.call_action(None,'send_private_msg',user_id=1,message='x')
                self.assertEqual([r['state'] for r in rows],['unknown','confirmed'])
                self.assertEqual(rows[-1]['message_id'],'19')
            finally:restore()

    def test_checked_decision_survives_restart_but_changed_content_is_new(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'actions.db';ledger=Ledger(path)
            self.assertFalse(ledger.checked('1','2','hello'))
            ledger.checked('1','2','hello',True)
            again=Ledger(path)
            self.assertTrue(again.checked('1','2','hello'));self.assertFalse(again.checked('1','2','changed'))
            self.assertEqual(again.rows('1'),[])
