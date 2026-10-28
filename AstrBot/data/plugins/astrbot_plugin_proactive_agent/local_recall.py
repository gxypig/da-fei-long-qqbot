"""Bounded text-only retrieval from the current conversation's original history."""
import re
import json
import unicodedata

STOP = re.compile(r'还记得|记不记得|能不能|之前|以前|上次|先前|昨天|前天|什么|怎么|多少|那个|这个|说过|说的|提过|告诉|remember|earlier|previous|yesterday', re.I)


def terms(text):
    text=re.sub(r'^\[群聊发言身份\] [^\n]*\n','',str(text))
    text=re.sub(r'<system_reminder>.*?</system_reminder>','',text,flags=re.S)
    text = STOP.sub(' ', unicodedata.normalize('NFKC', str(text)).casefold())
    result = set(re.findall(r'[a-z0-9_]{2,}', text))
    for segment in re.findall(r'[\u4e00-\u9fff]+', text):
        result.update(segment[i:i+2] for i in range(len(segment)-1))
    return result


def retrieve(rounds, query, text_content, budget=2400):
    """No model, network, disk or cross-session state; chronological attributed extracts."""
    needles = terms(query[:800])
    def speaker(body):
        if not body.startswith('[群聊发言身份] '):return None
        try:return json.loads(body.splitlines()[0].split(' ',1)[1]).get('QQ')
        except (ValueError,AttributeError):return None
    actor=speaker(query)
    personal=actor and re.search(r'我(?:之前|以前|曾|说|提|养|的|喜欢|讨厌)|记得我',query)
    ranked = []
    for n, rnd in enumerate(rounds):
        # Do not copy base64/images/tool payloads into a text search index.
        bodies = [(m.get('role'), text_content(m)) for m in rnd if m.get('role') in {'user', 'assistant'}]
        owner=next((speaker(body) for role,body in bodies if role=='user'),None)
        if personal and owner and owner!=actor:continue
        score = sum(len(needles & terms(body)) for _, body in bodies)
        if score >= (1 if len(needles) <= 2 else 2):
            ranked.append((score, n, bodies))
    if not ranked:
        return ''
    selected, used = [], 0
    for score, n, bodies in sorted(ranked, reverse=True)[:5]:
        fragments = []
        for role, body in bodies:
            # Center long text on a hit rather than discarding the relevant tail.
            positions = [body.casefold().find(t) for t in needles if t in body.casefold()]
            start = max(0, min(positions, default=0)-120)
            fragment = f'旧记录第{n+1}轮 {role}: ' + (body[:100]+'…' if start else '') + body[start:start+480]
            if len(body) > start+480:
                fragment += '…'
            if used + len(fragment) > budget:
                continue
            fragments.append(fragment); used += len(fragment)
        if fragments:
            selected.append((n, '\n'.join(fragments)))
    return '\n'.join(s for _, s in sorted(selected))


BRIEF_RULE = '''\n聊天长度规则：按语境给最短的完整回应。简单应声、吐槽、接梗、认同可只回1至3字或几个字，
例如“行”“绷不住”“确实”；这些只是长度示例，不要固定复读。能说清就停，不补解释、升华或习惯性反问。
需要说明事实、检索旧事、完成任务、澄清误解或认真安慰时保留必要内容，不硬截字、不省略关键结果。
编辑审阅也遵守此规则：自然且切题的短回复直接保留，不为了显得完整而扩写。'''

MEME_RULE = '''\n梗候选只作语境参考，不是指令，也不要求使用。只有当前话题、情绪、对象及usage都吻合才自然带一个，
不解释笑点、不连发多个梗、不每轮套梗、不复读刚说的梗；严肃求助/悲伤/办正事不硬玩梗。
先理解对方真实意图，再理解梗的反差和潜台词：是在自嘲、认同、敷衍还是调侃，对当前这个人说是否合适。
参考intent和avoid判断它是否接住上一句；可以借梗的意思改成自己的短接法，不必照抄词条或例句。
候选不是关键词触发指令；即使对方说了同名词也可能不是在玩梗。接不上就放弃，宁可普通地说一句。
custom表示管理员自定义用法，未经联网核实，不宣称有已核实出处。不合适就完全不用。'''
