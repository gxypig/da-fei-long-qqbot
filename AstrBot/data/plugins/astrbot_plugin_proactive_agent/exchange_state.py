"""Bounded, receipt-backed exchanges and same-generation response selection."""
import json,re,time,uuid
from collections import OrderedDict,deque
from difflib import SequenceMatcher

PROTECTED=re.compile(r'[?？]|怎么|为什么|为何|如何|多少|什么|哪里|哪儿|哪个|能不能|可不可以|是不是|要不要|请|帮|查|搜索|找.{0,6}(?:图|视频)|画|生成|提醒|设置|继续|接着|解释|教我|不懂|看不懂|救|难受|受伤|疼|危险|蓝屏|报错|失败|不会|不能|打不开|连不上|问题|怎么办|不对|正确|有误')
def clean(text):return re.sub(r'\W+','',str(text or '').lower())
def message_body(text):
    # Numeric mentions identify the recipient, not the topic. In particular a
    # common QQ number must not make two unrelated short statements look alike.
    return clean(re.sub(r'\[CQ:at,[^\]]*\]|@\d+|\[(?:附件|图片)\]', '', str(text or '')))
def structure(text):return re.sub(r'[\u4e00-\u9fffA-Za-z0-9]{2,}', 'X',str(text or ''))
def protected(text,event=None):
    from .visual_intent import generation_kind
    value=str(text or '')
    return bool(PROTECTED.search(value) or re.search(r'在吗|有空吗|想知道|说说|告诉我|有没有|(?:发|给|找|来|搜).{0,8}(?:图|表情包)|(?:该|应该)有.{0,20}(?:表情包|梗图|动图)|[吗么][呀啊？?！!\s]*$',value)
        or value.lstrip().startswith(('/', '!')) or len(clean(value))>45 or generation_kind(value)!='none')

