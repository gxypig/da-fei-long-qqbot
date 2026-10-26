"""Conservative, reversible account observations; never changes reply eligibility.

Evidence is private. Fast messages, prose style and one self-claim cannot certify
an account. Only two answered, actually sent probes plus sustained independent
automation evidence can produce the explicitly fallible automatic judgement.
"""
import asyncio
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
import time
import uuid
from .record_store import AsyncRecordStore
from .reply_jobs import QueueConflict,error_fingerprint

QUESTIONS=('你这消息是自己打的，还是程序自动回的呀？',
           '确认一下，你这个号现在是人手动回，还是机器人在自动回？',
           '你刚刚说的自动回复，是这个账号实际在用程序回消息的意思吗？')
AUTO=re.compile(r'(?:我(?:这边|这个号|的回复)?(?:是|由|确实是|用的是).{0,12}(?:AI|人工智能|自动回复程序|机器人|程序自动)|(?:这个|本)账号.{0,12}(?:机器人|程序自动)|(?:消息|回复).{0,8}(?:由程序|是自动生成)|\bI(?: am|\x27m) (?:an? )?(?:AI|bot|automated)|this account is automated)',re.I)
HUMAN=re.compile(r'不是.{0,5}(?:AI|机器人|自动)|我.{0,5}(?:真人|手动|自己打)|人工回复|I am (?:a )?human',re.I)
UNRELIABLE=re.compile(r'假装|扮演|角色|复读|引用|例如|比如|他说|她说|你说|>\s|```|[“”「」]|假如|如果我是|pretend|roleplay|quot(?:e|ing)',re.I)


def identity(platform,account):
    if not isinstance(platform,str) or not re.fullmatch(r'[\w.-]{1,80}',platform):raise ValueError('平台账号格式错误')
    if not isinstance(account,str) or not re.fullmatch(r'[\w.-]{1,100}',account):raise ValueError('平台账号格式错误')
    return platform+':'+account


