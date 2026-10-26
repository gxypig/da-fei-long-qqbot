"""Bounded diagnostic writer: failure cannot hold a chat or repeat an effect."""
import asyncio
from datetime import datetime
import json
import os
import time
import uuid
from pathlib import Path

class AuditWriter:
    def __init__(self,path):
        self.path=Path(path);self.queue=asyncio.Queue(maxsize=4000);self.task=None
        self.stats={'dropped':0,'failures':0,'last_error':None,'last_error_at':None,'recovered_at':None}
        self.closed=False
        self._io=None

    async def start(self):
        try:self.stats.update(json.loads(await asyncio.to_thread(self.path.with_suffix('.status.json').read_text,'utf-8')))
        except (OSError,ValueError):pass
        self.task=asyncio.create_task(self._worker(),name='conversation-audit-writer')

    def add(self,kind,fields):
        if self.closed:return
        record=dict(fields)
        if 'kind' in record:record['work_kind']=record.pop('kind')
        record.update(time=datetime.now().isoformat(timespec='seconds'),kind=kind)
        try:self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self.stats.update(dropped=self.stats['dropped']+1,last_error='audit queue full',last_error_at=time.time())

    def _write(self,batch):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.exists() and self.path.stat().st_size>4_000_000:os.replace(self.path,self.path.with_suffix('.previous.jsonl'))
        with self.path.open('a',encoding='utf-8') as f:f.write(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in batch))

    def _status(self):
        path=self.path.with_suffix('.status.json');temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            temp.write_text(json.dumps(self.stats,ensure_ascii=False),encoding='utf-8');os.replace(temp,path)
        finally:temp.unlink(missing_ok=True)

    async def _worker(self):
        while True:
            first=await self.queue.get();batch=[first]
            while not self.queue.empty() and len(batch)<100:batch.append(self.queue.get_nowait())
            try:
                await self._offload(self._write,batch)
                if self.stats['last_error']:
                    self.stats['recovered_at']=time.time();self.stats['latest_error']=self.stats['last_error'];self.stats['last_error']=None
            except Exception as exc:
                self.stats.update(failures=self.stats['failures']+1,dropped=self.stats['dropped']+len(batch),
                                  last_error=type(exc).__name__,last_error_at=time.time())
            finally:
                for _ in batch:self.queue.task_done()
            try:await self._offload(self._status)
            except OSError:pass

    async def _offload(self,function,*args):
        self._io=asyncio.create_task(asyncio.to_thread(function,*args))
        return await asyncio.shield(self._io)

    async def close(self):
        self.closed=True
        try:await asyncio.wait_for(self.queue.join(),5)
        except TimeoutError:self.stats['dropped']+=self.queue.qsize()
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        if self._io:
            try:await asyncio.wait_for(asyncio.shield(self._io),3)
            except TimeoutError:return
            except Exception:pass
        try:await asyncio.to_thread(self._status)
        except OSError:pass
