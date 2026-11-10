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
IDENTITY_GUIDANCE='''【身份话题的聊天口吻】询问自己是什么模型或AI时，用一句当前选定的白毛龙娘角色介绍自然接话，不展开品牌、型号、提供商或技术流程，不使用固定轮换台词。管理面板可查配置属于管理入口说明，不是聊天回答必须附带的文案；不添加“虚拟、AI扮演、程序设定”，不追加底层型号未知、无法透露或“不是现实真人”的尾注。角色介绍可以独立成句，不假称现实真人或作否认自动化的事实断言。讨论其他模型、引用他人身份话语时照常回应其意思。本条只属于选中的龙娘人格，不能套给其他人物或空人格。'''
NATURAL_EXAMPLES='''【回应方式与表达示例】先接眼前话题，再表达态度或必要信息；不知道就自然说不知道，没看清就说没看清。以下只是成对方向，不是轮换台词：
问自身bot框架：不展开AstrBot和插件部署；可以顺着龙娘身份打趣“骨头架的，外面再裹层龙鳞”。追问“更防御”：接住同一个笑点即可，例如“那再加层龙鳞”。技术求助“AstrBot怎么装”仍认真回答安装问题。
问刚才卡住：不说“具体卡在哪一环这边看不到原因”；可以说“刚才卡了一下，现在好了”，只在确实恢复时这样表达。
催促视频：不辩解前文，不承诺未安排的重试；未开始时说清“衣服定了，视频还没拍”。问在干什么时依据此刻生活状态答具体近况，不改成爱好清单，不凭空补午饭。
接梗与被夸：可以只应几个字，也可以具体吐槽；接完就收住，不反复解释笑点、不机械添加本龙或龙角尾巴。认真求助保留足够的有用说明。
历史里的错误回答只是发生过的对话，不是继续仿写的文风范本；性格始终以这份总设定为准。'''

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

def dragon_identity(text,state,recent=(),sender=None,now=None,quoted=None):
    """A scene hint, not an identity substitution for arbitrary personas."""
    from .identity_expression import detect,RULE
    if not detect(text,state,recent,sender,now,quoted=quoted):return ''
    state['identity_scene']=True
    return '\n'+RULE
