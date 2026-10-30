"""Shared pure generation, bounded tool lifetime and waiter-safe session locks."""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import time

from data.plugins.astrbot_plugin_proactive_agent.stage_budgets import deadline,remaining,total_budget,separate_stage

class SessionLock:
    def __init__(self):self.lock=asyncio.Lock();self.users=0
    def locked(self):return self.users>0 or self.lock.locked()
    async def __aenter__(self):
        self.users+=1
        try:
            async with separate_stage('queue',180):await self.lock.acquire()
        except BaseException:self.users-=1;raise
        return self
    async def __aexit__(self,*args):self.lock.release();self.users-=1

class SharedCopy:
    def __init__(self):self.flights={};self.cache={};self.stats={'joined':0,'generated':0,'hit':0}
    async def get(self,key,factory):
        cached=self.cache.get(key)
        if cached and time.monotonic()-cached[0]<120:self.stats['hit']+=1;return cached[1]
        flight=self.flights.get(key)
        if flight is None:
            flight={'task':asyncio.create_task(factory(),name='bili-shared-caption'),'waiters':0};self.flights[key]=flight
            self.stats['generated']+=1
        else:self.stats['joined']+=1
        flight['waiters']+=1
        try:
            result=await asyncio.shield(flight['task'])
            self.cache[key]=(time.monotonic(),result)
            while len(self.cache)>64:self.cache.pop(next(iter(self.cache)))
            return result
        finally:
            flight['waiters']-=1
            if not flight['waiters']:
                if self.flights.get(key) is flight:self.flights.pop(key,None)
                if not flight['task'].done():flight['task'].cancel()
                await asyncio.gather(flight['task'],return_exceptions=True)
    async def close(self):
        tasks=[r['task'] for r in self.flights.values()]
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        self.flights.clear()
