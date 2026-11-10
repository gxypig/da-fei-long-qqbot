"""Per-job spans, with provider time and queue time kept separate."""
from contextlib import contextmanager
from contextvars import ContextVar
import time
from functools import wraps
from .reply_jobs import current_job,TERMINAL
phase=ContextVar('qqbot_model_phase',default='main')

def timed(name):
    def decorate(function):
        @wraps(function)
        async def call(*args,**kwargs):
            started=time.monotonic()
            try:return await function(*args,**kwargs)
            finally:add(name,(time.monotonic()-started)*1000);span(name,started)
        return call
    return decorate

def stage(name):
    job=current_job.get()
    if job and job.state not in TERMINAL and job.stage!=name:
        job.stage=name
        queue=getattr(job,'queue',None)
        if queue:queue.changed('stage',job)

def add(name,milliseconds):
    job=current_job.get()
    if job:job.timings[name]=job.timings.get(name,0)+max(0,milliseconds)

def span(name,started,ended=None,job=None):
    job=job or current_job.get()
    if not job:return
    ended=time.monotonic() if ended is None else ended
    if not hasattr(job,'spans'):job.spans=[]
    if len(job.spans)<200:job.spans.append({'stage':name,'start_ms':round(max(0,started-job.received)*1000,2),'end_ms':round(max(0,ended-job.received)*1000,2)})

def timeline(job):
    spans=getattr(job,'spans',[]);intervals=sorted((s['start_ms'],s['end_ms']) for s in spans)
    total=0;end=0
    for a,b in intervals:total+=max(0,b-max(a,end));end=max(end,b)
    return {'spans':spans,'covered_ms':round(total,2),'overlap_ms':round(sum(b-a for a,b in intervals)-total,2),
        'note':'阶段可重叠；用户等待按接收至实际回执计算，不把并行耗时相加'}

@contextmanager
def operation(name):
    token=phase.set(name);started=time.monotonic()
    if name=='review':stage('review')
    try:yield
    finally:
        add(name+'_total_ms',(time.monotonic()-started)*1000);span(name,started);phase.reset(token)

def model_completed(seconds,queue_ms,kind):
    name=phase.get()
    if kind in {'background','scheduled'} and current_job.get() is None:return
    if kind=='auxiliary' and name=='main':name='auxiliary'
    add(name+'_model_ms',seconds*1000);add(name+'_model_queue_ms',queue_ms)
    now=time.monotonic();span(name+'_model',now-seconds,now);span(name+'_model_queue',now-seconds-queue_ms/1000,now-seconds)
    job=current_job.get()
    if job:job.timings[name+'_model_rounds']=job.timings.get(name+'_model_rounds',0)+1
