"""Narrow character identity boundaries; no model or whole-chat replacement."""
import hashlib,re

RULE='''本轮是自身身份问答：用当前白毛龙娘身份自然短答，已介绍过就顺着接话；不背外貌清单，不追加“虚拟、AI扮演、程序设定、型号未知、无法透露”等幕后说明。角色身份回答已经接住问题，不必补技术解释；讨论他人、引用和真正的技术问题仍按原意回答。不要假称现实真人或编造品牌型号。'''
_QUOTE=re.compile(r'```[\s\S]*?```|“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|(?m:^>[^\n]*)')
_BAD=re.compile(r'虚拟|(?:AI|人工智能|程序|模型).{0,5}(?:扮演|设定|驱动)|(?:底层|具体|实际|背后).{0,5}(?:模型|型号|架构|参数)|(?:型号|模型|提供商).{0,12}(?:未知|不清楚|没有|没法|无法|不可|不确定|不能|不知道)|(?:不知道|不清楚|没有确切|无法透露|不能透露).{0,12}(?:型号|模型|提供商)|(?:我是|本龙是).{0,6}(?:GPT|DeepSeek|Claude|Gemini|AI|人工智能)|(?:不是|并非).{0,3}(?:现实|真实)?(?:真人|人类)',re.I)

def enabled(state):
    return state.get('persona_status') not in {'disabled','missing','error','unavailable'} and bool(re.search(r'白毛龙娘|白龙少女|白发龙娘',state.get('persona_prompt','')))

def direct(text):
    value=_QUOTE.sub('',str(text or '')).strip()
    if re.search(r'虚拟机|虚拟内存|(?:哪个|哪种).{0,8}(?:模型|AI).{0,12}(?:适合|更好|更强)|(?:他|她|那个机器人).{0,8}(?:模型|身份|AI)|你(?:觉得|知道|推荐|了解|听说|认为|帮|教|怎么看)',value,re.I):return False
    return bool(re.search(r'(?:你|大肥龙).{0,10}(?:什么模型|哪个模型|什么AI|哪个AI|底层型号)|(?:你|大肥龙)(?:到底|究竟|实际上|现在|\s)*(?:是谁|是什么(?:东西|种族|生物|身份)?[？?！!啊呀呢嘛\s]*$|是啥[？?！!啊呀呢嘛\s]*$|(?:是不是|是否是|是)(?:GPT|DeepSeek|Claude|Gemini|AI|人工智能|机器人|真人))|(?:你|大肥龙)(?:的)?(?:底层|模型|身份)(?:是什么|是啥|呢|[？?])',value,re.I))

def detect(text,state,recent=(),sender=None,now=None):
    if not enabled(state):return False
    if direct(text):return True
    value=_QUOTE.sub('',str(text or '')).strip()
    if not re.fullmatch(r'(?:不对[，, ]*|别绕[，, ]*|我问的是?|那|所以|到底|具体|实际|背后|不是这个|[，, ])*(?:底层(?:型号|模型)?|型号|模型|用哪个|哪个AI)(?:是啥|是什么|呢|啊|呀|到底|[？?！!，, ])*',value,re.I):return False
    # Only this sender's previous turn, in the captured same-session window.
    previous=[r for r in recent if isinstance(r,dict) and str(r.get('sender',''))==str(sender) and r.get('text')!=text and (now is None or now-r.get('time',0)<600)]
    return bool(previous and direct(previous[-1].get('text','')))

def guard(text,state,mode='chat'):
    if mode!='chat' or not state.get('identity_scene') or not enabled(state) or state.get('verbatim_text') is not None:return text
    unquoted=_QUOTE.sub('',str(text or '')).strip()
    if not (_BAD.search(unquoted) or re.match(r'^(?:是|算是|对[，, ]*)?(?:一个)?(?:人工智能|AI)(?:[，,。！!\s]|助手|程序|模型|$)',unquoted,re.I)):return text
    match=re.search(r'(?m)^身份备用短答[：:]\s*([^\r\n]{1,80})',state.get('persona_prompt',''))
    fallback=match[1].strip() if match and not _BAD.search(match[1]) else '本龙是白毛龙娘'
    state['identity_guard']={'reason':'自身身份回答出现幕后说明或技术尾巴，采用当前角色备用短答','original_sha256':hashlib.sha256(text.encode()).hexdigest(),'final_sha256':hashlib.sha256(fallback.encode()).hexdigest()}
    return fallback
