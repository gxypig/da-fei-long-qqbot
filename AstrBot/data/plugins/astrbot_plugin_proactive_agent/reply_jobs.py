"""Actual reply admission, owned by the event loop; no model or transport I/O."""
from __future__ import annotations
import asyncio
from collections import OrderedDict, deque
from contextvars import ContextVar
from contextlib import asynccontextmanager
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


_REJECTION_PREFIX=re.compile(r'^The prompt could not be submitted\. The prompt contains sensitive words that violate Google',re.I)
_REJECTION_TAIL=re.compile(r'^se policy\]\(https://policies\.google\.com/terms/generative-ai/use-policy\)\. Try rephrasing the prompt\.',re.I)
_REJECTION_KEY=hashlib.sha256(b'google-generative-prompt-rejection').hexdigest()
_ERROR_HELP=re.compile(r'[?？]|怎么办|为什么|帮我|如何|我(?:改|试|换|发现)|但是|另外|补充|\b(?:why|how|help me|can you)\b',re.I)

def error_fingerprint(text):
    """Only machine error envelopes, not general questions or conversational tone."""
    known_rejection=bool(_REJECTION_PREFIX.match(text.strip()))
    if not known_rejection and not re.search(r'(?:HTTP\s*[45]\d\d|API(?:Error|\s*错误)|(?:错误|error|retcode|status_code)\s*[:：=]|rate_limit|insufficient_quota|Traceback \(most recent call)', text, re.I):
        return None
    # A question/new detail must still receive a normal reply.
    if _ERROR_HELP.search(text):
        return None
    if known_rejection:return _REJECTION_KEY
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
    mute_epoch: object = None
    force_quote: bool = False
    manually_adjusted: bool = False
    sources: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    sends: list = field(default_factory=list)
    gate: object = field(default=None,repr=False)
    owner: object = field(default=None,repr=False)
    passive: bool = False
    execution_result: str = 'pending'
    delivery_result: str = 'not_sent'
    reply_purpose: str = 'answer'
    first_conversation_created: bool = False

    @property
    def object_key(self):
        from .route_guard import canonical
        try:return canonical(self.origin)
        except (ValueError,AttributeError):return self.origin

    def __post_init__(self):
        if not self.sources:
            self.sources=[{'id':self.source_id,'text':self.text,'sender':self.sender}]

    def public(self, now):
        return {'id':self.id,'origin':self.origin,'sender':self.sender,'message_id':self.source_id,
                'summary':self.text[:300],'source_count':len(self.sources),'state':self.state,
                'stage':self.stage,'reason':self.reason,'force_quote':self.force_quote,
                'execution_result':self.execution_result,'delivery_result':self.delivery_result,'reply_purpose':self.reply_purpose,
                'admission':getattr(self,'admission',None),
                'adjusted':self.manually_adjusted,'wait_ms':round(1000*max(0,self.timings.get('started',now)-self.received)),
                'elapsed_ms':round(1000*max(0,self.timings.get('ended',now)-self.received)),
                'stages_ms':{k:round(v,2) for k,v in self.timings.items() if k.endswith('_ms')}}


