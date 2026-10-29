"""Admit each actual OpenAI request, outside native preparation and retry sleeps."""
import asyncio,inspect
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass,field
from functools import wraps

_frame=ContextVar('qqbot_wire_attempts',default=None)

class RetryBudgetExhausted(RuntimeError):pass

@dataclass
class Frame:
    scheduler:object
    provider:object
    kind:str
    maximum:int=5
    hedge:object=None
    streaming:bool=False
    used:int=0
    last_error:object=None
    on_start:object=None
    streams:list=field(default_factory=list)

def native(provider):
    method=inspect.unwrap(getattr(type(provider),'text_chat',lambda:None))
    return getattr(method,'__module__','')=='astrbot.core.provider.sources.openai_source'

def attempts(kwargs):
    value=kwargs.get('request_max_retries')
    try:return max(1,int(value)) if value is not None else 5
    except (ValueError,TypeError):return 5

@asynccontextmanager
async def scope(scheduler,provider,kind,maximum=5,hedge=None,streaming=False,on_start=None):
    frame=Frame(scheduler,provider,kind,maximum,hedge,streaming,on_start=on_start)
    token=_frame.set(frame)
    try:yield frame
    finally:
        for stream in frame.streams:
            await stream.aclose()
        _frame.reset(token)

async def one_attempt(frame,factory):
    if frame.used>=frame.maximum:raise RetryBudgetExhausted('本次模型调用的共享重试预算已耗尽') from frame.last_error
    frame.used+=1
    async def request():
        if frame.on_start:frame.on_start()
        try:return await factory()
        except Exception as exc:
            frame.last_error=exc;raise
    if not frame.streaming:
        return await frame.scheduler.run(frame.provider,request,kind=frame.kind,hedge=frame.hedge)
    async def chunks():
        response=await request()
        try:
            async for chunk in response:yield chunk
        finally:
            close=getattr(response,'close',None) or getattr(response,'aclose',None)
            if close:
                value=close()
                if inspect.isawaitable(value):await value
    iterator=frame.scheduler.stream(frame.provider,chunks,kind=frame.kind)
    try:first=await anext(iterator)
    except StopAsyncIteration:first=None
    except BaseException:
        await iterator.aclose();raise
    async def replay():
        try:
            if first is not None:yield first
            async for item in iterator:yield item
        finally:await iterator.aclose()
    result=replay();frame.streams.append(result);frame.streams.append(iterator)
    return result

def install(runtime):
    from astrbot.core.provider.sources import openai_source
    previous=getattr(openai_source,'retry_provider_request',None);active=True
    if previous is None:return None
    @wraps(previous)
    async def retry(label,factory,**kwargs):
        frame=_frame.get()
        if not active or frame is None or frame.scheduler is not runtime.scheduler:
            return await previous(label,factory,**kwargs)
        # The native retry predicate/backoff and provider recovery remain authoritative.
        # Every retry crosses this wrapper, including another API-key or image fallback.
        return await previous(label,lambda:one_attempt(frame,factory),**kwargs)
    openai_source.retry_provider_request=retry
    def restore():
        nonlocal active
        active=False
        if openai_source.retry_provider_request is retry:openai_source.retry_provider_request=previous
    return restore
