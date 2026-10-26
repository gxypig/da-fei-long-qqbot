"""Observe core pool ownership and collapse concurrent identical preference reads."""
import asyncio
import copy
import time
from collections import deque
from contextlib import asynccontextmanager
import inspect
from contextvars import ContextVar

session_owner=ContextVar('qqbot_db_session_owner',default='unknown')

def caller_owner():
    frame=inspect.currentframe()
    try:
        for _ in range(35):
            frame=frame.f_back if frame else None
            if frame is None:break
            if frame.f_code.co_filename.replace('\\','/').endswith('/db/sqlite.py'):
                return frame.f_code.co_name
        return 'unknown'
    finally:del frame

class DatabasePressure:
    def __init__(self,plugin):
        self.plugin=plugin;self.db=getattr(plugin.context,'_db',None)
        self.active={};self.recent=deque(maxlen=100);self.flights={};self.slots=asyncio.Semaphore(4)
        self.stats={'read_joined':0,'read_started':0,'session_errors':0,'checkouts':0,'checkins':0}
        self.restores=[]

    def snapshot(self):
        now=time.monotonic()
        return {**self.stats,'active':[{'owner':r[1],'held_ms':round((now-r[0])*1000)} for r in self.active.values()],
                'recent':list(self.recent),'shared_reads':len(self.flights)}

    def install(self):
        if self.db is None:return self.close
        from sqlalchemy import event
        from astrbot.core.db import BaseDatabase
        from astrbot.core.utils.shared_preferences import SharedPreferences
        engine=self.db.engine.sync_engine
        def checkout(connection,record,proxy):
            owner=session_owner.get()
            if owner=='unknown':owner=caller_owner()
            self.active[id(record)]=(time.monotonic(),owner);self.stats['checkouts']+=1
        def checkin(connection,record):
            row=self.active.pop(id(record),None);self.stats['checkins']+=1
            if row:
                elapsed=(time.monotonic()-row[0])*1000
                self.recent.append({'owner':row[1],'held_ms':round(elapsed),'at':time.time()})
                if elapsed>2000:self.plugin._audit('db_connection_slow',{'owner':row[1],'held_ms':round(elapsed)})
        event.listen(engine,'checkout',checkout);event.listen(engine,'checkin',checkin)
        self.restores.append(lambda:event.remove(engine,'checkout',checkout))
        self.restores.append(lambda:event.remove(engine,'checkin',checkin))
        original=SharedPreferences.get_async;missing=object()
        async def get(prefs,scope,scope_id,key,default=None):
            if prefs.db_helper is not self.db:return await original(prefs,scope,scope_id,key,default)
            identity=(id(prefs),scope,scope_id,key)
            task=self.flights.get(identity)
            if task is None:
                if len(self.flights)>=256:
                    async with self.slots:return await original(prefs,scope,scope_id,key,default)
                self.stats['read_started']+=1
                async def read():
                    async with self.slots:return await original(prefs,scope,scope_id,key,missing)
                task=asyncio.create_task(read(),name='preferences-shared-read');self.flights[identity]=task
                def done(finished):
                    if self.flights.get(identity) is finished:self.flights.pop(identity,None)
                    if not finished.cancelled():finished.exception()
                task.add_done_callback(done)
            else:self.stats['read_joined']+=1
            result=await asyncio.shield(task)
            return default if result is missing else copy.deepcopy(result)
        SharedPreferences.get_async=get
        def restore_get():
            if SharedPreferences.get_async is get:SharedPreferences.get_async=original
        self.restores.append(restore_get)
        previous=BaseDatabase.get_db
        @asynccontextmanager
        async def session(db):
            started=time.monotonic()
            token=session_owner.set(caller_owner())
            try:
                async with previous(db) as value:yield value
            except Exception as exc:
                if db is self.db:
                    self.stats['session_errors']+=1
                    self.plugin._audit('db_session_failed',{'error':type(exc).__name__,'elapsed_ms':round((time.monotonic()-started)*1000),'held':len(self.active)})
                raise
            finally:session_owner.reset(token)
        BaseDatabase.get_db=session
        def restore_session():
            if BaseDatabase.get_db is session:BaseDatabase.get_db=previous
        self.restores.append(restore_session)
        return self.close

    def close(self):
        for restore in reversed(self.restores):restore()
        self.restores.clear()
        for task in self.flights.values():task.cancel()
