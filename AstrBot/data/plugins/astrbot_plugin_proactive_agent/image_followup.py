"""Admit short visual continuations; the existing planner still decides intent.

No extra model call for ordinary conversation. Context is the selected session's
history, and group continuations never cross an intervening speaker's turn.
"""
import re,time

FOLLOWUP=re.compile(r'^(?:那(?:就)?[，,\s]*)?(?:(?:嗯|恩|好|好的|好啊|好呀|好吧|行|可以|可以啊|可|ok|就这样|就这么办|就这么着|按你说的(?:来|办)?|按这个来|照你说的(?:来|办)?|没问题|同意|开始吧|来吧)[，,、\s]*)+(?:就这样|就这么(?:着|办)|按你说的(?:来|办)?|开始(?:吧)?|弄吧|做吧|拍吧)?[啊呀吧呗嘛呢！!。~～\s]*$',re.I)
CHANGE=re.compile(r'^(?:那|嗯|好|好的|行|可以|请|再|帮我|给我|麻烦|能不能|可以不可以|你)*[，,\s]*(?:换成|改成|换个|换一|再来|重新来|按这|照这|用这|就这|选这|第二个|第一个|最后一个|要这).{0,85}$')
STOP=re.compile(r'不(?:用|要|必)|别(?:再|画|拍|做)|算了|取消|换个话题|先聊|先不|等会|等等|明天|以后|(?:怎么|为什么|如何|教程|解释|分析)')
VISUAL=re.compile(r'自拍|照片|图片|画面|生图|绘图|拍照|动图|gif|视频|短片|连衣裙|穿搭|衣服|服装|构图|立绘',re.I)
OFFER=re.compile(r'可以|要不|不如|换成|改成|选|方案|确认|同意|要.{0,12}[吗？?]|(?:没有|没|暂无|不能|无法|不会).{0,16}(?:工具|生图|生成|拍)|(?:工具).{0,8}(?:没有|不可用)')
REJECTED=re.compile(r'内容审核|画面请求被图片服务拒绝|图片服务拒绝了这次请求|不(?:能|会).{0,8}(?:照搬|照原|继续)')
EXECUTE=re.compile(r'^(?:(?:那|就|你|本龙|给我|帮我|请|赶紧|赶快|快点|快|倒是|现在|直接|继续|接着|赶紧给我)[，,\s]*)*(?:拍|录|画|开拍|开始拍|开始录|开始画|开始|动手)(?:一下|起来|出来|出来看看|吧|啊|呀|嘛|呗|啦|了|！|!|。|\s)*$')
AMEND=re.compile(r'(?:照片|图片|视频|动图|短片|自拍|这张|那张|这段|那段|画面).{0,25}(?:要|得|应该|请|换|改|穿|拿|戴|保留|别|不要)|(?:就|还是|记得|要|你要|让你|让她|再|把|给|这次|这回|现在).{0,12}(?:穿|拿|戴|换成|改成|保留).{1,70}|(?:换成|改成|换个|换一|再来|重新来).{1,70}|(?:衣服|外套|卫衣|裙子|颜色|背景|镜头|角度|光线|动作|表情|玩偶|小鸭|苹果).{0,12}(?:换|改|拉近|拉远|保留|挪|放到).{0,50}')
CONTEXTUAL_MEDIA=re.compile(r'(?:我(?:要|想)|给我|让(?:我|咱)|想|要)?(?:看看?|瞅瞅|瞧瞧).{0,12}(?:你|本龙|大肥龙).{0,8}(?:拍|录|摄).{0,6}(?:视频|短片|照片|自拍)|(?:把|让).{0,15}(?:图|照片).{0,8}(?:动起来|动一下)|(?:拍|录).{0,8}(?:视频|短片)')
TOPIC_END=re.compile(r'换个话题|算了|不画了|不拍了|取消|先聊别的')
COMPLETED=re.compile(r'刚拍的|刚录的|拍好了|录好了|收好|已(?:经)?(?:发出|发送)|给你(?:看看|看)|\[图片\]')
RESET_REFERENCE=re.compile(r'(?:不(?:用|要|再|必)|别)(?:再|继续)?(?:用|参考|照着|沿用|照搬).{0,18}(?:图|照片|参考)|(?:不用|不要|别用).{0,10}(?:那张图|这张图|原图|旧图)|(?:只|仅)(?:按|用|根据)(?:这次|当前|下面|以下)?文字(?:描述|要求)?(?:画|生成|创作)')

def reset_reference(text):
    return bool(RESET_REFERENCE.search(str(text or '')))

def content_text(message):
    value=message.get('content','')
    if isinstance(value,list):value=' '.join(p.get('text','') for p in value if isinstance(p,dict) and p.get('type')=='text')
    value=re.sub(r'<(?:system_reminder|Quoted Message)>.*?</(?:system_reminder|Quoted Message)>','',str(value),flags=re.S)
    value=re.sub(r'\[Image unavailable\]|\[补充图片不可用；不要猜测其中细节\]','',value)
    return re.sub(r'^\[(?:图片来源|Image \d)[^\n]*','',value,flags=re.M).strip()

def history_tail(event,req):
    """Contiguous owner/topic window; quoted prose is never an instruction."""
    from .image_spec import same_speaker
    selected=[];text=str(event.get_message_str() or '').strip()
    for message in reversed((getattr(req,'contexts',[]) or [])[-16:]):
        if message.get('role') not in {'user','assistant'}:continue
        value=content_text(message)
        if message['role']=='user':
            if not same_speaker(event,message.get('content','')):break
            clean=re.sub(r'\[群聊发言身份\]\s*\{[^\n]+?\}','',value).strip()
            if not selected and clean==text:continue
            if TOPIC_END.search(clean):break
            value=clean
        selected.append((message['role'],value))
    return selected

