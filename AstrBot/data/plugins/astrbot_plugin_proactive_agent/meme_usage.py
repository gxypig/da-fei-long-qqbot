"""Receipt-driven joke opportunities and an auditable, idle-only semantic check."""
import asyncio,copy,hashlib,random,time,uuid
from .record_store import AsyncRecordStore
from .meme_index import contains,fold,SERIOUS,strings

USAGE_RULE='''\n【本轮梗语境】候选是理解材料，不是必须使用的任务。先理解态度和指向，适合才自然接住或改编；每轮最多采用一个。直接抛梗通常接话，不展开词典解释；明确询问含义时可解释。严肃求助、难过、停止玩笑时不强行玩梗。没有合适候选就正常回答。不要对用户提候选、概率或计数。'''
ASSESS_SYSTEM='''你在后台核对已经发送的聊天是否自然使用了给定梗。不是见到关键词就算使用，也不要修改已发送事实。结合用户话语、含义、用法和禁止场景判断；自然改编允许不含原词。只返回JSON：{"uses":[{"phrase":"候选原名","evidence":"从已发送原文逐字摘取的证据","fit":true,"reason":"语境和指向为什么成立"}]}。没有使用则空列表；fit为false表示确实用了但语境不合适。不得虚构证据。'''

def key(origin):return '__session__:'+hashlib.sha256(str(origin).encode()).hexdigest()

