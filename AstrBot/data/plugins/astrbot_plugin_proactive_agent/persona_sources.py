"""Explicit local sources: excludes chat history and unselected personas."""
import ast
from pathlib import Path

def default_persona_id(plugin):
    from astrbot.core.persona_mgr import SYSTEM_DEFAULT_PERSONA_ID
    cfg=plugin.context.get_config();runner=cfg.get('agent_runner',{});settings=runner.get('config',{})
    return (settings.get('persona',{}).get('persona_id',SYSTEM_DEFAULT_PERSONA_ID)
        if runner.get('runner_type')=='local' else settings.get('persona_id',SYSTEM_DEFAULT_PERSONA_ID))

def active_sources(plugin):
    from .dragon_character import CHARACTER_POLICY
    from .cheeky_tone import TONE
    from .chat_style_policy import CHAT_STYLE_POLICY
    from .conversation_style import STYLE
    from .conversation_review import REVIEW_SYSTEM
    from .local_recall import BRIEF_RULE
    from .life_state import COMPOSE_SYSTEM
    rows=[]
    def add(id_,label,text,path,scene):
        if isinstance(text,str) and text.strip():rows.append({'id':id_,'label':label,'text':text,'path':path,'scene':scene,'priority':1})
    for id_,label,value,file in [('character','角色补充',CHARACTER_POLICY,'dragon_character.py'),('tone','口吻补充',TONE,'cheeky_tone.py'),
        ('chat','聊天约定',CHAT_STYLE_POLICY,'chat_style_policy.py'),('conversation','会话补充',STYLE,'conversation_style.py'),
        ('review','审阅角色约定',REVIEW_SYSTEM,'conversation_review.py'),('brief','长短表达习惯',BRIEF_RULE,'local_recall.py'),
        ('life','主动表达',COMPOSE_SYSTEM,'life_state.py')]:add(id_,label,value,file,id_)
    add('configured_proactive','已保存的主动人格',plugin._cfg('personality',''),'plugin-config:personality','proactive')
    base=Path(__file__).parent
    import json
    imported=base/'persona_import_sources.json'
    if imported.is_file():rows.extend(json.loads(imported.read_text('utf-8')))
    adapters=[(base/'dragon_character.py',['photo_caption'],'image'),(base/'visual_intent.py',['generation_note'],'image'),
        ]
    for path,names,scene in adapters:
        if not path.is_file():continue
        tree=ast.parse(path.read_text('utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names:
                texts=list(dict.fromkeys(n.value for n in ast.walk(node) if isinstance(n,ast.Constant) and isinstance(n.value,str) and len(n.value)>5))
                add(scene+':'+node.name,node.name,'\n'.join(texts),str(path.relative_to(base.parent)),scene)
    try:
        for star in plugin.context.get_all_stars():
            instance=getattr(star,'star_cls',None)
            if instance and 'astrbot_plugin_smart_imagechat_hub' in instance.__class__.__module__:
                import json
                value=getattr(instance,'config',{}).get('send_image_style',{})
                if value:add('gallery:saved','图库已保存表达',json.dumps(value,ensure_ascii=False),'plugin-config:send_image_style','gallery')
    except (AttributeError,TypeError):pass
    return rows
