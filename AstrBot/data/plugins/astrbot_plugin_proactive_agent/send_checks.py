"""Objective checks and receipt metadata. No semantic judge or hidden model call."""
import copy
import hashlib
import json
import re
from contextvars import ContextVar

_check=ContextVar('qqbot_send_check',default=None)

def begin_task():return _check.set(None)
def end_task(token):_check.reset(token)
_QUOTES=re.compile(r'```[\s\S]*?```|“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|(?m:^>[^\n]*)')
_INTERNAL=re.compile(r'Traceback \(most recent call last\):|(?m:^\s*File "[^"\n]+", line \d+)|'
                     r'(?i:sk-[a-zA-Z0-9]{20,}|Authorization:\s*Bearer\s+\S+)|'
                     r'(?m:^\s*(?:<tool_call>|<function_call>|\[QQBOT_[A-Z_]+\]))')
_PLACEHOLDER=re.compile(r'^(?:None|null|undefined|\{(?:reply|response|content|text)\}|<empty>|\[待生成\])$',re.I)

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()

def verbatim_request(text):
    """Only explicitly delimited source. Ambiguous requests retain normal handling."""
    raw=re.sub(r'^\s*(?:@\d+\s*)+','',text or '')
    # A newline after an explicit copy heading is a delimiter too. Normalize
    # only the heading: source whitespace, script and Unicode must remain exact.
    block=re.fullmatch(r'([^\r\n]{1,120})(?:\r\n|\n|\r)([\s\S]+)',raw)
    if block:
        heading=block[1].translate(str.maketrans('請復這條說','请复这条说')).strip()
        copying=re.search(r'复述|复读|重复|照抄|(?:原样|逐字)(?:输出|发送)',heading)
        source_named=re.search(r'以下|下面|这些|这段|原文|每一条|每条',heading)
        bare=re.fullmatch(r'(?:请|能否|麻烦|帮我|帮忙|用文字|原样|逐字|一字不差|\s)*(?:复述|复读|重复|照抄)(?:一下)?[：:？?\s]*',heading)
        excluded=re.search(r'不要|不用|别|不必|概括|摘要|总结|改写|翻译|解释|大意|为什么|为何|怎么|如何|什么意思',heading)
        if copying and (source_named or bare) and not excluded:return block[2]
    match=re.fullmatch(r'\s*(?:请)?(?:原样|一字不差|逐字)(?:复述|重复|转述|发送)(?:这句话|下面这段|以下内容)?\s*[:：]\s*([\s\S]+)',raw)
    if not match:return None
    value=match[1]
    if value.startswith('“') and value.endswith('”'):return value[1:-1]
    if value.startswith('「') and value.endswith('」'):return value[1:-1]
    if value.startswith('```') and value.endswith('```'):return value[3:-3].strip('\r\n')
    return value

def banned_tics(persona):
    found=[]
    for match in re.finditer(r'(?:不要|不许|禁止|禁用|不使用)[^\n。；]{0,12}[“「\"]([^”」\"\n]{1,12})[”」\"][^\n。；]{0,8}(?:口癖|语尾)|(?:不要|不许|禁止|禁用)[^\n。；]{0,6}(喵)(?:等|这种|这类)?(?:口癖|语尾)',persona or ''):
        found.append(match[1] or match[2])
    return list(dict.fromkeys(found))

def issues(text,*,state=None,mode='chat',media=0):
    state=state or {};text=str(text or '');out=[]
    if not text.strip() and not media:out.append('empty_output')
    if _PLACEHOLDER.fullmatch(text.strip()):out.append('placeholder_output')
    # User explicitly quoting a traceback is different from exposing an internal exception.
    if _INTERNAL.search(text) and not state.get('verbatim_text') and not state.get('quoted_error_requested'):
        out.append('internal_error_or_control_payload')
    own=_QUOTES.sub('',text)
    for word in state.get('self_banned_phrases',[]):
        if isinstance(word,str) and word and re.search(re.escape(word)+r'(?=$|[\s，,。！？!?～~])',own):out.append('self_banned_phrase:'+word)
    expected=state.get('verbatim_text')
    if expected is not None and text!=expected:out.append('verbatim_mismatch')
    # Only a producer's explicit structured contradiction, never guess failure from prose.
    for claim in state.get('invalid_completion_claims',[]):
        if isinstance(claim,str) and claim and claim in own:out.append('unconfirmed_completion_claim')
    return list(dict.fromkeys(out))

def normalize(text,state=None,mode='chat'):
    state=state or {}
    if state.get('verbatim_text') is not None:return state['verbatim_text']
    if mode=='management':return text
    from .chat_punctuation import casual_punctuation
    # Preserve quotation bytes while cleaning our own punctuation.
    pieces=[];at=0
    for match in _QUOTES.finditer(text or ''):
        pieces.extend((casual_punctuation(text[at:match.start()],mode),match[0]));at=match.end()
    pieces.append(casual_punctuation((text or '')[at:],mode))
    return ''.join(pieces)

def own_style(text,state=None,mode='chat'):
    """Formatting/count limits apply to authored speech, never quoted bytes."""
    state=state or {}
    if state.get('verbatim_text') is not None:return state['verbatim_text']
    from .dragon_character import limit_emoji,limit_father_address
    quotes=[]
    def mask(match):
        quotes.append(match[0]);return '\ue000'+str(len(quotes)-1)+'\ue001'
    value=_QUOTES.sub(mask,text or '')
    value=limit_father_address(limit_emoji(value,state,mode),state,mode)
    for i,quote in enumerate(quotes):value=value.replace('\ue000'+str(i)+'\ue001',quote)
    return normalize(value,state,mode)

def record(origin,text,settings,*,state=None,recent=(),path=None,local=(),reviewed=False):
    from .reply_jobs import current_job
    job=current_job.get();state=state or {}
    value={'task_id':job.id if job else None,'target':origin,'mode_snapshot':copy.deepcopy(settings),
           'persona_version':state.get('persona_version'),'context_fingerprint':digest(list(recent)),
           'final_fingerprint':digest(text),'path':path or ('review_off' if not settings['enabled'] else 'full_review'),
           'local_issues':list(local),'final_body_reviewed':bool(reviewed)}
    _check.set(value);return value

def snapshot(origin,text):
    value=_check.get()
    if not value or value.get('final_fingerprint')!=digest(text):return None
    same=value.get('target')==origin
    if not same:
        from .route_guard import canonical
        try:same=canonical(value.get('target',''))==canonical(origin)
        except ValueError:pass
    if same:return {**copy.deepcopy(value),'target':origin}
    return None
