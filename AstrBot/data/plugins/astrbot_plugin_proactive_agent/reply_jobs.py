"""Actual reply admission, owned by the event loop; no model or transport I/O."""
from __future__ import annotations
import asyncio
from collections import OrderedDict, deque
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import re
import time
import uuid

current_job = ContextVar('qqbot_reply_job', default=None)
TERMINAL = {'confirmed','partial','failed','unknown','cancelled','merged','ignored'}


class QueueConflict(ValueError):
    pass


class ReplyCancelled(Exception):
    pass


def error_fingerprint(text):
    """Only machine error envelopes, not general questions or conversational tone."""
    if not re.search(r'(?:HTTP\s*[45]\d\d|API(?:Error|\s*错误)|(?:错误|error|retcode|status_code)\s*[:：=]|rate_limit|insufficient_quota|Traceback \(most recent call)', text, re.I):
        return None
    # A question/new detail must still receive a normal reply.
    if re.search(r'[?？]|怎么办|为什么|帮我|如何|我(?:改|试|换|发现)|但是|另外|补充', text):
        return None
    normalized=re.sub(r'(?i)((?:request|trace|req)[_-]?id\s*[=:：]\s*)[\w-]+',r'\1<ID>',text)
    normalized=re.sub(r'\b[0-9a-f]{8}-[0-9a-f-]{27,}\b','<ID>',normalized,flags=re.I)
    return hashlib.sha256(normalized.strip().encode()).hexdigest()


@dataclass
class ReplyJob:
    origin: str
    sender: str
    source_id: str
    text: str
    event: object = field(repr=False, default=None)
    id: str = field(default_factory=lambda:uuid.uuid4().hex)
    received: float = field(default_factory=time.monotonic)
    created: float = field(default_factory=time.time)
    state: str = 'received'
    stage: str = 'conversion'
    reason: str = ''
    cid: str | None = None
    generation: str = ''
    session_epoch: int | None = None
    force_quote: bool = False
    manually_adjusted: bool = False
    sources: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    sends: list = field(default_factory=list)
    gate: object = field(default=None,repr=False)
    owner: object = field(default=None,repr=False)
    passive: bool = False

    def __post_init__(self):
        if not self.sources:
            self.sources=[{'id':self.source_id,'text':self.text,'sender':self.sender}]

    def public(self, now):
        return {'id':self.id,'origin':self.origin,'sender':self.sender,'message_id':self.source_id,
                'summary':self.text[:300],'source_count':len(self.sources),'state':self.state,
                'stage':self.stage,'reason':self.reason,'force_quote':self.force_quote,
                'adjusted':self.manually_adjusted,'wait_ms':round(1000*max(0,self.timings.get('started',now)-self.received)),
                'elapsed_ms':round(1000*max(0,self.timings.get('ended',now)-self.received)),
                'stages_ms':{k:round(v,2) for k,v in self.timings.items() if k.endswith('_ms')}}


