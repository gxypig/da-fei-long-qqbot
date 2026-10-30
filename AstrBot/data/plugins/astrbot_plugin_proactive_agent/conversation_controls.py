"""Receipt-backed conversation boundaries; no classifier/model on the hot path."""
import hashlib
import re
import time
from .route_guard import canonical

_GOODBYE=re.compile(r'^(?:(?:那|好|嗯|行)[，, ]*)?(?:(?:我|本龙)?(?:先)?(?:去睡了|睡了|睡觉了|下了|走了)|晚安|拜拜|再见|下次聊|明天聊)[呀啦了呢哦哈～~！!。,.，\s]*$')
_IMAGE_STOP=re.compile(r'(?:别|不要|停止|不用|少).{0,6}(?:发|来|给|刷).{0,8}(?:图|表情)|(?:图|表情包).{0,10}(?:太多|这么多|刷屏|够了|库存清仓)|(?:库存清仓|发这么多).{0,8}(?:图|表情|喵)?')

def image_stop(text):return bool(_IMAGE_STOP.search(str(text or '')))
def explicit_quote_requested(text):
    # Evaluate clauses so "不要引用回复" never turns into "要引用回复".
    for clause in re.split(r'[，。；!?！？\n]',str(text or '')):
        if re.search(r'(?:不要|不用|不必|别|无需|取消).{0,6}引用',clause):continue
        if re.search(r'(?:请|记得|要|用|带上|加上).{0,5}引用(?:原消息|这条|我这条|回复)|引用.{0,4}(?:回复|回答)',clause):return True
    return False
def goodbye(text):
    text=re.sub(r'^\s*(?:@\d+\s*)+','',str(text or '')).strip()
    if _GOODBYE.fullmatch(text):return True
    # Keep this conservative: a new question, request or substantive clause
    # must reopen the conversation even when prefixed with "晚安".
    if not re.search(r'晚安|好梦|拜拜|再见|明天(?:见|聊)|下次聊|睡[了啦]|睡觉[了啦]|去睡|快睡|早点睡',text):return False
    value=re.sub(r'[\s，,。.!！～~…·\U0001f300-\U0001faff]','',text)
    value=re.sub(r'(?:这回|这次|那)?(?:不|别|不用)(?:再)?(?:喊|叫|回|回复|聊)(?:你|我|本龙)?(?:了)?','',value)
    return bool(re.fullmatch(r'(?:晚安|好梦|拜拜|再见|明天见|明天聊|下次聊|睡觉|睡|闭眼|唔|嗯|好|那|行|我|你|本龙|大家|各位|都|也|就|先|快|赶紧|早点|去|做个|祝你|祝|了|啦|呀|哦|呢|哈|吧|喽|咯|安安)+',value))

def closing_ack(text):
    value=re.sub(r'[\s，,。.!！～~…·()（）]','',str(text or ''))
    return bool(re.fullmatch(r'(?:嗯|唔|好|好哒|好的|行|收到|知道了|闭眼|躺下|睡啦|眼睛睁不开了|困了|😴|💤|🌙)+',value))
def _key(origin,sender):return '__conversation__:'+hashlib.sha256(repr((canonical(origin),str(sender))).encode()).hexdigest()

class ConversationControls:
    def __init__(self,ledger,clock=time.time):self.ledger=ledger;self.clock=clock

    def before(self,job):
        key=_key(job.origin,job.sender);state=dict(self.ledger.store.rows.get(key,{}));now=self.clock()
        parts=getattr(job.event,'get_messages',lambda:[])() if job.event else []
        has_material=any(type(part).__name__ not in {'Plain','At'} for part in parts)
        if not has_material and (goodbye(job.text) or closing_ack(job.text)) and state.get('closed_until',0)>now:return '重复告别，保留已确认的收尾'
        if (has_material or job.text.strip() and not goodbye(job.text)) and state.get('closed_until',0):
            self.ledger.store.put(key,{**state,'closed_until':0})
        if image_stop(job.text):
            event=job.event
            event.set_extra('qqbot_image_stop',True)
            self.ledger.store.put(key,{**state,'optional_images_stopped':True,'closed_until':0})
        else:
            from .visual_intent import generation_kind
            if generation_kind(job.text)!='none' or re.search(r'(?:发|给|找|来|搜).{0,8}(?:图|表情包)',job.text):
                if state.get('optional_images_stopped'):self.ledger.store.put(key,{**state,'optional_images_stopped':False,'closed_until':0})
        return None

    def optional_image_allowed(self,origin,sender):
        return not self.ledger.store.rows.get(_key(origin,sender),{}).get('optional_images_stopped')

    def delivered(self,row):
        if row.get('state')!='confirmed' or not row.get('sender'):return
        closing=str(row.get('text',''))
        confirmed_closing=bool(re.search(r'晚安|好梦|拜拜|再见|明天见|下次聊|早点睡|去睡',closing)) and not re.search(r'(?:别|不要|先不)(?:说)?(?:晚安|睡)|[?？]|等等|等下',closing)
        if goodbye(row.get('source_text','')) and confirmed_closing:
            key=_key(row['origin'],row['sender']);state=dict(self.ledger.store.rows.get(key,{}))
            self.ledger.store.put(key,{**state,'closed_until':self.clock()+600})

    def duplicate_answer(self,job,text,media=0):
        if not job or not text.strip() or media:return False
        event=job.event
        if not event or not event.get_extra('qqbot_agent_final_done'):return False
        if event.get_extra('qqbot_send_kind') in {'correction','new_fact','explicit_segment'}:return False
        expected=event.get_extra('qqbot_final_text') or ''
        rows=[self.ledger.store.rows.get(x,{}) for x in job.sends]
        sent=''.join(r.get('text','') for r in rows if r.get('state')=='confirmed' and r.get('purpose')=='answer')
        clean=lambda t:re.sub(r'\s+','',t)
        # Only when the COMPLETE planned answer has actually been acknowledged.
        # Partial multi-part text and previously failed/unknown sends are not success.
        return bool(expected and clean(sent)==clean(expected))