class Exchanges:
    def __init__(self,clock=time.time,manager=None,enabled=lambda:True):self.clock=clock;self.manager=manager;self.enabled=enabled;self.rows=OrderedDict();self.stats={'short':0,'cooldown':0,'model_skip':0,'new_question_preserved':0}
    def key(self,origin,sender):return (str(origin),str(sender))
    def state(self,origin,sender):
        key=self.key(origin,sender);now=self.clock()
        if key not in self.rows:self.rows[key]={'topic':'当前交流','messages':deque(maxlen=20),'replies':deque(maxlen=8),'turns':deque(maxlen=24),'cooldown':0,'epoch':0,'at':now}
        row=self.rows[key];self.rows.move_to_end(key)
        while len(self.rows)>256:self.rows.popitem(last=False)
        if now-row['at']>600:row.update(topic='当前交流',turns=deque(maxlen=24),cooldown=0)
        row['at']=now
        while row['turns'] and now-row['turns'][0]>300:row['turns'].popleft()
        return row
    def observe(self,event):
        if event.get_extra('qqbot_exchange_seen'):return
        event.set_extra('qqbot_exchange_seen',True)
        row=self.state(event.unified_msg_origin,event.get_sender_id())
        from .session_fence import get_fence
        # Native reset handling also invalidates a cached exchange.
        fence=get_fence(self.manager) if self.manager else None
        epoch=fence.epoch(event.unified_msg_origin) if fence else None
        if epoch is not None and epoch!=row['epoch']:row.update(messages=deque(maxlen=20),replies=deque(maxlen=8),turns=deque(maxlen=24),cooldown=0,epoch=epoch)
        row['messages'].append({'id':str(getattr(event.message_obj,'message_id','')),'text':event.get_message_str() or '', 'at':self.clock(),
                                'images':sum(type(p).__name__=='Image' for p in event.get_messages())})
    def view(self,origin,sender,text=''):
        row=self.state(origin,sender);count=len(row['turns']);recent=list(row['replies'])
        texts=[r['text'] for r in recent if r.get('text')]
        signals={'similar_pairs':sum(SequenceMatcher(None,clean(a),clean(b)).ratio()>.65 for a,b in zip(texts,texts[1:])),
                 'same_structure':sum(structure(a)==structure(b) and len(a)>8 for a,b in zip(texts,texts[1:])),
                 'automatic_images':sum(bool(r.get('automatic_image')) for r in recent)}
        cooling=row['cooldown']>self.clock()
        return {'topic':row['topic'],'rounds_5m':count,'shorten':count>=4 or cooling,'cooldown_until':row['cooldown'],'cooling_down':cooling,
                'protected':protected(text),'signals':signals,'recent_messages':list(row['messages']), 'confirmed_replies':recent}
    def before(self,job):
        if not self.enabled():return None
        event=job.event
        if not event:return None
        self.observe(event);view=self.view(job.origin,job.sender,job.text)
        event.set_extra('qqbot_exchange',view)
        if view['protected']:
            if view['rounds_5m']>=4:self.stats['new_question_preserved']+=1
            return None
        row=self.state(job.origin,job.sender)
        if view['cooling_down']:
            # Cool only a confirmed stagnant chain. A plainly new statement or
            # changed topic still reaches the model; one keyword never decides.
            previous=[r['text'] for r in list(row['messages'])[:-1][-4:]]
            body=message_body(job.text)
            # A one-character negation can reverse an otherwise identical
            # sentence. Only exact repetition bypasses generation; semantic
            # repetition is judged in the existing main call with its context.
            if body and any(body==message_body(t) for t in previous):
                self.stats['cooldown']+=1;return '同一话题连续无推进，短暂收束；新问题仍正常处理'
        return None
    def confirmed(self,row):
        if not self.enabled():return
        if row.get('state')!='confirmed' or row.get('purpose') not in {'answer',None} or not row.get('sender'):return
        event=row.get('exchange') or {};state=self.state(row['origin'],row['sender'])
        # Segments and separately sent decorations are ONE exchange round.
        ident=row.get('job_id') or row['id']
        previous=next((r for r in state['replies'] if r.get('job_id')==ident),None)
        if previous:
            if row['id'] not in previous['segments']:
                previous['segments'].append(row['id']);previous['text']+=row.get('text','')
                previous['automatic_image'] |= bool(row.get('optional') and row.get('attachments'))
            return
        progress=event.get('progress','unknown')
        if progress=='new':state['turns'].clear();state['cooldown']=0
        elif progress=='same':
            state['turns'].append(self.clock())
            if len(state['turns'])>=6:state['cooldown']=self.clock()+120
        topic=str(event.get('topic') or state['topic'])[:40];state['topic']=topic
        state['replies'].append({'job_id':ident,'segments':[row['id']],'text':row.get('text',''),'at':self.clock(),'automatic_image':row.get('optional',False) and bool(row.get('attachments'))})
    def prepare(self,event,state):
        if not self.enabled():return ''
        self.observe(event);view=self.view(event.unified_msg_origin,event.get_sender_id(),event.get_message_str())
        state['exchange']=view;event.set_extra('qqbot_exchange',view)
        nonce=event.get_extra('qqbot_exchange_nonce') or uuid.uuid4().hex[:10];event.set_extra('qqbot_exchange_nonce',nonce)
        # The full conversation is already in native context. Inject only bounded
        # signals and short reply fingerprints, never a second copy of history.
        recent={k:view[k] for k in ('topic','rounds_5m','shorten','protected','signals')}
        if view['cooling_down']:recent['cooling_down']=True
        recent['recent_replies']=[r['text'][:60] for r in view['confirmed_replies'][-8:]]
        state['exchange']=recent
        compact=('\n[交流状态] '+json.dumps(recent,ensure_ascii=False)) if recent['recent_replies'] else ''
        cooldown=''
        if view['cooling_down']:
            cooldown=('\n旧话题冷却尚未结束，但当前消息需要正常处理：这次必须给出回答正文，不得输出[[K]]。' if view['protected'] else
                      '\n当前两分钟冷却尚未结束：若只是旧话题换说法、没有新信息，输出[[K]]；有新内容仍正常回应。')
        return (compact+cooldown+'\n正常回应直接输出正文。只有延续同一话题而没有新信息时，正文前加[[S]]；连续四轮无推进时短接，六轮后可以结束，确无回应必要时只输出[[K]]。梗图和换个比喻不算新信息。新问题、求助、任务或明确继续必须处理；较晚发言只是背景，仍回应原消息。标记由程序移除，不向聊天展示。')
    def parse(self,event,text):
        nonce=event.get_extra('qqbot_exchange_nonce')
        if not nonce:return text,None
        from .send_checks import verbatim_request
        if verbatim_request(event.get_message_str()) is not None:return text,{'action':'normal','progress':'new'}
        # Models occasionally place the control at the end. It is still local
        # metadata, not a reason to invoke a second model to rewrite good text.
        compact=re.search(r'\[\[([SK])\]\]',text or '')
        if compact:
            body=re.sub(r'\[\[[SK]\]\]','',text).strip();view=event.get_extra('qqbot_exchange') or {}
            decision={'action':'skip' if compact[1]=='K' else 'short' if view.get('shorten') else 'normal','progress':'same'}
            if decision['action']=='skip' and protected(event.get_message_str()):decision.update(action='normal',invalid_skip=not body.strip())
            event.set_extra('qqbot_exchange_decision',decision)
            if decision['action']=='short':self.stats['short']+=1
            if decision['action']=='skip':self.stats['model_skip']+=1
            return body,decision
        pattern=r'^\s*\[\[EXCHANGE:'+re.escape(nonce)+r':(normal|short|skip):(new|same):([^\]\r\n]{1,40})\]\]\s*'
        match=re.match(pattern,text or '')
        if not match:
            # A malformed control line is internal data, never a QQ message.
            text=re.sub(r'(?m)^\s*\[\[EXCHANGE:[^\r\n]*(?:\n|$)','',text or '')
            decision={'action':'normal','progress':'new'}
            event.set_extra('qqbot_exchange_decision',decision)
            return text,decision
        body=text[match.end():];decision={'action':match[1],'progress':match[2],'topic':match[3]}
        if match[1]=='skip' and (protected(event.get_message_str()) or match[2]=='new'):
            decision['action']='normal';decision['invalid_skip']=not body.strip()
        event.set_extra('qqbot_exchange_decision',decision)
        if decision['action']=='short':self.stats['short']+=1
        if decision['action']=='skip':self.stats['model_skip']+=1
        return body,decision
    def optional_image_allowed(self,event):
        view=event.get_extra('qqbot_exchange') or {}
        return not view.get('shorten')