class AIProfiles:
    def __init__(self,path,cfg,busy=lambda:False,clock=time.time):
        self.store=AsyncRecordStore(path);self.cfg=cfg;self.busy=busy;self.clock=clock
        self.instance=uuid.uuid4().hex;self.pending=OrderedDict();self.wake=asyncio.Event();self.task=None
        self.version=0;self.default_days=7;self.actions=OrderedDict();self.closed=False

    async def start(self):
        await self.store.start();control=self.store.rows.get('__control__',{})
        self.version=control.get('version',0);self.default_days=control.get('default_days',7)
        for key,row in list(self.store.rows.items()):
            if row.get('outstanding') and not row['outstanding'].get('sent'):
                self._put(key,{**row,'outstanding':None})
        self.task=asyncio.create_task(self._worker(),name='account-evidence-idle')

    def _control(self):
        self.version+=1
        self.store.put('__control__',{'version':self.version,'default_days':self.default_days})

    def _put(self,key,row):
        self.store.put(key,row);self._control()

    def _empty(self,platform,account):
        return {'platform':platform,'account':account,'epoch':0,'state':'unknown','source':'',
                'expires_at':None,'observations':[],'evidence':[],'probes':{},'outstanding':None,
                'created_at':self.clock(),'updated_at':self.clock()}

    def effective(self,row):
        if row.get('state') in {'auto_confirmed','manual_confirmed'} and (row.get('expires_at') or 0)<=self.clock():return 'expired'
        return row.get('state','unknown')

    def _fresh(self,key,row):
        row=dict(row)
        if self.effective(row)=='expired' and row['state']!='expired':
            row.update(state='expired',epoch=row['epoch']+1,outstanding=None,evidence=[])
        return row

    def observe(self,platform,account,text,origin,source_id,*,at=None):
        if not self.cfg('ai_profiles_enabled',False) or self.closed:return
        key=identity(platform,account);row=self._fresh(key,self.store.rows.get(key,self._empty(platform,account)))
        if any(x['source_id']==str(source_id) and x['origin']==origin for x in row['observations']):return
        now=self.clock() if at is None else at
        previous=row['observations'][-1] if row['observations'] else None
        outstanding=row.get('outstanding');probe=None
        if outstanding and outstanding.get('sent') and now>=outstanding['sent_at'] and str(source_id)!=outstanding['trigger_id']:
            probe=outstanding['id'];row['outstanding']=None
        obs={'id':uuid.uuid4().hex,'epoch':row['epoch'],'source_id':str(source_id),'origin':origin,
             'text':str(text)[:2500],'at':now,'probe':probe,
             'response_interval':max(0,now-outstanding['sent_at']) if probe else None,
             'inter_message_interval':max(0,now-previous['at']) if previous else None}
        row['observations']=[*row['observations'][-39:],obs];row['updated_at']=now
        if row['state'] in {'unknown','expired','revoked'}:row['state']='observing'
        self._put(key,row)
        self.pending[key]=row['epoch'];self.pending.move_to_end(key)
        while len(self.pending)>1000:self.pending.popitem(last=False)
        self.wake.set()

    def assess(self,key,epoch):
        original=self.store.rows.get(key)
        if not original or original['epoch']!=epoch or self.effective(original) in {'manual_confirmed','auto_confirmed','expired'}:return
        row=dict(original);observations=[x for x in row['observations'] if x['epoch']==epoch]
        explicit=[];contradictions=[];errors={}
        for obs in observations:
            text=obs['text']
            if HUMAN.search(text) and not UNRELIABLE.search(text):contradictions.append(obs)
            if obs.get('probe') and AUTO.search(text) and not HUMAN.search(text) and not UNRELIABLE.search(text):explicit.append(obs)
            fingerprint=error_fingerprint(text)
            if fingerprint:errors.setdefault(fingerprint,[]).append(obs)
        sustained=[xs for xs in errors.values() if len(xs)>=5 and xs[-1]['at']-xs[0]['at']>=60]
        evidence=[]
        for obs in explicit:
            evidence.append({'kind':'answered_probe','observation':obs['id'],'probe':obs['probe'],'at':obs['at'],
                             'summary':'在已发送身份问题之后明确自述为自动账号'})
        if sustained:evidence.append({'kind':'sustained_machine_errors','count':sum(len(xs) for xs in sustained),
                                      'summary':'跨多条消息持续重复相同结构的接口错误；单独不足以认定身份'})
        if contradictions:evidence.append({'kind':'contradiction','summary':'存在相反的人类/手动操作自述，阻止自动确认'})
        row['evidence']=evidence
        if len({x['probe'] for x in explicit})>=2 and sustained and not contradictions:
            row.update(state='auto_confirmed',source='automatic_high_confidence',expires_at=self.clock()+self.default_days*86400)
        elif explicit or sustained or any(AUTO.search(x['text']) and not UNRELIABLE.search(x['text']) for x in observations):row['state']='suspected'
        else:row['state']='observing'
        self._put(key,row)

    async def _worker(self):
        while True:
            await self.wake.wait();self.wake.clear()
            while self.pending:
                if self.busy():await asyncio.sleep(1);continue
                key,epoch=self.pending.popitem(last=False)
                self.assess(key,epoch)
                await asyncio.sleep(0)

    def day(self):
        return datetime.fromtimestamp(self.clock(),timezone(timedelta(hours=8))).date().isoformat()

    async def prepare_probe(self,platform,account,source_id,origin):
        if not self.cfg('ai_profiles_enabled',False) or not self.cfg('ai_probe_enabled',False):return None
        key=identity(platform,account);original=self.store.rows.get(key)
        if not original:return None
        row=self._fresh(key,original)
        if self.effective(row)!='suspected' or row.get('outstanding'):return None
        day=self.day();count=row.get('probes',{}).get(day,0)
        if count>=3:return None
        probe={'id':uuid.uuid4().hex,'epoch':row['epoch'],'trigger_id':str(source_id),'origin':origin,
               'question':QUESTIONS[count],'at':self.clock(),'sent':False,'key':key}
        # Conservatively occupy an attempt before generation. A crash, unknown
        # send or a rewritten-away question cannot reset the daily upper bound.
        row['probes']={k:v for k,v in row['probes'].items() if k>=str(datetime.fromtimestamp(self.clock()-8*86400,timezone(timedelta(hours=8))).date())}
        row['probes'][day]=count+1;row['outstanding']=probe;self._put(key,row)
        try:await self.store.flush(key,timeout=2)
        except (TimeoutError,OSError):
            self._put(key,{**row,'outstanding':None});return None
        return dict(probe)

    def delivery(self,probe,text,state):
        if not probe:return
        row=self.store.rows.get(probe['key'])
        if not row or row['epoch']!=probe['epoch'] or (row.get('outstanding') or {}).get('id')!=probe['id']:return
        row=dict(row)
        if probe['question'].rstrip('？?') in text and state in {'confirmed','unknown'}:
            row['outstanding']={**row['outstanding'],'sent':state=='confirmed','sent_at':self.clock(),'result':state}
        else:row['outstanding']=None
        self._put(probe['key'],row)

    def end_turn(self,probe):
        if not probe:return
        row=self.store.rows.get(probe['key'])
        outstanding=row.get('outstanding') if row else None
        if outstanding and outstanding['id']==probe['id'] and not outstanding.get('sent'):
            self._put(probe['key'],{**row,'outstanding':None})

    def reply_note(self,platform,account):
        if not self.cfg('ai_profiles_enabled',False):return ''
        row=self.store.rows.get(identity(platform,account),{})
        if self.effective(row) not in {'auto_confirmed','manual_confirmed'}:return ''
        return '\n当前发言账号在可撤销的AI账号认证有效期内。确实需要称呼对方时，可自然叫一声“弱鸡ai”，不要每句追加或围绕身份纠缠；仍正常接话，不向其他人披露识别证据。'

    def source_label(self,platform,account):
        row=self.store.rows.get(identity(platform,account),{})
        state=self.effective(row)
        return 'automatic_confirmed' if state in {'auto_confirmed','manual_confirmed'} else 'automatic_suspected' if state=='suspected' else 'unverified'

    def snapshot(self,query='',state='',offset=0):
        rows=[]
        for key,row in self.store.rows.items():
            if 'account' not in row:continue
            effective=self.effective(row)
            if state and effective!=state:continue
            if query and query.casefold() not in key.casefold():continue
            rows.append({'key':key,'platform':row['platform'],'account':row['account'],'state':effective,
                'source':row.get('source',''),'expires_at':row.get('expires_at'),
                'evidence':row.get('evidence',[]) if effective not in {'expired','revoked'} else [],
                'today_probes':row.get('probes',{}).get(self.day(),0),'outstanding':bool(row.get('outstanding')),
                'updated_at':row['updated_at']})
        rows.sort(key=lambda r:r['updated_at'],reverse=True);offset=max(0,offset)
        return {'instance':self.instance,'version':self.version,'default_days':self.default_days,
                'rows':rows[offset:offset+50],'total':len(rows),'offset':offset,'day':self.day(),
                'pending':len(self.pending),'storage':self.store.snapshot()}

    def action(self,body):
        if not isinstance(body,dict):raise ValueError('操作格式错误')
        request_id=body.get('request_id')
        if not isinstance(request_id,str) or not 8<=len(request_id)<=100:raise ValueError('需要幂等请求ID')
        fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
        if request_id in self.actions:
            if self.actions[request_id]!=fingerprint:raise QueueConflict('请求ID不能用于不同操作')
            return {**self.snapshot(),'duplicate':True}
        if body.get('instance')!=self.instance or type(body.get('version')) is not int or body['version']!=self.version:
            raise QueueConflict('账号状态已变化，请刷新')
        action=body.get('action')
        if action=='settings':
            days=body.get('default_days')
            if type(days) is not int or not 1<=days<=365:raise ValueError('默认有效期须为1至365天')
            self.default_days=days;self._control()
        else:
            platform,account=body.get('platform'),body.get('account');key=identity(platform,account)
            row=self._fresh(key,self.store.rows.get(key,self._empty(platform,account)))
            if action=='revoke':row.update(state='revoked',epoch=row['epoch']+1,expires_at=None,outstanding=None,evidence=[],source='manual_revoke')
            elif action in {'confirm','expiry'}:
                expiry=body.get('expires_at',self.clock()+self.default_days*86400)
                if type(expiry) not in (int,float) or not self.clock()<expiry<=self.clock()+366*86400:raise ValueError('到期时间须在未来一年内')
                if action=='expiry' and self.effective(row) not in {'auto_confirmed','manual_confirmed'}:raise ValueError('仅能修改当前有效认证的到期时间')
                if action=='confirm':row.update(state='manual_confirmed',source='manual',epoch=row['epoch']+1,outstanding=None)
                row['expires_at']=expiry
            else:raise ValueError('不支持的操作')
            row['updated_at']=self.clock();self._put(key,row);self.pending.pop(key,None)
        self.actions[request_id]=fingerprint
        while len(self.actions)>512:self.actions.popitem(last=False)
        return self.snapshot()

    async def close(self):
        self.closed=True
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        await self.store.close()
