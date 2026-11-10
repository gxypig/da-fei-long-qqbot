"""Read-only I/O lifetime is independent of foreground model admission."""
import asyncio,time,json,os
from .long_memory_store import digest
from .work_scheduler import BackgroundPaused


class ReadingRuntime:
    def __init__(self,service):
        self.s=service;self.inflight={};self.idle_since=None;self.slots=asyncio.Semaphore(1)
        self.stats={'downloads':0,'cache_hits':0,'joins':0,'completed_while_busy':0}
    def ready(self):
        if self.s.plugin._work.busy():self.idle_since=None;return False
        now=self.s.clock()
        if self.idle_since is None:self.idle_since=now
        return now-self.idle_since>=10
    async def read(self,key,factory,*,ttl=21600,timeout=90):
        """One owned request, durable result before returning. Cancellation doesn't discard it."""
        cache_key='read-cache:'+digest(key);old=self.s.store.rows.get(cache_key,{})
        cached=old.get('state')=='ready' and old.get('at',0)+ttl>self.s.clock()
        if cached and isinstance(old.get('result'),dict) and old['result'].get('path'):
            from pathlib import Path
            cached=await asyncio.to_thread(Path(old['result']['path']).is_file)
        if cached:
            self.stats['cache_hits']+=1;return old['result']
        task=self.inflight.get(cache_key)
        if task is None:
            async def work():
                async with self.slots:
                    if self.s.closed:raise BackgroundPaused('阅读服务停止')
                    if self.s.plugin._work.busy():raise BackgroundPaused('聊天开始，暂不启动新的网络请求')
                    self.stats['downloads']+=1
                    async with asyncio.timeout(timeout):result=await factory()
                    if self.s.plugin._work.busy():self.stats['completed_while_busy']+=1;self.idle_since=None
                    self.s.store.put(cache_key,{'type':'read_cache','state':'ready','at':self.s.clock(),'result':result})
                    await self.s.store.flush(cache_key)
                    rows=sorted(((k,r) for k,r in self.s.store.rows.items() if r.get('type')=='read_cache' and r.get('state')=='ready'),key=lambda pair:pair[1].get('at',0),reverse=True)
                    for stale,row in rows[64:]:self.s.store.put(stale,{k:v for k,v in {**row,'state':'expired'}.items() if k!='result'})
                    return result
            task=asyncio.create_task(work(),name='reading-network')
            self.inflight[cache_key]=task
            def finished(done):
                self.inflight.pop(cache_key,None)
                if not done.cancelled():done.exception() # consumed by caller or preserved on disk
            task.add_done_callback(finished)
        else:self.stats['joins']+=1
        return await asyncio.shield(task)
    async def model(self,operation,generation):
        """Fallback preemption also covers providers which have no scheduler hook."""
        task=asyncio.create_task(operation)
        try:
            while not task.done():
                self.s.check(generation,'active_reading_enabled')
                await asyncio.wait({task},timeout=.05)
            self.s.check(generation,'active_reading_enabled');return task.result()
        finally:
            if not task.done():task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    def checkpoint(self,url,version=None):return RawCheckpoint(self.s,[url,version])
    async def close(self):
        # Never release ownership while an HTTP/browser child still runs. Requests are bounded.
        if self.inflight:await asyncio.gather(*list(self.inflight.values()),return_exceptions=True)


def reading_evidence(material):
    level=material.get('reading_level')
    if not level:
        level='body_fragment' if material.get('body_verified') else 'search_excerpt' if '摘要' in material.get('scope','') else 'metadata'
    return {'level':level,'body_verified':bool(material.get('body_verified')),'truncated':bool(material.get('truncated')),
            'images_recognized':bool(material.get('images_recognized')),'image_count':material.get('image_count',0)}


class RawCheckpoint:
    """HTML lives in a bounded on-disk cache, never in the chat prompt or gallery."""
    def __init__(self,service,key):
        self.s=service;self.id='raw-read:'+digest(key)
        self.path=service.db.path.parent/'reading-cache'/(digest(key)+'.json')
        self.generation=service.generation
    async def load(self):
        row=self.s.store.rows.get(self.id,{})
        if row.get('at',0)+21600<=self.s.clock():return None
        try:return json.loads(await asyncio.to_thread(self.path.read_text,'utf-8'))
        except (OSError,ValueError):return None
    async def save(self,raw):
        def write():
            self.path.parent.mkdir(parents=True,exist_ok=True)
            tmp=self.path.with_suffix('.tmp');tmp.write_bytes(json.dumps(raw,ensure_ascii=False).encode());os.replace(tmp,self.path)
            # Only this exact cache directory and its own .json files are managed.
            entries=sorted(self.path.parent.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
            for p in entries[64:]:p.unlink(missing_ok=True)
        await asyncio.to_thread(write)
        self.s.store.put(self.id,{'type':'read_checkpoint','stage':'fetched','at':self.s.clock(),'url':raw['url']})
        await self.s.store.flush(self.id)
    async def parse(self,raw,parser):
        self.s.check(self.generation,'active_reading_enabled')
        # to_thread cancellation does not stop its worker. Retain ownership until exit.
        task=asyncio.create_task(asyncio.to_thread(parser,raw))
        try:result=await asyncio.shield(task)
        finally:
            if not task.done():await asyncio.gather(task,return_exceptions=True)
        self.s.store.put(self.id,{**self.s.store.rows.get(self.id,{}),'stage':'parsed'})
        await self.s.store.flush(self.id)
        return result