class MemeUsage:
    def __init__(self,path,generate=None,busy=lambda:False,rng=None):
        self.store=AsyncRecordStore(path);self.generate=generate;self.busy=busy;self.rng=rng or random.random
        self.task=None;self.closed=False;self.ledger=None

    async def start(self,ledger=None):
        self.ledger=ledger;await self.store.start()
        # The outbox is authoritative if a process died after the platform receipt.
        if ledger:
            async for row in ledger.store.iterate(states={'confirmed'},sort='confirmed_at'):
                if row.get('state')=='confirmed':self.delivery(row)
        self.task=asyncio.create_task(self._worker(),name='meme-usage-evidence')

    def choose(self,event,rows,text):
        previous=event.get_extra('qqbot_meme_plan')
        if previous:return copy.deepcopy(previous.get('candidates',[]))
        origin=event.unified_msg_origin;state=self.store.rows.get(key(origin),{})
        candidates=[];reason='no_relevant_candidate';mode='none'
        character=event.get_extra('qqbot_character') or {}
        if character.get('mood') in {'serious','stop'} or SERIOUS.search(text):reason='serious_or_stop'
        elif rows:
            direct=[r for r in rows if r.get('retrieval_kind')=='direct']
            if direct:candidates=direct[:3];mode='direct';reason='direct_reply'
            else:
                recent=state.get('replies',[])[-20:];sequence=state.get('sequence',0)
                used={fold(p) for r in recent for p in r.get('uses',[])}
                # Pending inference reserves only a possibility, never counts it as use.
                reserved={fold(p) for r in recent if r.get('assessment')=='pending' for p in r.get('candidates',[])}
                last=max([r.get('sequence',0) for r in recent if r.get('active_use') or r.get('active_pending')]+[-100])
                eligible=[r for r in rows if fold(r.get('phrase')) not in used|reserved]
                if sequence-last<3:reason='confirmed_reply_gap'
                elif not eligible:reason='recent_twenty_repeat'
                elif self.rng()>=.2:reason='natural_opportunity_not_drawn'
                else:candidates=eligible[:3];mode='active';reason='natural_opportunity'
        job=event.get_extra('qqbot_reply_job')
        plan={'id':uuid.uuid4().hex,'origin':origin,'job_id':getattr(job,'id',None),'at':time.time(),
              'input':str(text)[:1800],'mode':mode,'reason':reason,'candidates':copy.deepcopy(candidates),
              'retrieved':copy.deepcopy(rows[:3]),'stages':[{'stage':'retrieved','at':time.time(),'count':len(rows)},
                {'stage':'candidate','at':time.time(),'count':len(candidates),'reason':reason}]}
        self.store.put(plan['id'],plan);event.set_extra('qqbot_meme_plan',plan)
        return copy.deepcopy(candidates)

    def stage(self,event,stage,text='',reason=''):
        plan=event.get_extra('qqbot_meme_plan')
        if not plan:return
        row=self.store.rows.get(plan['id'],plan)
        changes={'stages':(row.get('stages',[])+[{'stage':stage,'at':time.time(),'text':str(text)[:3000],'reason':reason}])[-24:]}
        if stage in {'draft','review'}:changes[stage]=str(text)[:6000]
        self.store.put(plan['id'],{**row,**changes})

    def delivery(self,record):
        if record.get('state')!='confirmed' or record.get('optional') or record.get('purpose')=='error_notification':return
        origin=record.get('origin');rid=record.get('id');reply=record.get('job_id') or rid
        if not origin or not rid:return
        # One logical reply, regardless of segmentation, text/audio combination or retries.
        replykey='reply:'+str(reply);old=self.store.rows.get(replykey,{})
        if rid in old.get('deliveries',[]):return
        session=self.store.rows.get(key(origin),{});sequence=old.get('sequence') or session.get('sequence',0)+1
        plan=record.get('meme_plan') or {};pid=plan.get('id');trace=self.store.rows.get(pid,plan) if pid else {}
        text='\n'.join(x for x in (old.get('text',''),record.get('text','')) if x)
        candidates=trace.get('candidates',[]) or old.get('candidate_rows',[])
        literals=[r['phrase'] for r in candidates if any(contains(text,a) for a in [r['phrase']]+strings(r.get('aliases')))]
        row={**old,'id':replykey,'origin':origin,'at':record.get('confirmed_at') or time.time(),'sequence':sequence,
             'text':text,'plan_id':pid or old.get('plan_id'),'input':trace.get('input',old.get('input','')),
             'deliveries':old.get('deliveries',[])+[rid],'candidate_rows':candidates,
             'candidates':[r['phrase'] for r in candidates],'mode':trace.get('mode',old.get('mode','none')),
             'literal_matches':literals,'uses':old.get('uses',[]),'assessment':'pending' if candidates and text else 'not_needed',
             'attempts':0,'retry_at':0,'active_pending':bool(candidates and text and trace.get('mode')=='active')}
        self.store.put(replykey,row);self._session(origin,row,sequence)
        if pid:
            self.store.put(pid,{**trace,'confirmed_reply':replykey,'stages':(trace.get('stages',[])+[
                {'stage':'confirmed','at':row['at'],'delivery_id':rid,'text':record.get('text',''),'literal_matches':literals,
                 'note':'字面命中不代表自然采用；语义核对在空闲时完成'}])[-24:]})

    def _session(self,origin,row,sequence):
        session=self.store.rows.get(key(origin),{});replies=[r for r in session.get('replies',[]) if r['id']!=row['id']]
        replies.append({k:row.get(k) for k in ('id','sequence','uses','assessment','candidates','active_use','active_pending')})
        replies.sort(key=lambda r:r['sequence'])
        self.store.put(key(origin),{**session,'origin':origin,'sequence':max(sequence,session.get('sequence',0)),'replies':replies[-20:]})

    def finished(self,event,reason):
        plan=event.get_extra('qqbot_meme_plan')
        if plan and not self.store.rows.get(plan['id'],{}).get('confirmed_reply'):
            self.stage(event,'abandoned',reason=reason)

    async def assess(self,row):
        from .work_scheduler import model_work
        with model_work('background'):
            result=await self.generate(ASSESS_SYSTEM,{'input':row['input'],'sent':row['text'],'candidates':row['candidate_rows']},row['origin'])
        if isinstance(result,str):
            from .conversation_review import json_object
            result=json_object(result)
        # A later segment may arrive while inference was in flight. Re-evaluate the complete reply.
        latest=self.store.rows[row['id']]
        if latest.get('text')!=row['text']:return
        if not isinstance(result,dict) or not isinstance(result.get('uses'),list):raise ValueError('invalid usage assessment')
        names={r['phrase'] for r in row['candidate_rows']};uses=[]
        for item in result['uses']:
            if not isinstance(item,dict) or item.get('phrase') not in names or not isinstance(item.get('fit'),bool):raise ValueError('invalid evidence')
            evidence=str(item.get('evidence',''))
            if not evidence or evidence not in row['text']:raise ValueError('evidence absent from confirmed text')
            uses.append({**item,'mode':'literal' if item['phrase'] in row.get('literal_matches',[]) else 'inferred'})
        value={**latest,'uses':list(dict.fromkeys(u['phrase'] for u in uses)),'evidence':uses,'assessment':'checked',
               'active_pending':False,'active_use':bool(uses and row['mode']=='active'),'assessed_at':time.time()}
        self.store.put(row['id'],value);self._session(row['origin'],value,row['sequence'])

    async def _worker(self):
        while True:
            await asyncio.sleep(2)
            if self.busy() or not self.generate:continue
            row=next((r for r in self.store.rows.values() if r.get('assessment')=='pending' and r.get('retry_at',0)<=time.time()),None)
            if not row:continue
            try:await self.assess(copy.deepcopy(row))
            except asyncio.CancelledError:raise
            except Exception as exc:
                if type(exc).__name__=='BackgroundPaused':continue
                latest=self.store.rows.get(row['id'],row);attempt=latest.get('attempts',0)+1
                self.store.put(row['id'],{**latest,'attempts':attempt,'retry_at':time.time()+min(3600,15*2**min(attempt,8)),
                    'assessment':'failed' if attempt>=5 else 'pending','error':type(exc).__name__})

    def page(self,before=None,limit=30):
        rows=[r for r in self.store.rows.values() if r.get('stages')]
        rows.sort(key=lambda r:(r['at'],r['id']),reverse=True)
        if before:rows=[r for r in rows if (r['at'],r['id'])<tuple(before)]
        page=rows[:limit]
        return {'rows':[{**r,'actual':self.store.rows.get(r.get('confirmed_reply'),{})} for r in page],
                'next':[page[-1]['at'],page[-1]['id']] if len(rows)>limit else None,
                'capabilities':{'usage_trace':True,'receipt_based':True,'semantic_inference':'idle'}}

    async def close(self):
        if self.task and not self.task.done():self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        await self.store.close();self.closed=True
