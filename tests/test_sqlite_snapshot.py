import asyncio
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.sqlite_snapshot import SQLiteSnapshot

class SnapshotShutdown(unittest.IsolatedAsyncioTestCase):
    async def test_close_waits_for_cancelled_coroutine_disk_worker_and_keeps_latest(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'derived.db';store=SQLiteSnapshot(path)
            with store.db:store.db.execute('CREATE TABLE example(value)');store.db.execute('INSERT INTO example VALUES(1)')
            original=store._write;entered=threading.Event();release=threading.Event()
            def slow(data):entered.set();release.wait(2);original(data)
            store._write=slow
            task=asyncio.create_task(store.flush())
            for _ in range(100):
                if entered.is_set():break
                await asyncio.sleep(.01)
            self.assertTrue(entered.is_set());task.cancel();await asyncio.gather(task,return_exceptions=True)
            with store.db:store.db.execute('INSERT INTO example VALUES(2)')
            closing=asyncio.create_task(store.close());await asyncio.sleep(.04)
            self.assertFalse(closing.done());release.set();await closing;store.db.close()
            db=sqlite3.connect(path)
            try:self.assertEqual(db.execute('SELECT value FROM example').fetchall(),[(1,),(2,)])
            finally:db.close()
            self.assertEqual([p.name for p in Path(d).iterdir()],['derived.db'])
