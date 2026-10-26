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
            finally:add(name,(time.monotonic()-started)*1000)
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

@contextmanager
def operation(name):
    token=phase.set(name);started=time.monotonic()
    if name=='review':stage('review')
    try:yield
    finally:
        add(name+'_total_ms',(time.monotonic()-started)*1000);phase.reset(token)

def model_completed(seconds,queue_ms,kind):
    name=phase.get()
    if kind in {'background','scheduled'} and current_job.get() is None:return
    if kind=='auxiliary' and name=='main':name='auxiliary'
    add(name+'_model_ms',seconds*1000);add(name+'_model_queue_ms',queue_ms)
    job=current_job.get()
    if job:job.timings[name+'_model_rounds']=job.timings.get(name+'_model_rounds',0)+1
