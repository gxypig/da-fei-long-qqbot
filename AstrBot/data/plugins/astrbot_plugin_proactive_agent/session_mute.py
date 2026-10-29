"""Durable physical-recipient silence, independent of wake/review probabilities."""
import asyncio,copy,hashlib,json,os,time,uuid
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
from .route_guard import canonical
from .reply_jobs import ReplyCancelled,TERMINAL

_pinned=ContextVar('qqbot_mute_scope',default=None)
_active=None

class MuteConflict(ValueError):pass
class SessionMuted(ReplyCancelled):pass

def object_id(origin):
    platform,kind,target=canonical(origin)
    if not platform or kind not in {'GroupMessage','FriendMessage'} or not target.isdigit():raise ValueError('无效群聊或私聊对象')
    return ':'.join((platform,kind,target))

def get_mutes():return _active

@contextmanager
def pin_recipient(origin):
    service=_active
    if service is None or origin=='qzone':yield;return
    key=object_id(origin);epoch=service.capture(origin)
    token=_pinned.set({**(_pinned.get() or {}),key:epoch})
    try:yield
    finally:_pinned.reset(token)

def pinned_epoch(origin):
    try:return (_pinned.get() or {}).get(object_id(origin))
    except ValueError:return None

def recipient_task(resolve):
    def decorate(function):
        @wraps(function)
        async def run(*args,**kwargs):
            if kwargs.get('dry_run'):return await function(*args,**kwargs)
            origin=resolve(*args,**kwargs)
            with pin_recipient(origin):return await function(*args,**kwargs)
        return run
    return decorate

class SessionMutes:
    def __init__(self,path,queue=None,ledger=None,clock=time.time):
        self.path=__import__('pathlib').Path(path);self.queue=queue;self.ledger=ledger;self.clock=clock
        self.data={'rules':{},'actions':{}};self.lock=asyncio.Lock();self.transitioning=set();self.fail_closed=set();self.volatile={}
        self.instance=uuid.uuid4().hex;self.task=None;self.closed=False

    async def _write(self,data):
        payload=json.dumps(data,ensure_ascii=False).encode()
        def write():
            self.path.parent.mkdir(parents=True,exist_ok=True);tmp=self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
            try:
                with tmp.open('wb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
                os.replace(tmp,self.path)
            finally:tmp.unlink(missing_ok=True)
        task=asyncio.create_task(asyncio.to_thread(write))
        # Finish the durable operation even if the HTTP client disconnects.
        try:await asyncio.shield(task)
        except asyncio.CancelledError:await task

    async def start(self,*,activate=True):
        global _active
        try:self.data=json.loads(await asyncio.to_thread(self.path.read_text,'utf-8'))
        except FileNotFoundError:pass
        if not isinstance(self.data.get('rules'),dict) or not isinstance(self.data.get('actions'),dict):raise ValueError('静默文件损坏，未启用发送')
        for key,row in self.data['rules'].items():
            if object_id(key)!=key or type(row.get('revision')) is not int or type(row.get('muted')) is not bool:raise ValueError('静默规则损坏，未启用发送')
        # Expiry is itself a durable new epoch, never a read-side implicit unmute.
        await self.expire()
        if activate:_active=self
        self.task=asyncio.create_task(self._worker(),name='recipient-mute-expiry')

    def status(self,origin):
        key=object_id(origin);row=self.data['rules'].get(key,{'revision':0,'muted':False,'until':None})
        blocked=key in self.transitioning or key in self.fail_closed
        return {**row,'origin':key,'muted':row['muted'] or blocked,'durable':not blocked,
            'reason':'静默修改尚未可靠保存，暂停发送' if blocked else '会话已静默' if row['muted'] else '',
            'expired_waiting':bool(row['muted'] and row.get('until') and row['until']<=self.clock())}

    def epoch(self,origin):
        key=object_id(origin)
        return (self.instance,self.data['rules'].get(key,{}).get('revision',0),self.volatile.get(key,0))

    def check(self,origin,expected=None):
        if origin=='qzone':return
        if self.status(origin)['muted']:raise SessionMuted('会话已静默，停止发送')
        if expected is not None and tuple(expected)!=self.epoch(origin):raise SessionMuted('静默状态已改变，旧任务不会恢复发送')

    def capture(self,origin):
        self.check(origin);return self.epoch(origin)

    def cancel_old(self,key):
        cancelled=0;submitted=0
        ledger=self.ledger
        if ledger:
            submitted=sum(1 for r in ledger.store.index.values() if r.get('origin') and _same(r['origin'],key)
                and r.get('submission_state')=='submitted' and r.get('state') in {'sending','unknown'})
        if self.queue:
            for job in list(self.queue.jobs.values()):
                if job.state in TERMINAL or not _same(job.origin,key):continue
                cancelled+=1;self.queue.finish(job,'cancelled','会话静默变更，取消旧回复')
                if job.owner and job.owner is not asyncio.current_task() and not job.owner.done():job.owner.cancel()
        return cancelled,submitted

    async def change(self,origin,muted,until,revision,request_id):
        key=object_id(origin)
        if type(muted) is not bool or type(revision) is not int or revision<0:raise ValueError('静默状态或版本错误')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=128:raise ValueError('缺少幂等操作编号')
        if until is not None and (type(until) not in (float,int) or not self.clock()<until<=self.clock()+366*86400):raise ValueError('截止时间须在未来一年内')
        if not muted:until=None
        signature=hashlib.sha256(json.dumps([key,muted,until,revision],sort_keys=True).encode()).hexdigest()
        async with self.lock:
            previous=self.data['actions'].get(request_id)
            if previous:
                if previous['signature']!=signature:raise MuteConflict('操作编号已用于另一项修改')
                return copy.deepcopy(previous['result'])
            current=self.data['rules'].get(key,{'revision':0})
            if current['revision']!=revision:raise MuteConflict('该会话静默状态已改变，请刷新后重试')
            self.transitioning.add(key);self.volatile[key]=self.volatile.get(key,0)+1
            cancelled,submitted=self.cancel_old(key)
            row={'origin':key,'revision':revision+1,'muted':muted,'until':until,'changed_at':self.clock()}
            result={**row,'cancelled':cancelled,'submitted_waiting_receipts':submitted,'durable':True,'progress':'已保存；等待已提交消息回执' if submitted else '已保存并生效'}
            updated=copy.deepcopy(self.data);updated['rules'][key]=row
            updated['actions'][request_id]={'signature':signature,'result':result}
            if len(updated['actions'])>2000:updated['actions']=dict(list(updated['actions'].items())[-2000:])
            try:await self._write(updated)
            except BaseException:
                self.fail_closed.add(key);raise
            else:self.data=updated;self.fail_closed.discard(key)
            finally:self.transitioning.discard(key)
            return copy.deepcopy(result)

    async def expire(self):
        for key,row in list(self.data['rules'].items()):
            if row['muted'] and row.get('until') and row['until']<=self.clock():
                try:await self.change(key,False,None,row['revision'],'expiry:'+hashlib.sha256((key+str(row['revision'])).encode()).hexdigest())
                except MuteConflict:pass

    async def _worker(self):
        while not self.closed:
            try:await self.expire()
            except OSError:pass  # fail-closed state remains visible, retry after delay
            await asyncio.sleep(1)

    async def close(self):
        global _active
        self.closed=True
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        if _active is self:_active=None

def _same(left,right):
    try:return object_id(left)==object_id(right)
    except ValueError:return False