class ReplyQueue:
    def __init__(self, cfg, audit=lambda *_:None, clock=time.monotonic):
        self.cfg,self.audit,self.clock=cfg,audit,clock
        self.instance=uuid.uuid4().hex;self.version=0;self.closed=False
        self.jobs={};self.pending=[];self.active={};self.ended=deque(maxlen=200)
        self.inflight={};self.suspended={};self.last_object=None;self.manual_order=False
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
        if (existing and existing.id in self.jobs and existing.state not in TERMINAL
                and existing.event is event):return existing
        # A framework session waiter shallow-copies the new event and its extra
        # map. Give the new execution an independent map and job identity.
        if existing and hasattr(event,'_extras'):
            event._extras=dict(event._extras)
            for key in ('qqbot_reply_job','_qqbot_work_state','qqbot_character','qqbot_native_persona','qqbot_unified_admitted','qqbot_flow_started','qqbot_model_started',
                        'qqbot_review_settings','qqbot_reply_prediction','qqbot_prediction_version','qqbot_prediction_persona','qqbot_send_path','qqbot_emoji_selection','qqbot_generation_request','qqbot_local_repair_used'):
                event._extras.pop(key,None)
        job=ReplyJob(str(event.unified_msg_origin),str(event.get_sender_id()),
            str(getattr(getattr(event,'message_obj',None),'message_id','') or ''),event.get_message_str() or '',event,
            received=received if received is not None else self.clock())
        self.jobs[job.id]=job;event.set_extra('qqbot_reply_job',job)
        from .session_mute import get_mutes
        mutes=get_mutes()
        if mutes:
            try:job.mute_epoch=mutes.epoch(job.origin)
            except ValueError:pass
        job.queue=self
        return job

    def submit(self, job, *, mergeable=False, probability=False):
        if self.closed:raise ReplyCancelled('队列已关闭')
        if job.state!='received':return job
        job.owner=asyncio.current_task();job.passive=bool(probability and mergeable)
        if len(self.pending)>=500:
            self.finish(job,'cancelled','queue_capacity');return job
        if not self.precheck(job):return job
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

    def precheck(self,job):
        if getattr(job,'conversation_checked',False):return job.state not in TERMINAL
        job.conversation_checked=True
        controls=getattr(self,'controls',None)
        reason=controls.before(job) if controls else None
        if reason:self.finish(job,'ignored',reason);return False
        parts=getattr(job.event,'get_messages',lambda:[])() if job.event else []
        empty_mention=bool(parts) and any(type(p).__name__=='At' for p in parts) and all(type(p).__name__ in {'At','Plain'} for p in parts)
        if not job.text.strip() and empty_mention:
            previous=next((old for old in reversed(list(self.jobs.values())) if old.id!=job.id and old.object_key==job.object_key
                and old.sender==job.sender and old.text.strip() and 0<=job.received-old.received<=30
                and old.state not in {'failed','cancelled','ignored','merged'}),None)
            if previous:
                job.linked_job=previous.id;self.finish(job,'merged','空@关联原问题:'+previous.id);return False
        fingerprint=error_fingerprint(job.text)
        # A known split error tail is linked only if this same sender/session
        # already has its head. An isolated fragment is not silently swallowed.
        if not fingerprint and _REJECTION_TAIL.match(job.text.strip()) and not _ERROR_HELP.search(job.text):
            previous=self.echoes.get((job.origin,job.sender,_REJECTION_KEY))
            if previous and (previous['state'] in {'pending','unknown'} or previous['state']=='confirmed' and self.clock()-previous['at']<120):fingerprint=_REJECTION_KEY
        key=(job.origin,job.sender,fingerprint)
        if fingerprint:
            previous=self.echoes.get(key)
            active=previous and (previous['state'] in {'pending','unknown'} or previous['state']=='confirmed' and self.clock()-previous['at']<120)
            if active:
                previous['count']+=1
                job.linked_job=previous['job']
                self.finish(job,'merged','重复接口错误已归并；原文保留在接收记录')
                return False
            self.echoes[key]={'at':self.clock(),'count':1,'job':job.id,'state':'pending'}
            self.echoes.move_to_end(key);job.error_key=key
            while len(self.echoes)>1000:self.echoes.popitem(last=False)
        return True

    def reconcile_echo(self,job_id,state):
        for key,row in list(self.echoes.items()):
            if row['job']!=job_id:continue
            if state in {'confirmed','partial'}:
                if row['state']!='confirmed':row.update(state='confirmed',at=self.clock())
            elif state=='unknown':row['state']='unknown'
            elif state in TERMINAL:self.echoes.pop(key,None)

    def restore_echoes(self,ledger,wall_clock=time.time):
        """Unknown deliveries survive restart; acknowledged TTL never restarts."""
        now=wall_clock()
        for row in sorted(ledger.store.rows.values(),key=lambda r:r.get('at',0)):
            if not row.get('job_id') or row.get('optional'):continue
            fingerprint=error_fingerprint(row.get('source_text',''))
            if not fingerprint:continue
            state=row.get('state');at=row.get('confirmed_at',row.get('result_at',row.get('at',0)))
            if state=='confirmed' and now-at>=120:continue
            if state not in {'unknown','submitted','sending','confirmed'}:continue
            key=(row['origin'],str(row.get('sender','')),fingerprint)
            self.echoes[key]={'at':self.clock()-max(0,now-at),'count':1,'job':row['job_id'],
                              'state':'confirmed' if state=='confirmed' else 'unknown'}
        while len(self.echoes)>1000:self.echoes.popitem(last=False)

    def pump(self):
        if self.closed:return
        limit=max(1,min(8,int(self.cfg('reply_queue_concurrency',4))))
        inflight_limit=max(limit,min(64,int(self.cfg('reply_inflight_limit',8))))
        occupied={j.object_key:j.id for j in self.inflight.values()}
        candidates=list(self.pending)
        if not self.manual_order and self.last_object is not None:
            candidates.sort(key=lambda id_:self.jobs[id_].object_key==self.last_object)
        for id_ in candidates:
            job=self.jobs[id_]
            # Only obsolete probabilistic small-talk may yield to a newer
            # candidate. Explicit requests and manually adjusted jobs survive.
            newer=any(self.jobs[x].origin==job.origin and self.jobs[x].passive for x in self.pending[self.pending.index(id_)+1:])
            stale=(self.clock()-job.received>=int(self.cfg('group_passive_max_age',25)))
            if (job.id not in self.inflight and job.passive and not job.manually_adjusted and newer and self.cfg('group_flow_enabled',True)
                    and (stale or self.cfg('latency_optimization_enabled',False))):
                self.finish(job,'cancelled','过时概率接话，由较新候选接话',pump=False);continue
            if len(self.active)>=limit:job.reason='等待前台执行名额';continue
            if job.object_key in occupied and occupied[job.object_key]!=job.id:job.reason='同一会话上一条仍在执行';continue
            if job.id not in self.inflight and len(self.inflight)>=inflight_limit:job.reason='等待在途任务名额';continue
            self.pending.remove(id_);self.active[id_]=job;self.inflight[id_]=job;occupied[job.object_key]=job.id
            self.suspended.pop(id_,None);self.last_object=job.object_key
            job.state='running';job.stage='preparing';job.reason=''
            job.execution_result='running';job.timings.setdefault('started',self.clock())
            if job.gate and not job.gate.done():job.gate.set_result(True)
            self.changed('started',job)
        if not self.pending:self.manual_order=False

    @asynccontextmanager
    async def parked(self,job,reason):
        """Release execution capacity during I/O, retain physical conversation order."""
        if not job or job.id not in self.active or job.state in TERMINAL:
            yield;return
        self.active.pop(job.id);self.suspended[job.id]=job
        job.state='suspended';job.stage='waiting';job.reason=reason
        started=self.clock();self.changed('suspended',job);self.pump()
        try:yield
        except asyncio.CancelledError:
            self.finish(job,'cancelled','cancelled_while_suspended')
            raise
        finally:
            job.timings['suspended_ms']=job.timings.get('suspended_ms',0)+(self.clock()-started)*1000
            if job.state not in TERMINAL:
                if self.closed:self.finish(job,'cancelled','service_shutdown')
                else:
                    job.gate=asyncio.get_running_loop().create_future();job.state='pending';self.pending.append(job.id)
                    self.changed('resume_wait',job);self.pump()
                    await self.wait(job)

    async def wait(self,job):
        if job.state in TERMINAL:raise ReplyCancelled(job.reason)
        try:await job.gate
        except asyncio.CancelledError:
            self.finish(job,'cancelled','caller_cancelled');raise
        if job.state!='running':raise ReplyCancelled(job.reason)

    def finish(self,job,state,reason='',*,pump=True):
        if job.state in TERMINAL:return
        assert state in TERMINAL
        self.reconcile_echo(job.id,state)
        passive_only=(state=='ignored' and job.state=='received' and not job.sends)
        occupied=job.id in self.active or job.id in self.pending or job.id in self.inflight
        if job.id in self.pending:self.pending.remove(job.id)
        self.active.pop(job.id,None)
        self.inflight.pop(job.id,None);self.suspended.pop(job.id,None)
        if job.execution_result not in {'failed','cancelled'}:
            job.execution_result='completed' if state in {'confirmed','partial'} else state
        job.state=state;job.stage='ended';job.reason=reason;job.timings['ended']=self.clock()
        if passive_only:
            # The journal/audit retain incoming traffic. It must not invalidate
            # queue edits, evict reply history or repeatedly scan all waiters.
            self.jobs.pop(job.id,None)
            return
        if job.gate and not job.gate.done():job.gate.set_result(False)
        if len(self.ended)==self.ended.maxlen:self.jobs.pop(self.ended[0],None)
        self.ended.append(job.id);self.changed('finished',job)
        if pump and occupied:self.pump()

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
            self.manual_order=True
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
            'echo_merged':sum(max(0,row['count']-1) for row in self.echoes.values()),
            'pending':[{**self.jobs[x].public(now),'position':i,'previous_id':self.pending[i-1] if i else None,
                        'after_next_id':self.pending[i+2] if i+2<len(self.pending) else None}
                       for i,x in enumerate(self.pending[offset:offset+limit],offset)],
            'running':[j.public(now) for j in self.active.values()],
            'suspended':[j.public(now) for j in self.suspended.values()],
            'inflight':len(self.inflight),'execution_limit':int(self.cfg('reply_queue_concurrency',4)),
            'recent':[self.jobs[x].public(now) for x in list(reversed(self.ended))[:limit]]}

    def close(self):
        self.closed=True
        for id_ in list(self.pending):self.finish(self.jobs[id_],'cancelled','service_shutdown')
        self.changed('closed')

    async def drain(self,timeout=8):
        self.close()
        owners={j.owner for j in self.inflight.values() if j.owner and j.owner is not asyncio.current_task()}
        if not owners:return
        _,pending=await asyncio.wait(owners,timeout=timeout)
        for task in pending:task.cancel()
        if pending:await asyncio.wait(pending,timeout=2)
