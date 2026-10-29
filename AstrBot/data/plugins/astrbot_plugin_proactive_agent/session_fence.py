"""Reset and transport commit share one per-session boundary (no model lock)."""
import asyncio
from contextlib import asynccontextmanager,contextmanager
from contextvars import ContextVar
from functools import wraps

captured_session=ContextVar('qqbot_captured_session',default=None)


class SessionFence:
    def __init__(self,manager):
        self.manager=manager;self.locks={};self.epochs={};self.owners={};self.references=0;self.patches=[]

    @asynccontextmanager
    async def boundary(self,origin):
        # A manager method may call another manager method for the same origin.
        task=asyncio.current_task()
        if self.owners.get(origin) is task:
            yield;return
        async with self.locks.setdefault(origin,asyncio.Lock()):
            self.owners[origin]=task
            try:yield
            finally:self.owners.pop(origin,None)

    def epoch(self,origin):return self.epochs.get(origin,0)

    def acquire(self):
        self.references+=1
        if self.patches:return
        for name in ('new_conversation','switch_conversation','delete_conversation','delete_conversations_by_user_id','update_conversation'):
            previous=getattr(self.manager,name,None)
            if not callable(previous):continue
            def make(previous,name):
                @wraps(previous)
                async def change(origin=None,*args,**kwargs):
                    origin=origin if origin is not None else kwargs.pop('unified_msg_origin')
                    if not self.references:return await previous(origin,*args,**kwargs)
                    if name=='update_conversation':
                        history=kwargs.get('history',args[1] if len(args)>1 else None)
                        if history!=[]:return await previous(origin,*args,**kwargs)
                    async with self.boundary(origin):
                        before=self.epoch(origin)
                        self.epochs[origin]=before+1
                        result=await previous(origin,*args,**kwargs)
                        if name=='new_conversation':
                            # Only the executing task may adopt its own first
                            # conversation. An unrelated reset never refreshes
                            # a waiting/generated reply's fence token.
                            from .reply_jobs import current_job,TERMINAL
                            job=current_job.get()
                            if (job and job.origin==origin and job.cid is None
                                    and job.session_epoch==before and job.state not in TERMINAL
                                    and job.owner is asyncio.current_task()):
                                job.cid=result;job.session_epoch=self.epoch(origin)
                                job.generation='';job.first_conversation_created=True
                        return result
                return change
            wrapper=make(previous,name);setattr(self.manager,name,wrapper);self.patches.append((name,wrapper,previous))

    def release(self):
        self.references=max(0,self.references-1)
        if self.references:return
        for name,wrapper,previous in reversed(self.patches):
            if getattr(self.manager,name,None) is wrapper:setattr(self.manager,name,previous)
        self.patches=[]


def get_fence(manager):
    if manager is None:return None
    fence=getattr(manager,'_qqbot_send_fence',None)
    if fence is None:fence=SessionFence(manager);manager._qqbot_send_fence=fence
    return fence


@contextmanager
def for_session(origin,cid,epoch):
    token=captured_session.set((origin,cid,epoch))
    try:yield
    finally:captured_session.reset(token)
