import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from sqlalchemy.ext.asyncio import create_async_engine
from astrbot.core.utils.shared_preferences import SharedPreferences
from data.plugins.astrbot_plugin_proactive_agent.db_pressure import DatabasePressure

class PoolObservation(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_readers_join_but_cancelled_waiter_does_not_cancel_owner(self):
        engine=create_async_engine('sqlite+aiosqlite:///:memory:');self.addAsyncCleanup(engine.dispose)
        db=SimpleNamespace(engine=engine);prefs=SimpleNamespace(db_helper=db)
        entered=asyncio.Event();release=asyncio.Event()
        async def read(*args):entered.set();await release.wait();return {'value':[1]}
        with patch.object(SharedPreferences,'get_async',side_effect=read) as original:
            observer=DatabasePressure(SimpleNamespace(context=SimpleNamespace(_db=db),_audit=lambda *_:None))
            restore=observer.install()
            try:
                first=asyncio.create_task(SharedPreferences.get_async(prefs,'s','id','k'))
                await entered.wait()
                second=asyncio.create_task(SharedPreferences.get_async(prefs,'s','id','k'))
                await asyncio.sleep(.01);first.cancel();await asyncio.gather(first,return_exceptions=True)
                release.set();result=await second
                self.assertEqual(result,{'value':[1]});original.assert_awaited_once()
                self.assertEqual(observer.snapshot()['read_joined'],1)
                async with engine.connect():self.assertEqual(len(observer.snapshot()['active']),1)
                self.assertEqual(observer.snapshot()['active'],[])
            finally:restore()

    async def test_uncached_defaults_remain_per_waiter(self):
        engine=create_async_engine('sqlite+aiosqlite:///:memory:');self.addAsyncCleanup(engine.dispose)
        db=SimpleNamespace(engine=engine);prefs=SimpleNamespace(db_helper=db)
        async def absent(prefs,scope,id,key,default):await asyncio.sleep(.04);return default
        with patch.object(SharedPreferences,'get_async',side_effect=absent):
            observer=DatabasePressure(SimpleNamespace(context=SimpleNamespace(_db=db),_audit=lambda *_:None));restore=observer.install()
            try:
                values=await asyncio.gather(SharedPreferences.get_async(prefs,'s','id','missing',1),SharedPreferences.get_async(prefs,'s','id','missing',2))
                self.assertEqual(values,[1,2])
            finally:restore()
