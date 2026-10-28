"""Share the selected native persona with review; no inference/model request."""
PRIORITY = ('\n口吻优先级：当前会话在AstrBot选定的人格是性格与说话方式的依据；插件的默认性格和例句只是补充参考，'
            '不覆盖管理员已修改的人格，不把旧聊天里的道歉当成永久性格。身份编号、权限、发送回执及真实功能事实仍按程序提供的事实。'
            '只有明确开启旧版反差且本轮apology_required=true时才要求被怼认怂；否则按当前人格接话，不能仅因对方骂你就编造过错或强行认错。')


async def bind_persona(plugin,event,req,state):
    try:
        manager=plugin.context.persona_manager
        config=plugin.context.get_config(umo=event.unified_msg_origin)
        _,persona,_,_=await manager.resolve_selected_persona(
            umo=event.unified_msg_origin,
            conversation_persona_id=getattr(getattr(req,'conversation',None),'persona_id',None),
            platform_name=getattr(event,'get_platform_name',lambda:event.unified_msg_origin.split(':')[0])(),
            provider_settings=config.get('provider_settings',{}))
        prompt=persona.get('prompt',persona.get('system_prompt','')) if isinstance(persona,dict) else ''
        state['persona_prompt']=prompt if isinstance(prompt,str) else ''
    except (AttributeError,KeyError,TypeError):
        state.pop('persona_prompt',None)


def review_persona(state):
    prompt=state.get('persona_prompt','')
    return ('\n管理员为当前会话选定的人格（与正文生成同源，性格判断不得用旧默认模板覆盖）：\n'+prompt if prompt else '')+PRIORITY
