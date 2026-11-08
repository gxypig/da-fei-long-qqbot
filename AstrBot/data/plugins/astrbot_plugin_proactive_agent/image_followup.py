"""Admit short visual continuations; the existing planner still decides intent.

No extra model call for ordinary conversation. Context is the selected session's
history, and group continuations never cross an intervening speaker's turn.
"""
import re

FOLLOWUP=re.compile(r'^(?:那(?:就)?[，,\s]*)?(?:(?:嗯|恩|好|好的|好啊|好呀|好吧|行|可以|可以啊|可|ok|就这样|就这么办|就这么着|按你说的(?:来|办)?|按这个来|照你说的(?:来|办)?|没问题|同意|开始吧|来吧)[，,、\s]*)+(?:就这样|就这么(?:着|办)|按你说的(?:来|办)?|开始(?:吧)?|弄吧|做吧|拍吧)?[啊呀吧呗嘛呢！!。~～\s]*$',re.I)
CHANGE=re.compile(r'^(?:那|嗯|好|好的|行|可以|请|再|帮我|给我|麻烦|能不能|可以不可以|你)*[，,\s]*(?:换成|改成|换个|换一|再来|重新来|按这|照这|用这|就这|选这|第二个|第一个|最后一个|要这).{0,85}$')
STOP=re.compile(r'不(?:用|要|必)|别(?:再|画|拍|做)|算了|取消|换个话题|先聊|先不|等会|等等|明天|以后|(?:怎么|为什么|如何|教程|解释|分析)')
VISUAL=re.compile(r'自拍|照片|图片|画面|生图|绘图|拍照|动图|gif|视频|短片|连衣裙|穿搭|衣服|服装|构图|立绘',re.I)
OFFER=re.compile(r'可以|要不|不如|换成|改成|选|方案|确认|同意|要.{0,12}[吗？?]|(?:没有|没|暂无|不能|无法|不会).{0,16}(?:工具|生图|生成|拍)|(?:工具).{0,8}(?:没有|不可用)')
REJECTED=re.compile(r'内容审核|画面请求被图片服务拒绝|图片服务拒绝了这次请求|不(?:能|会).{0,8}(?:照搬|照原|继续)')
RESET_REFERENCE=re.compile(r'(?:不(?:用|要|再|必)|别)(?:再|继续)?(?:用|参考|照着|沿用|照搬).{0,18}(?:图|照片|参考)|(?:不用|不要|别用).{0,10}(?:那张图|这张图|原图|旧图)|(?:只|仅)(?:按|用|根据)(?:这次|当前|下面|以下)?文字(?:描述|要求)?(?:画|生成|创作)')

def reset_reference(text):
    return bool(RESET_REFERENCE.search(str(text or '')))

def content_text(message):
    value=message.get('content','')
    if isinstance(value,list):value=' '.join(p.get('text','') for p in value if isinstance(p,dict) and p.get('type')=='text')
    return re.sub(r'<system_reminder>.*?</system_reminder>','',str(value),flags=re.S).strip()

def scoped_messages(event,messages):
    from .image_spec import same_speaker
    owner=':GroupMessage:' not in event.unified_msg_origin
    for message in messages:
        if message.get('role')=='user':owner=same_speaker(event,message.get('content',''))
        if owner and message.get('role') in {'user','assistant'}:yield message

def continuation(event,req):
    text=str(event.get_message_str() or '').strip()
    if not text or len(text)>100 or STOP.search(text) or not (FOLLOWUP.fullmatch(text) or CHANGE.fullmatch(text)):return False
    if any(type(c).__name__=='Reply' for c in getattr(event,'get_messages',lambda:[])()):return False
    from .image_spec import same_speaker
    from .visual_intent import generation_kind
    history=getattr(req,'contexts',[]) or []
    selected=[]
    for message in reversed(history[-10:]):
        if message.get('role') not in {'user','assistant'}:continue
        value=content_text(message)
        if message.get('role')=='user':
            if not same_speaker(event,message.get('content','')):break
            clean=re.sub(r'\[群聊发言身份\]\s*\{[^\n]+?\}','',value).strip()
            if not selected and clean==text:continue
            if re.search(r'换个话题|算了|不画了|不拍了|取消',clean):break
        selected.append((message.get('role'),value))
    if not selected:return False
    latest=next((v for role,v in selected if role=='assistant'), '')
    if not latest or REJECTED.search(latest):return False
    visual_request=any(role=='user' and generation_kind(value)!='none' for role,value in selected)
    if not visual_request:return False
    changed=bool(CHANGE.fullmatch(text) and not FOLLOWUP.fullmatch(text))
    return bool((VISUAL.search(latest) or len(latest)<80) and (OFFER.search(latest) or changed))

CAPABILITY_NOTE=('\n图片创作能力事实：本系统已启用独立图片/短循环动图流程，不依赖聊天工具列表里是否有绘图函数。'
    '不要因列表缺少绘图函数就声称没有工具、不会生图；未获得实际附件不能声称已拍好。'
    '服务拒绝某次内容不等于功能不可用，不重复提交被拒绝的原要求。')
