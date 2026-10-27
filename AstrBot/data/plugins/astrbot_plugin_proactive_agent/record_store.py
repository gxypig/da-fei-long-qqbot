"""Single bounded asynchronous persistence lane with retryable memory-first rows."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import closing


class AsyncRecordStore:
    def __init__(self,path,limit=5000):
        self.path=Path(path);self.limit=limit;self.rows={};self.pending={};self.persisted={}
        self.wake=asyncio.Event();self.progress=asyncio.Event();self.task=None;self.closed=False
        self._io=None
        self._stats_dirty=False
        self.stats={'failures':0,'dropped':0,'last_error':None,'last_error_at':None,'recovered_at':None}

    def _load(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path,timeout=1)) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)')
            return {k:(rev,json.loads(body)) for k,rev,body in db.execute('SELECT id,revision,body FROM records')}

    def _write(self,rows):
        with closing(sqlite3.connect(self.path,timeout=1)) as db, db:
            db.executemany('INSERT INTO records VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,body=excluded.body WHERE excluded.revision>records.revision',
                [(k,rev,json.dumps(body,ensure_ascii=False)) for k,(rev,body) in rows.items()])

    async def start(self):
        loaded=await asyncio.to_thread(self._load)
        self.rows={k:body for k,(rev,body) in loaded.items()};self.persisted={k:rev for k,(rev,body) in loaded.items()}
        self.stats.update(self.rows.get('__persistence_stats__',{}).get('stats',{}))
        try:
            spool=json.loads(await asyncio.to_thread(self.path.with_suffix('.pending.json').read_text,'utf-8'))
            for k,body in spool.items():
                if body.get('revision',0)>self.persisted.get(k,0):
                    self.rows[k]=body;self.pending[k]=(body['revision'],body)
        except FileNotFoundError:pass
        except (ValueError,TypeError,AttributeError) as exc:
            self.stats.update(failures=self.stats['failures']+1,last_error='invalid_recovery_spool',last_error_at=time.time())
        self.task=asyncio.create_task(self._worker(),name='persist-'+self.path.stem)
        self.task.add_done_callback(lambda _: self.progress.set())
        if self.pending:self.wake.set()

    def put(self,key,body):
        if self.closed:raise RuntimeError('persistence closed')
        if key not in self.pending and key not in self.rows and len(self.pending)>=self.limit:
            self.stats['dropped']+=1;self.stats['last_error']='pending capacity exceeded'
            raise RuntimeError('persistence queue full')
        revision=self.rows.get(key,{}).get('revision',0)+1
        body={**body,'revision':revision}
        self.rows[key]=body;self.pending[key]=(revision,body);self.wake.set()
        return body

    async def flush(self,key,timeout=3):
        revision=self.rows[key]['revision']
        async with asyncio.timeout(timeout):
            while self.persisted.get(key,0)<revision:
                self.progress.clear()
                if self.persisted.get(key,0)>=revision:break
                self._check_worker()
                await self.progress.wait()

    def _check_worker(self):
        if self.closed or not self.task or self.task.done():
            cause=None
            if self.task and self.task.done() and not self.task.cancelled():
                cause=self.task.exception()
            raise RuntimeError('persistence worker unavailable; pending records retained') from cause

    def _queue_stats(self):
        # Telemetry is best effort. It must never stop delivery facts being committed.
        if not self._stats_dirty or self.closed:return
        if '__persistence_stats__' not in self.pending and len(self.pending)>=self.limit:return
        try:
            self.put('__persistence_stats__',{'stats':dict(self.stats)})
            self._stats_dirty=False
        except Exception:
            self._stats_dirty=True

    async def _worker(self):
        delay=.1
        while True:
            await self.wake.wait();self.wake.clear()
            while self.pending:
                batch=dict(list(self.pending.items())[:100])
                try:
                    self._io=asyncio.create_task(asyncio.to_thread(self._write,batch))
                    await asyncio.shield(self._io)
                except Exception as exc:
                    self.stats.update(failures=self.stats['failures']+1,last_error=type(exc).__name__,last_error_at=time.time())
                    self._stats_dirty=True
                    await asyncio.sleep(delay);delay=min(5,delay*2)
                    continue
                for key,(revision,_) in batch.items():
                    self.persisted[key]=revision
                    if self.pending.get(key,(None,))[0]==revision:self.pending.pop(key,None)
                self.progress.set();delay=.1
                if self.stats['last_error']:
                    self.stats['recovered_at']=time.time();self.stats['latest_error']=self.stats['last_error'];self.stats['last_error']=None
                    self._stats_dirty=True
                self._queue_stats()

    def snapshot(self):
        return {**self.stats,'pending':len(self.pending),'closed':self.closed,
                'worker_running':bool(self.task and not self.task.done())}

    def _spool(self):
        path=self.path.with_suffix('.pending.json');temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            temp.write_text(json.dumps({k:v[1] for k,v in self.pending.items()},ensure_ascii=False),encoding='utf-8')
            os.replace(temp,path)
        finally:temp.unlink(missing_ok=True)

    async def close(self,timeout=5):
        if self.closed:return
        self._stats_dirty=True;self._queue_stats()
        try:
            async with asyncio.timeout(timeout):
                while self.pending:
                    self.progress.clear()
                    if not self.pending:break
                    self._check_worker()
                    await self.progress.wait()
        except (TimeoutError,RuntimeError):pass
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        if self._io:
            try:await asyncio.wait_for(asyncio.shield(self._io),3)
            except Exception:pass
        await asyncio.to_thread(self._spool)
        self.closed=True
