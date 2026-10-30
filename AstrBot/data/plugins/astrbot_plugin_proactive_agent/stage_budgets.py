"""One tool execution deadline with separately bounded queue/review/receipt waits."""
import asyncio,time
from contextlib import asynccontextmanager
from contextvars import ContextVar

deadline=ContextVar('qqbot_tool_execution_deadline',default=None)
_budgets=ContextVar('qqbot_tool_budget_stack',default=())

def remaining(cap=45,reserve=0):
    active=_budgets.get()
    end=min((b['end'] for b in active if not b['paused']),default=deadline.get())
    return max(.01,min(cap,end-time.monotonic()-reserve)) if end is not None else cap

@asynccontextmanager
async def total_budget(seconds):
    end=time.monotonic()+remaining(seconds);token=deadline.set(end)
    async with asyncio.timeout_at(end) as timer:
        budget={'end':end,'timer':timer,'paused':0};stack=_budgets.set((*_budgets.get(),budget))
        try:yield
        finally:_budgets.reset(stack);deadline.reset(token)

@asynccontextmanager
async def separate_stage(name,seconds):
    """Only elapsed time is excluded; disabled review reserves no extra time."""
    budgets=_budgets.get();started=time.monotonic();token=deadline.set(None)
    for b in budgets:
        if not b['paused']:b['pause_start']=started;b['timer'].reschedule(None)
        b['paused']+=1
    try:
        async with asyncio.timeout(seconds):yield
    finally:
        now=time.monotonic()
        for b in budgets:
            b['paused']-=1
            if not b['paused']:
                b['end']+=now-b['pause_start'];b['timer'].reschedule(b['end'])
        deadline.reset(token)
        from .reply_jobs import current_job
        job=current_job.get()
        if job:job.timings['tool_'+name+'_ms']=job.timings.get('tool_'+name+'_ms',0)+(now-started)*1000
