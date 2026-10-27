"""Delivery facts are receipt-driven. Unknown results are never retried here."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import json
import time
import uuid
from difflib import SequenceMatcher
from contextvars import ContextVar
from contextlib import contextmanager,asynccontextmanager
from .hook_lifecycle import HookLease
from .session_fence import get_fence,captured_session
from aiocqhttp import CQHttp, Message
from .record_store import AsyncRecordStore
from .reply_jobs import current_job
from .chain_metrics import stage as metric_stage,add as metric_add
from .route_guard import canonical,destination,SEND_ACTIONS

delivery_scope=ContextVar('qqbot_delivery_scope',default=None)
optional_delivery=ContextVar('qqbot_optional_delivery',default=False)

@contextmanager
def optional_attachment():
    token=optional_delivery.set(True)
    try:yield
    finally:optional_delivery.reset(token)


class DeliveryUnconfirmed(RuntimeError):
    def __init__(self,message,state='failed'):
        super().__init__(message);self.state=state


def receipt_status(result):
    if not isinstance(result,dict):return 'unknown',None
    if result.get('status')=='failed' or result.get('retcode',0) not in (0,1):return 'failed',None
    if result.get('status','ok')!='ok' or result.get('retcode',0)!=0:return 'unknown',None
    data=result.get('data',result)
    value=data.get('message_id') if isinstance(data,dict) else None
    if isinstance(value,bool) or value in (None,'',0,'0'):return 'unknown',None
    return 'confirmed',str(value)


def plain(parts):
    if isinstance(parts,str):parts=list(Message(parts))
    return '\n'.join(str(p.get('data',{}).get('text','')) for p in parts or [] if isinstance(p,dict) and p.get('type')=='text')


def digest(text):return hashlib.sha256(str(text).encode()).hexdigest()


class DeliveryLedger:
    def __init__(self,path):
        self.store=AsyncRecordStore(path);self.generations={};self.stats={'confirmed':0,'failed':0,'unknown':0}
        self.image_details=lambda attachment:attachment

    async def start(self):
        await self.store.start()
        cutoff=self.store.rows.get('__legacy_cutoff__')
        if cutoff is None:cutoff=self.store.put('__legacy_cutoff__',{'at':time.time()})
        self.legacy_cutoff=cutoff['at']
        for key,row in list(self.store.rows.items()):
            if row.get('state')=='sending':self.store.put(key,{**row,'state':'unknown','reason':'restart_during_send'})

    def scope(self,origin,cid):
        key='__origin__:'+digest(origin);metadata=self.store.rows.get(key,{})
        if cid is not None and not metadata.get('ever_bound'):
            for id_,row in list(self.store.rows.items()):
                if row.get('state')=='confirmed' and row.get('origin')==origin and row.get('cid') is None:
                    self.store.put(id_,{**row,'cid':cid,'adopted_first_conversation':True})
            self.store.put(key,{'ever_bound':True,'cid':cid})
        previous=self.generations.get(origin)
        if previous is None or previous[0]!=cid:
            self.generations[origin]=(cid,uuid.uuid4().hex)
        return self.generations[origin][1]

    def begin(self,origin,cid,text,attachments,job=None):
        # Reserve the derived circuit row before submission. Receipt completion
        # may happen while new-key capacity is exhausted; both updates must then
        # remain possible without turning an accepted message into an exception.
        target='__target__:'+digest(origin)
        if target not in self.store.rows:self.store.put(target,{'origin':origin,'consecutive_failures':0,'pause_until':0})
        id_=uuid.uuid4().hex
        row={'id':id_,'job_id':job.id if job else None,'origin':origin,'cid':cid,'source_id':job.source_id if job else '',
            'sender':job.sender if job else '', 'generation':job.generation if job else self.scope(origin,cid),
            'at':time.time(),'state':'sending','text':text,'attachments':attachments,'history_saved':False,
            'optional':bool(optional_delivery.get() or job and job.state in {'confirmed','partial'} and not text.strip()),'reason':''}
        self.store.put(id_,row)
        if job:job.sends.append(id_)
        return id_

    def result(self,id_,state,message_id=None,reason=''):
        row=self.store.rows[id_]
        self.store.put(id_,{**row,'state':state,'message_id':message_id,'reason':reason,'result_at':time.time(),'confirmed_at':time.time() if state=='confirmed' else None})
        if state in self.stats:self.stats[state]+=1
        attempt=delivery_scope.get()
        if attempt is not None:attempt.update(state=state,id=id_,message_id=message_id)
        if state=='confirmed' or state=='failed' and not reason.startswith(('outbox_','session_reset','hook_unloaded','pre_send_')):
            key='__target__:'+digest(row['origin']);previous=self.store.rows.get(key,{})
            failures=previous.get('consecutive_failures',0)+1 if state=='failed' else 0
            self.store.put(key,{**previous,'origin':row['origin'],'consecutive_failures':failures,
                'last_state':state,'last_result_at':time.time(),
                'pause_until':time.time()+1800 if failures>=3 else 0,
                'recovered_at':time.time() if state=='confirmed' and previous.get('consecutive_failures') else previous.get('recovered_at')})

    def health(self,now=None):
        now=time.time() if now is None else now
        records=[r for r in self.store.rows.values() if r.get('state') in {'confirmed','failed','unknown','sending'}]
        recent=[r for r in records if r.get('result_at',r.get('at',0))>=now-86400]
        waiting=[r for r in records if r.get('state')=='sending']
        targets=[dict(r) for k,r in self.store.rows.items() if k.startswith('__target__:')]
        return {'window_hours':24,'counts':{s:sum(r['state']==s for r in recent) for s in ('confirmed','failed','unknown')},
            'inflight':len(waiting),'oldest_pending_at':min((r['at'] for r in waiting),default=None),
            'targets':targets,'current_faults':[t for t in targets if t.get('consecutive_failures',0)>=3],
            'historical_failures':sum(r['state']=='failed' for r in records),
            'persistence':self.store.snapshot(),'last_confirmed_at':max((r.get('confirmed_at',0) or 0 for r in records if r['state']=='confirmed'),default=None)}

    def state(self,job):
        rows=[self.store.rows[x] for x in job.sends if x in self.store.rows and not self.store.rows[x].get('optional')]
        states={r['state'] for r in rows}
        if states=={'confirmed'}:return 'confirmed'
        if 'confirmed' in states:return 'partial'
        if states & {'unknown','sending'}:return 'unknown'
        if 'failed' in states:return 'failed'
        return 'ignored'

    def draft(self,job,text):
        key='draft:'+job.id
        self.store.put(key,{'id':key,'job_id':job.id,'origin':job.origin,'cid':job.cid,'source_id':job.source_id,
                           'at':time.time(),'state':'generated','text':text})

    def confirmed_text(self,job):
        return '\n'.join(self.store.rows[x]['text'] for x in job.sends if x in self.store.rows
                         and self.store.rows[x].get('state')=='confirmed' and self.store.rows[x].get('text'))

    def voice_history(self,job):
        rows=[self.store.rows[x] for x in job.sends if x in self.store.rows
              and self.store.rows[x].get('state')=='confirmed']
        if not any(a.get('kind')=='record' for row in rows for a in row.get('attachments',[])):return None
        parts=[]
        for row in rows:
            voice=any(a.get('kind')=='record' for a in row.get('attachments',[]))
            text=row.get('text','')
            if voice:parts.append('[发送形式：语音] '+(text or '[内容未记录]'))
            elif text:parts.append(text)
        return '\n'.join(parts)

    def voice_context(self,origin,cid):
        rows=[row for row in self.store.rows.values() if row.get('state')=='confirmed'
              and row.get('origin')==origin and row.get('cid')==cid
              and any(a.get('kind')=='record' for a in row.get('attachments',[]))]
        rows=sorted(rows,key=lambda row:row.get('at',0))[-2:]
        if not rows:return ''
        # Receipt-backed facts, not an assistant's self-authored label. Keep
        # these retrievable even after the general last-eight window moves on.
        return '\n系统依据平台成功回执确认的本会话语音记录（确实以语音发出，不是文字假扮；早先模型说不会发语音不能推翻回执事实）：'+json.dumps([
            {'时间':row['at'],'发送形式':'语音','原文':[a['transcript'] for a in row.get('attachments',[]) if a.get('kind')=='record' and a.get('transcript')],
             '未知内容不猜':not any(a.get('transcript') for a in row.get('attachments',[]) if a.get('kind')=='record')}
            for row in rows],ensure_ascii=False)

    def history_saved(self,job):
        for x in job.sends:
            row=self.store.rows.get(x,{})
            if row.get('state')=='confirmed' and row.get('text'):
                self.store.put(x,{**row,'history_saved':True})

    def recent(self,origin,cid,*,include_history=False):
        rows=[r for r in self.store.rows.values() if r.get('state')=='confirmed' and r.get('origin')==origin
              and r.get('cid')==cid and r.get('at',0)>time.time()-7*86400
              and (include_history or not r.get('history_saved') or r.get('attachments'))]
        return sorted(rows,key=lambda r:r['at'])[-8:]

    def note(self,origin,cid):
        self.scope(origin,cid)
        rows=self.recent(origin,cid)
        if not rows:return ''
        voice='；语音 transcript 是合成时的原文，无 transcript 的语音内容未知，不能拿生成稿补猜' if any(a.get('kind')=='record' for r in rows for a in r.get('attachments',[])) else ''
        return '\n当前会话平台已确认发送的记录（仅表示发送成功，不表示对方已读；图片用途推断不是图片原文'+voice+'）：\n'+json.dumps([
            {'消息编号':r['message_id'],'时间':r['at'],'文字':r['text'] if not r.get('history_saved') else '',
             **({'发送形式':'语音'} if any(a.get('kind')=='record' for a in r.get('attachments',[])) else {}),
             '附件':[self.image_details(a) for a in r.get('attachments',[])]} for r in rows],ensure_ascii=False)

    def blocked_hashes(self,origin,cid):
        return {r.get('text_hash') for r in self.store.rows.values()
                if r.get('state')=='legacy_undelivered' and r.get('origin')==origin and r.get('cid')==cid}

    def filter_history(self,origin,cid,messages,raw_history=None):
        blocked=[r for r in self.store.rows.values() if r.get('state')=='legacy_undelivered' and r.get('origin')==origin and r.get('cid')==cid]
        if not blocked:return messages
        if isinstance(raw_history,str):
            try:raw_history=json.loads(raw_history)
            except ValueError:raw_history=None
        original=raw_history if isinstance(raw_history,list) else messages
        row_hashes=[digest(json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':'))) for row in original]
        request_hashes=[digest(json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':'))) for row in messages]
        positions={j:i for block in SequenceMatcher(None,row_hashes,request_hashes,autojunk=False).get_matching_blocks()
                   for i,j in zip(range(block.a,block.a+block.size),range(block.b,block.b+block.size))}
        # Keep user records and tool calls/results; only hide proven unsent prose.
        result=[]
        for position,row in enumerate(messages):
            if row.get('role')=='assistant' and not row.get('tool_calls'):
                content=row.get('content','')
                text=content if isinstance(content,str) else '\n'.join(p.get('text','') for p in content or [] if isinstance(p,dict) and p.get('type')=='text')
                original_position=positions.get(position)
                previous=row_hashes[original_position-1] if original_position else None
                row_hash=digest(json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':')))
                if any(b.get('position')==original_position and b.get('row_hash')==row_hash and
                       b.get('text_hash')==digest(text) and b.get('previous_hash')==previous for b in blocked):continue
            result.append(row)
        return result

    async def close(self):await self.store.close()


def install_delivery_transport(plugin):
    previous=CQHttp.call_action;ledger=plugin._delivery
    lease=HookLease('delivery',plugin)
    restored=False
    fence=get_fence(getattr(plugin.context,'conversation_manager',None))
    if fence:fence.acquire()
    @asynccontextmanager
    async def boundary(origin):
        if fence:
            async with fence.boundary(origin):yield
        else:yield
    async def send(bot,action,**params):
        if not lease.active:return await previous(bot,action,**params)
        platform=plugin._platform();base=action.removesuffix('_async')
        if platform is None or getattr(platform,'bot',None) is not bot or base not in SEND_ACTIONS:
            return await previous(bot,action,**params)
        origin=destination(action,params,plugin._cfg('platform_id','aiocqhttp'));job=current_job.get()
        if job and canonical(job.origin)==canonical(origin):origin=job.origin
        else:job=None
        manager=getattr(plugin.context,'conversation_manager',None)
        async with asyncio.timeout(3):
            cid=await manager.get_curr_conversation_id(origin) if manager else None
        epoch=fence.epoch(origin) if fence else 0
        captured=captured_session.get()
        expected=(job.cid,job.session_epoch) if job else ((captured[1],captured[2]) if captured and captured[0]==origin else (cid,epoch))
        if job and job.cid is not None and cid!=job.cid:
            raise DeliveryUnconfirmed('会话已重置，已取消旧会话附件/回复')
        if job:
            job.cid=cid
            if not job.generation:job.generation=ledger.scope(origin,cid)
        parts=params.get('message',params.get('messages',[]))
        if isinstance(parts,str):parts=([{'type':'text','data':{'text':parts}}] if params.get('auto_escape') else list(Message(parts)))
        def flatten(rows,depth=0):
            if depth>5:return []
            output=[]
            for part in rows[:200] if isinstance(rows,list) else []:
                if isinstance(part,dict) and part.get('type')=='node':output.extend(flatten(part.get('data',{}).get('content',[]),depth+1))
                else:output.append(part)
            return output
        parts=flatten(parts)
        from .voice_memory import record_transcript,spoken_text
        attachments=[]
        images=getattr(plugin,'_image_memory',None)
        for part in parts:
            if isinstance(part,dict) and part.get('type') in {'image','video','file','record'}:
                if images:
                    try:
                        async with asyncio.timeout(2):attachments.append(await images.describe_transport(part))
                    except Exception:attachments.append({'kind':part['type'],'semantic_status':'unknown'})
                else:attachments.append({'kind':part['type'],'semantic_status':'unknown'})
                transcript=record_transcript(part,bot,origin)
                if transcript:
                    attachments[-1].update(transcript=transcript,transcript_source='tts_input',semantic_status='known')
        if not lease.active:return await previous(bot,action,**params)
        text=spoken_text(parts,bot,origin);id_=ledger.begin(origin,cid,text,attachments,job)
        attempt=delivery_scope.get()
        if attempt is not None:attempt.update(state='preparing',id=id_)
        # A failed durability barrier prevents sending; later writes only retry storage.
        persisted=time.monotonic()
        try:await ledger.store.flush(id_)
        except Exception:
            if lease.active and not ledger.store.closed:ledger.result(id_,'failed',reason='outbox_persistence_unavailable')
            raise
        if job:job.timings['persistence_ms']=job.timings.get('persistence_ms',0)+(time.monotonic()-persisted)*1000
        started=time.monotonic();metric_stage('sending');submitted=False
        try:
            async with boundary(origin):
                if not lease.active or ledger.store.closed:return await previous(bot,action,**params)
                latest=await manager.get_curr_conversation_id(origin) if manager else None
                if not lease.active or ledger.store.closed:return await previous(bot,action,**params)
                if ((expected[0] is not None and latest!=expected[0]) or latest!=cid
                    or fence and expected[1] is not None and fence.epoch(origin)!=expected[1]):
                    ledger.result(id_,'failed',reason='session_reset_before_send')
                    raise DeliveryUnconfirmed('会话已重置，已取消旧会话附件/回复')
                from .conversation_review import _delivery_check
                final_check=_delivery_check.get()
                if final_check:await final_check()
                submitted=True
                if attempt is not None:attempt.update(state='submitted')
                result=await previous(bot,action,**params)
        except BaseException as exc:
            if not lease.active or ledger.store.closed:raise
            if ledger.store.rows[id_].get('state')=='failed':raise
            explicit=not submitted or isinstance(getattr(exc,'retcode',None),int) or type(exc).__name__=='ActionFailed'
            ledger.result(id_,'failed' if explicit else 'unknown',reason=('pre_send_' if not submitted else '')+type(exc).__name__)
            if job:job.timings['send_ms']=job.timings.get('send_ms',0)+(time.monotonic()-started)*1000
            raise
        if not lease.active or ledger.store.closed:return result
        state,message_id=receipt_status(result);ledger.result(id_,state,message_id)
        record=ledger.store.rows[id_]
        ledger.store.put(id_,{**record,'send_ms':(time.monotonic()-started)*1000,
                              'elapsed_ms':(time.monotonic()-job.received)*1000 if job else None})
        profiles=getattr(plugin,'_ai_profiles',None)
        if profiles and job and job.event:profiles.delivery(job.event.get_extra('qqbot_ai_probe'),text,state,id_,message_id)
        if job:job.timings['send_ms']=job.timings.get('send_ms',0)+(time.monotonic()-started)*1000
        if state!='confirmed':raise DeliveryUnconfirmed('平台未确认发送：'+state,state)
        life=getattr(plugin,'_life',None)
        if life:
            try:life.confirmed(ledger.store.rows[id_])
            except Exception as exc:plugin.logger.error('已发送承诺的后台提取待恢复：%s',type(exc).__name__)
        if job:
            label='optional_attachment_complete_ms' if record.get('optional') else 'required_complete_ms'
            job.timings[label]=(time.monotonic()-job.received)*1000
        # Memory becomes visible immediately, durable writer retries independently.
        if images:
            try:images.confirmed(ledger.store.rows[id_])
            except Exception as exc:
                plugin.logger.error('图片已确认发送，语义记忆待修复：%s',type(exc).__name__)
        return result
    CQHttp.call_action=send
    def restore():
        nonlocal restored
        if restored:return
        restored=True
        lease.close()
        if fence:fence.release()
        if CQHttp.call_action is send:CQHttp.call_action=previous
    return restore


def install_delivery_history(plugin):
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
    previous=InternalAgentSubStage._save_to_history
    lease=HookLease('delivery_history',plugin)
    async def save(stage,event,req,response,messages,stats,user_aborted=False):
        if not lease.active:return await previous(stage,event,req,response,messages,stats,user_aborted)
        started=time.monotonic()
        job=event.get_extra('qqbot_reply_job')
        if not job:return await previous(stage,event,req,response,messages,stats,user_aborted)
        ledger=plugin._delivery;draft=getattr(response,'completion_text','') or ''
        if draft:ledger.draft(job,draft)
        confirmed=ledger.confirmed_text(job)
        voice_history=ledger.voice_history(job) if draft else None
        copies=list(messages)
        if draft and (voice_history or not confirmed or digest(confirmed)!=digest(draft)):
            # Leave all actual tool actions and user messages in raw history.
            for i in range(len(copies)-1,-1,-1):
                message=copies[i]
                if message.role=='user':break
                if message.role=='assistant' and not getattr(message,'tool_calls',None):
                    message=copy.copy(message);message._no_save=True;copies[i]=message
        if voice_history:
            # Persist modality with the archive, not only the last-eight receipt
            # note. Never alter the live output or reinsert unconfirmed drafts.
            from astrbot.core.agent.message import Message as AgentMessage,TextPart
            copies.append(AgentMessage(role='assistant',content=[TextPart(text=voice_history)]))
        result=await previous(stage,event,req,response,copies,stats,user_aborted)
        metric_add('history_save_ms',(time.monotonic()-started)*1000)
        if lease.active and draft and req and req.conversation and (voice_history or confirmed and digest(confirmed)==digest(draft)):
            ledger.history_saved(job)
        return result
    InternalAgentSubStage._save_to_history=save
    def restore():
        lease.close()
        if InternalAgentSubStage._save_to_history is save:InternalAgentSubStage._save_to_history=previous
    return restore
