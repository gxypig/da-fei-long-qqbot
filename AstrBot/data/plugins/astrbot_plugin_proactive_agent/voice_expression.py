"""Sparse S2 performance directions, applied only to the final TTS input.

No LLM/IO/random draw here. The original transcript remains the source of truth.
Fish's speech model interprets the full line; these conservative local cues are
delivery hints, not another personality prompt or a claim to infer hidden mood.
Docs: https://docs.fish.audio/developer-guide/core-features/emotions
"""
import re
import time

VERSION = 1
MODELS = {'s2.1-pro-free', 's2.1-pro'}
DEFAULT = 'natural conversational tone'
ALLOWED = {DEFAULT, 'relaxed', 'soft tone', 'whispering', 'confident',
           'happy', 'excited', 'curious', 'surprised', 'sad', 'empathetic',
           'slightly sarcastic', 'uncertain', 'emphasis'}
_QUOTED = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|‘[^’]*’|`[^`]*`')
_CLAUSE = re.compile(r'[^，,。！？!?；;\n]+[，,。！？!?；;\n]*')


def _mask_quotes(text):
    return _QUOTED.sub(lambda m: ' ' * len(m[0]), text)


def _requested_tone(instruction):
    # Only explicit delivery directions, not "她说话很温柔" or quoted history.
    text = _mask_quotes(str(instruction or '')[-600:])
    for pattern, cue in (
        (r'(?:小声|悄悄|压低声音)(?:一点|点|地)?(?:说|读|念|讲|回)|(?:用|以)(?:悄悄话|耳语)', 'whispering'),
        (r'(?:温柔|轻柔|轻声)(?:一点|点|地)?(?:说|读|念|讲|回)|(?:用|以)温柔的?(?:语气|声音)', 'soft tone'),
        (r'(?:开心|兴奋)(?:一点|点|地)?(?:说|读|念|讲|回)|(?:用|以)开心的?(?:语气|声音)', 'happy'),
        (r'(?:平静|自然)(?:一点|点|地)?(?:说|读|念|讲|回)|(?:用|以)平静的?(?:语气|声音)', 'relaxed'),
    ):
        for match in re.finditer(pattern, text):
            if not re.search(r'不要|不用|不许|别|禁止', text[max(0, match.start()-6):match.start()]):
                return cue
    return None


def _mood(clause):
    text = _mask_quotes(clause).strip()
    # Comfort outranks joking keywords; negated happiness must not become happy.
    if re.search(r'辛苦(?:了|啦|你)|别(?:怕|急|难过|着急)|不(?:用|要)怕|先缓(?:缓|一缓)|慢慢来|有我在|陪着你|好好休息|早点休息|晚安', text):
        return 'soft tone'
    if re.search(r'(?:我|本龙).{0,5}(?:好难过|很难过|有点难过|不开心|好失落)|真(?:的)?很遗憾', text):
        return 'sad'
    if re.search(r'太好了|好耶|好棒|赢啦|成功啦|开心死了', text):
        return 'excited'
    if re.search(r'^(?:哇|诶[？?]|啊[？?])|居然|竟然', text):
        return 'surprised'
    if re.search(r'难不住(?:我|本龙)|交给(?:我|本龙)|本龙(?:当然|可不是|厉害|出手)|那还用说|哼哼', text):
        return 'confident'
    if re.search(r'^(?:就这[？?]?|你倒是敢想|想得美|少来这套|嘴挺硬|还挺会使唤)', text):
        return 'slightly sarcastic'
    if re.search(r'(?:不太确定|还不确定|我也拿不准|说不准)', text):
        return 'uncertain'
    if re.search(r'哈哈|嘿嘿|好开心|真开心|谢谢|喜欢你', text) and not re.search(r'不.{0,2}(?:开心|喜欢)|别笑', text):
        return 'happy'
    if re.search(r'[？?]|(?:哪(?:个|张|里)|怎么|什么|为啥|为什么|多少|几点|谁).{0,16}[呀呢啊吗嘛]?$', text):
        return 'curious'
    return None


def _emphasis(text, instruction):
    # An explicit quote is used only as an exact anchor, never as a directive.
    for match in re.finditer(r'(?:强调|重读)[一下]*[「“"‘]([^」”"’\n]{1,20})[」”"’]', str(instruction or '')[-600:]):
        if re.search(r'不要|不用|别|禁止', str(instruction or '')[-600:][max(0, match.start()-6):match.start()]):
            continue
        at = text.find(match[1])
        if at >= 0 and text.count(match[1]) == 1:
            return at
    unquoted = _mask_quotes(text)
    match = re.search(r'(?:记住[，,：:]?|重点是[，,：:]?|最重要的是[，,：:]?|说的是[，,：:]?)(\S{1,12})', unquoted)
    if match:
        return match.start(1)
    match = re.search(r'(?:千万别|千万不要|一定要|只有一次|只说一次)', unquoted)
    return match.start() if match else None


def plan(text, *, enabled=True, instruction=''):
    """Insert at most three delivery changes + one stress cue; never edit words."""
    started = time.perf_counter()
    cues = []
    reason = 'enabled'
    if not enabled:
        reason = 'disabled'
    elif not text.strip():
        reason = 'empty'
    elif re.search(r'[\[\]<>`]|https?://', text):
        # Leave authored bracket directions/technical literals alone. Panel
        # previews can deliberately audition documented Fish tags verbatim.
        reason = 'literal_or_authored_markup'
    else:
        requested = _requested_tone(instruction)
        previous = None
        for match in _CLAUSE.finditer(text):
            clause = match[0]
            at = match.start() + len(clause) - len(clause.lstrip())
            if not clause.strip():
                continue
            cue = requested or _mood(clause)
            if previous is None:
                cue = cue or DEFAULT
            # Neutral clauses return to normal instead of inheriting sarcasm.
            elif cue is None and re.search(r'[。！？!?；;\n]\s*$', text[:match.start()]):
                cue = DEFAULT
            if cue and cue != previous and len(cues) < 3:
                cues.append({'offset': at, 'tag': cue})
                previous = cue
        at = _emphasis(text, instruction)
        if at is not None and len(text.strip()) > 4:
            cues.append({'offset': at, 'tag': 'emphasis'})
    marked = text
    for cue in sorted(cues, key=lambda c: (c['offset'],c['tag']=='emphasis'), reverse=True):
        marked = marked[:cue['offset']] + '[' + cue['tag'] + ']' + marked[cue['offset']:]
    return {'text': marked, 'cues': cues, 'version': VERSION, 'reason': reason,
            'planning_ms': round((time.perf_counter()-started)*1000, 3)}
