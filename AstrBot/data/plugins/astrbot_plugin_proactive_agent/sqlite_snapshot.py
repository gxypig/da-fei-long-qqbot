"""Event-loop in-memory SQLite with bounded, atomic off-loop checkpoints.

Used for derived learning work, never the delivery outbox or original history.
The connection stays on one thread. Only immutable serialized bytes cross into
the disk worker, so admin readers cannot race a worker using the same cursor.
"""
import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time
import uuid

class SQLiteSnapshot:
    def __init__(self,path):
        self.path=Path(path);self.db=sqlite3.connect(':memory:');self.saved=-1;self.task=None;self.closed=False
        self._io=None;self._lock=asyncio.Lock()
        self.stats={'failures':0,'last_error':None,'latest_error':None,'last_error_at':None,'recovered_at':None,'unsaved':False}
        if self.path.exists():
            try:
                with closing(sqlite3.connect(f'file:{self.path.as_posix()}?mode=ro',uri=True,timeout=.1)) as source:source.backup(self.db)
            except BaseException:self.db.close();raise

    def _write(self,data):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,self.path)
        finally:temporary.unlink(missing_ok=True)

    async def flush(self):
        async with self._lock:
            # A cancelled coroutine cannot stop a filesystem thread. Keep its
            # handle and join it before starting a newer atomic replacement.
            if self._io is not None:
                try:await asyncio.shield(self._io)
                except Exception:pass
            return await self._flush()

    async def _flush(self):
        if self.db.in_transaction:return False
        version=self.db.total_changes
        if version==self.saved:return True
        data=self.db.serialize()
        try:
            self._io=asyncio.create_task(asyncio.to_thread(self._write,data))
            await asyncio.shield(self._io)
        except Exception as exc:
            self.stats.update(failures=self.stats['failures']+1,last_error=type(exc).__name__,
                latest_error=type(exc).__name__,last_error_at=time.time(),unsaved=True)
            return False
        self.saved=version
        if self.stats['last_error']:self.stats.update(last_error=None,recovered_at=time.time())
        self.stats['unsaved']=self.db.total_changes!=version
        return True

    def start(self):
        if not self.task:self.task=asyncio.create_task(self._worker(),name='learning-db-checkpoint')

    async def _worker(self):
        while True:
            await asyncio.sleep(2)
            await self.flush()

    async def close(self):
        if self.closed:return
        # Wait for the one in-flight immutable checkpoint instead of cancelling
        # its to_thread write and racing a newer final checkpoint.
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        try:ok=await asyncio.wait_for(self.flush(),5)
        except TimeoutError:
            self.stats.update(last_error='checkpoint timeout',unsaved=True)
            raise RuntimeError('检查点尚未完成，停止中止以保留内存数据')
        if not ok or self.stats.get('unsaved'):raise RuntimeError('检查点保存失败，停止中止以保留内存数据')
        self.closed=True