def needs_context(event,req):
    text=str(event.get_message_str() or '')
    return bool(CONTEXTUAL_MEDIA.search(text) or continuation(event,req))

def confirmed_visuals(plugin,event,req):
    ledger=getattr(plugin,'_delivery',None)
    if ledger is None:return []
    cid=getattr(getattr(req,'conversation',None),'cid',None)
    rows=ledger.recent(event.unified_msg_origin,cid,include_history=True)
    return [r for r in rows if r.get('state')=='confirmed' and r.get('visual') and str(r.get('sender'))==str(event.get_sender_id())][-2:]

def repeated_confirmation(event):
    text=str(event.get_message_str() or '').strip()
    return bool(FOLLOWUP.fullmatch(text) or EXECUTE.fullmatch(text))

def completed_execution(plugin,event,req):
    """An impatient repeat of a recent, receipted task is a status request.

    Match the initiating user turn, not its caption: TTS can send the caption
    separately from the animation, leaving the visual receipt text empty.
    """
    if not EXECUTE.fullmatch(str(event.get_message_str() or '').strip()):return None
    tail=history_tail(event,req)
    source=next((v for role,v in tail if role=='user' and not (FOLLOWUP.fullmatch(v) or EXECUTE.fullmatch(v))), '')
    if not source:return None
    for row in reversed(confirmed_visuals(plugin,event,req)):
        request=str(row.get('source_text') or '').strip()
        if request and source.endswith(request) and time.time()-600<=row.get('at',0)<=time.time():return row
    return None

def scoped_messages(event,messages):
    from .image_spec import same_speaker
    owner=':GroupMessage:' not in event.unified_msg_origin
    for message in messages:
        if message.get('role')=='user':owner=same_speaker(event,message.get('content',''))
        if owner and message.get('role') in {'user','assistant'}:yield message

def continuation(event,req,plugin=None):
    text=str(event.get_message_str() or '').strip()
    if not text or len(text)>180:return False
    amendment=bool(AMEND.search(text) or CHANGE.fullmatch(text) and not FOLLOWUP.fullmatch(text))
    if TOPIC_END.search(text) or (STOP.search(text) and not amendment):return False
    if re.search(r'^(?:为什么|怎么|如何)|(?:怎么|如何).{0,8}(?:拍|录|制作)|(?:好了|完成了|拍了|录了)(?:没|吗)|拍一拍|拍桌子|拍马屁|拍卖',text):return False
    if not (repeated_confirmation(event) or amendment):return False
    if any(type(c).__name__=='Reply' for c in getattr(event,'get_messages',lambda:[])()) and not amendment:return False
    from .visual_intent import generation_kind
    selected=history_tail(event,req)
    if not selected:return False
    nearest_user=next((v for role,v in selected if role=='user'), '')
    if nearest_user and not (generation_kind(nearest_user)!='none' or AMEND.search(nearest_user)
            or FOLLOWUP.fullmatch(nearest_user) or EXECUTE.fullmatch(nearest_user) or nearest_user.startswith('[图片]')):return False
    latest=next((v for role,v in selected if role=='assistant'), '')
    if not latest or REJECTED.search(latest) or '无法确认服务端' in latest:return False
    sent_captions={str(r.get('text') or '').strip() for r in confirmed_visuals(plugin,event,req)} if not amendment and plugin is not None else set()
    # A confirmed artwork is a fact, even if its caption sounds like an offer.
    if latest.strip() in sent_captions:return False
    if not amendment and COMPLETED.search(latest) and not re.search(r'没|未|没有|尚未',latest):return False
    visual_request=any(role=='user' and generation_kind(value)!='none' for role,value in selected)
    if not visual_request:return False
    if amendment:return True
    changed=bool(CHANGE.fullmatch(text) and not FOLLOWUP.fullmatch(text))
    pending_edit=False
    for role,value in selected:
        if role=='assistant' and (REJECTED.search(value) or '无法确认服务端' in value):return False
        if role=='assistant' and COMPLETED.search(value) and not re.search(r'没|未|没有|尚未',value):return False
        if role=='assistant' and value.strip() in sent_captions:return False
        if role=='user' and AMEND.search(value):pending_edit=True;break
        if role=='user' and not (FOLLOWUP.fullmatch(value) or EXECUTE.fullmatch(value)):
            break
    return bool(pending_edit or EXECUTE.fullmatch(text) or (VISUAL.search(latest) or len(latest)<80) and (OFFER.search(latest) or changed))

CAPABILITY_NOTE=('\n图片创作能力事实：本系统已启用独立图片/短循环动图流程，不依赖聊天工具列表里是否有绘图函数。'
    '不要因列表缺少绘图函数就声称没有工具、不会生图；未获得实际附件不能声称已拍好。'
    '服务拒绝某次内容不等于功能不可用，不重复提交被拒绝的原要求。'
    '这里口语中的拍视频默认用短循环动图交付；确认发出后该次拍摄已完成，不再虚构另有未交付的视频文件。只有用户明确要求MP4或长视频时才解释格式范围。'
    '只有实际任务状态说明正在执行时才说正在拍或尚未生成完成；口头确认穿搭不等于任务已经开始。平台已发送作品记录优先于旧回复里的自述。')