class ReplyQueue:
    def __init__(self, cfg, audit=lambda *_:None, clock=time.monotonic):
        self.cfg,self.audit,self.clock=cfg,audit,clock
        self.instance=uuid.uuid4().hex;self.version=0;self.closed=False
        self.jobs={};self.pending=[];self.active={};self.ended=deque(maxlen=200)
        self.actions=OrderedDict();self.echoes=OrderedDict()
        self.started=time.time();self.on_change=lambda:None

    def changed(self, action, job=None):
        self.version+=1
        try:self.audit('reply_queue',{'instance':self.instance,'version':self.version,'action':action,'job_id':job.id if job else None})
        except Exception:pass
        try:self.on_change()
        except Exception:pass

    def create(self, event, received=None):
        existing=event.get_extra('qqbot_reply_job')
        if existing and existing.id in self.jobs:return existing
        job=ReplyJob(str(event.unified_msg_origin),str(event.get_sender_id()),
            str(getattr(getattr(event,'message_obj',None),'message_id','') or ''),event.get_message_str() or '',event,
            received=received if received is not None else self.clock())
        self.jobs[job.id]=job;event.set_extra('qqbot_reply_job',job)
        job.queue=self
        return job

    def submit(self, job, *, mergeable=False, probability=False):
        if self.closed:raise ReplyCancelled('队列已关闭')
        if job.state!='received':return job
        job.owner=asyncio.current_task();job.passive=bool(probability and mergeable)
        if len(self.pending)>=500:
            self.finish(job,'cancelled','queue_capacity');return job
        fingerprint=error_fingerprint(job.text)
        key=(job.origin,job.sender,fingerprint)
        if fingerprint:
            previous=self.echoes.get(key)
            self.echoes[key]=(self.clock(),(previous[1]+1 if previous else 1))
            self.echoes.move_to_end(key)
            while len(self.echoes)>1000:self.echoes.popitem(last=False)
            if previous and self.clock()-previous[0]<120:
                self.finish(job,'merged','重复接口错误已归并；原文保留在接收记录')
                return job
        if mergeable:
            candidates=[self.jobs[x] for x in self.pending if self.jobs[x].origin==job.origin]
            old=candidates[-1] if candidates else None
            if (old and old.sender==job.sender and not old.manually_adjusted
                    and old.event.get_extra('qqbot_queue_mergeable') and len(old.sources)<8
                    and sum(len(s['text']) for s in old.sources)+len(job.text)<=1600):
                old.sources.extend(job.sources)
                old.passive=old.passive and job.passive
                old.event.set_extra('qqbot_chat_batch',[s['text'] for s in old.sources])
                old.text=' / '.join(s['text'] for s in old.sources)
                self.finish(job,'merged','merged_into:'+old.id)
                self.changed('merge',old);return job
        job.event.set_extra('qqbot_queue_mergeable',mergeable)
        job.gate=asyncio.get_running_loop().create_future()
        job.state='pending';job.stage='reply_queue';self.pending.append(job.id)
        self.changed('queued',job);self.pump()
        return job

    def pump(self):
        if self.closed:return
        limit=max(1,min(8,int(self.cfg('reply_queue_concurrency',2))))
        occupied={j.origin for j in self.active.values()}
        for id_ in list(self.pending):
            job=self.jobs[id_]
            # Only obsolete probabilistic small-talk may yield to a newer
            # candidate. Explicit requests and manually adjusted jobs survive.
            newer=any(self.jobs[x].origin==job.origin and self.jobs[x].passive for x in self.pending[self.pending.index(id_)+1:])
            stale=(self.clock()-job.received>=int(self.cfg('group_passive_max_age',25)))
            if (job.passive and not job.manually_adjusted and newer and self.cfg('group_flow_enabled',True)
                    and (stale or self.cfg('latency_optimization_enabled',False))):
                self.finish(job,'cancelled','过时概率接话，由较新候选接话',pump=False);continue
            if len(self.active)>=limit:job.reason='等待前台执行名额';continue
            if job.origin in occupied:job.reason='同一会话上一条仍在执行';continue
            self.pending.remove(id_);self.active[id_]=job;occupied.add(job.origin)
            job.state='running';job.stage='preparing';job.reason=''
            job.timings['started']=self.clock()
            if job.gate and not job.gate.done():job.gate.set_result(True)
            self.changed('started',job)

    async def wait(self,job):
        if job.state in TERMINAL:raise ReplyCancelled(job.reason)
        try:await job.gate
        except asyncio.CancelledError:
            self.finish(job,'cancelled','caller_cancelled');raise
        if job.state!='running':raise ReplyCancelled(job.reason)

    def finish(self,job,state,reason='',*,pump=True):
        if job.state in TERMINAL:return
        assert state in TERMINAL
        if job.id in self.pending:self.pending.remove(job.id)
        self.active.pop(job.id,None)
        job.state=state;job.stage='ended';job.reason=reason;job.timings['ended']=self.clock()
        if job.gate and not job.gate.done():job.gate.set_result(False)
        if len(self.ended)==self.ended.maxlen:self.jobs.pop(self.ended[0],None)
        self.ended.append(job.id);self.changed('finished',job)
        if pump:self.pump()

    def action(self,body):
        if not isinstance(body,dict):raise ValueError('操作必须为对象')
        request_id=body.get('request_id');expected=body.get('version')
        if not isinstance(request_id,str) or not 8<=len(request_id)<=100:raise ValueError('需要幂等请求ID')
        digest=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
        cached=self.actions.get(request_id)
        if cached:
            if cached!=digest:raise QueueConflict('同一请求ID不能复用于不同操作')
            return {**self.snapshot(),'duplicate':True}
        if body.get('instance')!=self.instance or type(expected) is not int or expected!=self.version:
            raise QueueConflict('队列已变化，请重新读取')
        job=self.jobs.get(body.get('job_id'))
        if not job or job.state!='pending':raise QueueConflict('任务已开始或结束，不能移动/删除')
        position=self.pending.index(job.id);action=body.get('action')
        if action=='delete':
            affected=[self.jobs[x] for x in self.pending[position+1:] if self.jobs[x].origin==job.origin]
        elif action=='move':
            anchor=body.get('before')
            if anchor==job.id:raise ValueError('不能移动到自己之前')
            if anchor is not None and anchor not in self.pending:raise QueueConflict('移动目标已变化')
            target=self.pending.index(anchor) if anchor else len(self.pending)
            affected=[self.jobs[x] for x in self.pending[min(position,target):max(position,target)+(target<position)]]
            if job not in affected:affected.append(job)
        else:raise ValueError('未知队列操作')
        for row in affected:row.force_quote=True;row.manually_adjusted=True
        if action=='delete':self.finish(job,'cancelled','管理员取消本次回复')
        else:
            self.pending.remove(job.id)
            self.pending.insert(self.pending.index(anchor) if anchor else len(self.pending),job.id)
            self.changed('manual_move',job)
        self.actions[request_id]=digest
        while len(self.actions)>512:self.actions.popitem(last=False)
        return self.snapshot()

    def snapshot(self,version=None,instance=None,offset=0,limit=50):
        common={'instance':self.instance,'version':self.version,'started':self.started,'closed':self.closed}
        if instance==self.instance and version==self.version:return {**common,'unchanged':True}
        offset=max(0,int(offset));limit=max(1,min(100,int(limit)));now=self.clock()
        return {**common,'unchanged':False,'offset':offset,'total':len(self.pending),'echo_groups':len(self.echoes),
            'first_pending':self.pending[0] if self.pending else None,
            'echo_merged':sum(max(0,count-1) for _,count in self.echoes.values()),
            'pending':[{**self.jobs[x].public(now),'position':i,'previous_id':self.pending[i-1] if i else None,
                        'after_next_id':self.pending[i+2] if i+2<len(self.pending) else None}
                       for i,x in enumerate(self.pending[offset:offset+limit],offset)],
            'running':[j.public(now) for j in self.active.values()],
            'recent':[self.jobs[x].public(now) for x in list(reversed(self.ended))[:limit]]}

    def close(self):
        self.closed=True
        for id_ in list(self.pending):self.finish(self.jobs[id_],'cancelled','service_shutdown')
        self.changed('closed')

    async def drain(self,timeout=8):
        self.close()
        owners={j.owner for j in self.active.values() if j.owner and j.owner is not asyncio.current_task()}
        if not owners:return
        _,pending=await asyncio.wait(owners,timeout=timeout)
        for task in pending:task.cancel()
        if pending:await asyncio.wait(pending,timeout=2)
