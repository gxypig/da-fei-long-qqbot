"""Two preparation lanes and a bounded immutable cache before model admission."""
import asyncio,hashlib,json,time
from collections import OrderedDict

class ContextPrepare:
    def __init__(self,entries=64,bytes_limit=64*1024*1024):
        self.slots=asyncio.Semaphore(2);self.entries=entries;self.limit=bytes_limit;self.cache=OrderedDict();self.bytes=0
        self.stats={'hits':0,'misses':0,'invalid':0,'wait_ms':0.,'prepare_ms':0.}
    @staticmethod
    def inspect(messages):
        from .context_window import split_rounds,estimate
        raw=json.dumps(messages,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
        split=split_rounds(messages)
        costs=[estimate(r) for r in split[1]] if split else []
        return hashlib.sha256(raw).hexdigest(),split,costs
    async def prepare(self,payload,scope,build):
        messages=payload.get('messages');started=time.monotonic()
        if not isinstance(messages,list):return payload
        async with self.slots:
            admitted=time.monotonic();self.stats['wait_ms']+=(admitted-started)*1000
            from .chain_metrics import add
            add('prepare_queue_ms',(admitted-started)*1000)
            try:
                try:digest,split,costs=await asyncio.to_thread(self.inspect,messages)
                except (TypeError,ValueError,RecursionError,AttributeError):
                    self.stats['invalid']+=1;return payload
                cachekey=(scope,digest)
                if cachekey in self.cache:
                    self.stats['hits']+=1;body=self.cache.pop(cachekey);self.cache[cachekey]=body
                    return {**payload,'messages':await asyncio.to_thread(json.loads,body)}
                self.stats['misses']+=1
                projected=build((split,costs))
                body=await asyncio.to_thread(lambda:json.dumps(projected['messages'],ensure_ascii=False,separators=(',',':')).encode())
                if len(body)<=self.limit:
                    old=self.cache.pop(cachekey,None)
                    if old:self.bytes-=len(old)
                    self.cache[cachekey]=body;self.bytes+=len(body)
                    while len(self.cache)>self.entries or self.bytes>self.limit:
                        _,removed=self.cache.popitem(last=False);self.bytes-=len(removed)
                return projected
            finally:
                elapsed=(time.monotonic()-admitted)*1000;self.stats['prepare_ms']+=elapsed;add('prepare_ms',elapsed)
    def clear(self):self.cache.clear();self.bytes=0
