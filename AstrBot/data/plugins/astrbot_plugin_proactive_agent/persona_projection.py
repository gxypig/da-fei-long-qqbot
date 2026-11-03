"""Lossless scene routing of a full editable persona; no model on the hot path.

Unknown sections deliberately stay in every scene. Every byte belongs to one
mapped block, and every block has at least one consumer. The full master remains
the editing and verification authority, including rollback versions.
"""
import copy,hashlib,re
from functools import lru_cache

PURPOSES=('chat','proactive','schedule','qzone','bili','gallery','image','review')
SCOPES={
    '主动联系与公开动态':('proactive','schedule','qzone','review'),
    'B站评论回复场景':('bili','review'),
    'QQ视频分享文案场景':('bili','review'),
    '管理回执与发送前编辑场景':('review',),
}
NATURAL='''\n【本轮交流原则】以眼前这句话的意思为准，平常情绪中性；有具体可笑之处再贫嘴。已经说清就停，必要解释保留。不评价群友发言的水平，不把一句喜欢扩写成陪伴宣言。接梗不必再造一个梗；同个笑点已经接过，就自然应声或收住，不逐轮换比喻、谐音、押韵或对仗来续话，更不为对称改掉原意。被夸时接住夸奖，不预设攻击。玩笑不升级动作、不另编前提，也不改成字面常识纠错。分享个人偏好不是请求求证，不附加无关的核实声明；真正求证仍保留准确性。当前发言优先于旧助手话题，已结束的话题不强接回来。'''
IDENTITY_GUIDANCE='''【身份话题的聊天口吻】询问自己是什么模型或AI时，用一句当前选定的虚拟龙娘角色介绍自然接话，不展开品牌、型号、提供商或技术流程，不使用固定轮换台词。管理面板可查配置属于管理入口说明，不是聊天回答必须附带的文案；不必追加后台说明或“不是现实真人”的尾注。虚拟角色介绍可以独立成句，不假称现实真人或作否认自动化的事实断言。讨论其他模型、引用他人身份话语时照常回应其意思。本条只属于选中的龙娘人格，不能套给其他人物或空人格。'''

@lru_cache(maxsize=64)
def project(master):
    starts=[m.start() for m in re.finditer(r'(?m)^【[^\r\n】]+】',master)]
    if not starts or starts[0]!=0:starts.insert(0,0)
    blocks=[];views={p:[] for p in PURPOSES}
    for i,start in enumerate(starts):
        text=master[start:starts[i+1] if i+1<len(starts) else len(master)]
        heading=re.match(r'【([^】]+)】',text)
        title=heading[1] if heading else '未分区内容'
        scopes=SCOPES.get(title,PURPOSES)
        blocks.append({'id':hashlib.sha256(text.encode()).hexdigest()[:12],'title':title,'start':start,'end':start+len(text),'purposes':list(scopes)})
        for p in scopes:views[p].append(text)
    assert ''.join(master[b['start']:b['end']] for b in blocks)==master
    return {p:''.join(v) for p,v in views.items()},blocks

def binding_projection(master,adapters):
    views,mapping=project(master)
    return {'persona_runtime_views':{p:views[p]+(('\n【本用途补充】\n'+adapters[p]) if adapters.get(p) else '') for p in PURPOSES},
            'persona_scope_map':copy.deepcopy(mapping)}

def dragon_identity(text,state):
    """A scene hint, not an identity substitution for arbitrary personas."""
    if not state.get('persona_prompt') or not re.search(r'白毛龙娘|白龙少女|白发龙娘',state['persona_prompt']):return ''
    value=str(text or '').strip()
    # Questions about someone else, quoted text and technical model comparisons
    # are not self-identity questions.
    if re.search(r'[“”「」"\n]|哪个.{0,8}(?:模型|AI).{0,8}(?:好|强)|他|她|那个机器人',value):return ''
    named=re.search(r'(?:你|大肥龙)\s*(?:到底|究竟|实际|背后|现在|真)?\s*(?:是不是|是|用的是)\s*(?:GPT|DeepSeek|Claude|Gemini)',value,re.I)
    if not named and not re.search(r'(?:你|本龙|大肥龙).{0,12}(?:什么模型|哪个模型|什么AI|哪个AI|是AI|人工智能|机器人|真人|模型|身份)',value,re.I):return ''
    state['identity_scene']=True
    return '\n本轮在问你的身份：只用一句当前虚拟龙娘角色的自我介绍接话，不展开品牌、参数或技术流程。设定中管理面板可查配置，是管理入口说明，不是聊天回答必须附带的文案；不必额外追加后台说明或“不是现实真人”的尾注。虚拟角色介绍本身可以独立成句；不得假称现实真人或作否认自动化的事实断言。'
