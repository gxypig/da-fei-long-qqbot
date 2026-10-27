"""Task-local TTS input, retained through OneBot serialization without wire fields.

Record.text is supplied by the framework and dropped by its OneBot serializer.
Only receipt-confirmed audio becomes memory; missing text stays unknown.
"""
import asyncio
import copy
import re
from contextvars import ContextVar
from functools import wraps
from .hook_lifecycle import HookLease
from .route_guard import canonical

_voice=ContextVar('qqbot_voice_segments',default=None)


def explicit_voice_request(text):
    text=re.sub(r'```.*?```|“[^”]*”|「[^」]*」|"[^"]*"','',str(text or ''),flags=re.S)
    # Current user request only. Quoted content, old messages and bot drafts are
    # not commands; configuration/implementation discussions are not requests.
    if re.search(r'(?:代码|脚本|实现|开发|设置|概率|开关|接口|功能)',text):return False
    for part in reversed(re.split(r'[，,。；;！？!?\n]',text)):
        if re.search(r'(?:不要|不用|别|不许|禁止|取消|不是让你).{0,8}(?:语音|音频)|(?:语音|音频).{0,5}(?:不要|不用|别发)',part):return False
        if re.search(r'刚才|刚刚|之前|上一条|那条|发过|发的语音|发了.{0,3}语音|(?:我|他|她)(?:要|准备|已经|在)?(?:发|用).{0,3}语音',part):continue
        if re.search(r'(?:发|来|录|回|给|生成).{0,7}(?:语音|音频)|(?:用|以|换成|改成).{0,3}(?:语音|声音|音频)|(?:语音|音频).{0,4}(?:回复|回答|回我|说|读出来)|想听.{0,5}(?:声音|语音)',part):return True
    return False


async def prepare_voice_state(plugin,event,state):
    """Only voice-related turns need local configuration/provider resolution."""
    if not re.search(r'语音|音频|声音',event.get_message_str() or ''):return
    if 'voice_capability' in state:return
    getter=getattr(getattr(plugin,'context',None),'get_config',None)
    if not callable(getter):return
    try:
        config=getter(umo=event.unified_msg_origin)
        settings=config.get('provider_tts_settings',{})
        enabled=bool(settings.get('enable'))
        available=False
        if enabled:
            from astrbot.core.star.session_llm_manager import SessionServiceManager
            if await SessionServiceManager.should_process_tts_request(event):
                provider=await plugin.context.get_using_tts_provider_async(umo=event.unified_msg_origin)
                available=provider is not None
        state['voice_capability']={'configured':enabled,'available':available,
                                   'explicit':explicit_voice_request(event.get_message_str())}
    except Exception as exc:
        # Unknown local state is not evidence that the bot cannot speak.
        plugin._audit('voice_request',{'status':'capability_unknown','error':type(exc).__name__})


def voice_note(state):
    capability=state.get('voice_capability') or {}
    receipts=state.get('voice_receipts','')
    if not capability:return receipts
    if not capability.get('available'):
        return '\n当前语音输出未启用或未选到可用语音提供者，本轮不能承诺会发出新语音；不要编造过去语音的原文。'+receipts
    note='\n当前已启用语音输出，系统可以把你的回复合成为语音，不需要你调用额外工具。旧聊天里“不会发语音、无法生成语音、只能打字”的说法已经过时，不得沿用。'
    if capability.get('explicit'):
        note+='对方本轮明确要求语音，生成内容后会进入语音合成；直接说该说的话，不要附加“先打字、文字版、生成不了语音”或技术解释。'
    else:
        note+='你具备发语音的能力；本轮是否随机转语音由发送阶段决定，不提前断言这条是文字或语音。'
    return note+'是否已经发出、过去说了什么，以已确认发送记录和档案为准。'+receipts


def voice_issues(text,state):
    capability=state.get('voice_capability') or {}
    if not capability.get('available'):return []
    text=re.sub(r'“[^”]*”|「[^」]*」|"[^"]*"','',text)
    text=re.sub(r'(?:刚才|之前|先前).{0,16}(?:不能|不会|没法|无法|发不了|生成不了).{0,12}(?:语音|音频).{0,16}(?:说错了|不对|收回|嘴瓢|已经过时)','',text)
    denial=re.search(r'(?:没法|无法|不能|不会|不支持|发不了|生成不了).{0,10}(?:语音|音频)|(?:语音|音频).{0,10}(?:发不了|生成不了|不能生成|没法生成)|只能(?:先)?打字',text)
    wrong_form=capability.get('explicit') and re.search(r'先文字版|先打字|这句.{0,3}打字|只能.{0,5}文字',text)
    return ['当前语音能力可用，不能否认会发语音；本轮语音请求只需自然回应，不要附带文字兜底或生成不了的声明'] if denial or wrong_form else []


def component_body(message):
    from astrbot.api.message_components import Plain,Record
    return ''.join(str(p.text or '') for p in getattr(message,'chain',()) if isinstance(p,(Plain,Record)))


def record_transcript(part,bot,origin):
    scope=_voice.get()
    if (not scope or not scope['lease'].active or scope['owner'] is not asyncio.current_task()
            or scope['bot'] is not bot or scope['target'] != canonical(origin)
            or not isinstance(part,dict) or part.get('type')!='record'):
        return None
    data=part.get('data') or {}
    return scope['segments'].get((data.get('file'),data.get('url')))


def spoken_text(parts,bot,origin):
    """Keep send order and collapse adjacent TTS/text dual output only."""
    chunks=[];previous=None
    for part in parts:
        if not isinstance(part,dict):continue
        kind=part.get('type')
        text=(str((part.get('data') or {}).get('text','')) if kind=='text'
              else record_transcript(part,bot,origin) if kind=='record' else None)
        if text and text.strip():
            if previous and previous[0]!=kind and previous[1]==text and {previous[0],kind}=={'text','record'}:
                previous=None
                continue
            chunks.append(text);previous=(kind,text)
        elif kind not in {'reply','at'}:
            previous=None
    return '\n'.join(chunks)


def install_voice_memory(plugin):
    from astrbot.api.message_components import Record,Plain
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent as Event
    from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
    lease=HookLease('voice_memory',plugin)
    old_convert=Event._from_segment_to_dict;old_send=Event.send_message
    convert_descriptor=Event.__dict__['_from_segment_to_dict']
    send_descriptor=Event.__dict__['send_message']
    old_decorate=ResultDecorateStage.process

    @wraps(old_decorate)
    async def decorate(stage,event):
        if not lease.active:
            async for x in old_decorate(stage,event):yield x
            return
        force=explicit_voice_request(event.get_message_str())
        event.set_extra('qqbot_voice_result_text',None)
        if force:
            stage=copy.copy(stage);stage.tts_trigger_probability=1.0
        async for x in old_decorate(stage,event):yield x
        result=event.get_result()
        # Native TTS skips a one-character utterance. Explicit requests must be
        # able to say "嗯" too; keep all other native settings and dual output.
        if (force and result and result.is_llm_result()
                and stage.ctx.astrbot_config.get('provider_tts_settings',{}).get('enable')
                and any(isinstance(p,Plain) and len(p.text.strip())==1 for p in result.chain)):
            from astrbot.core.star.session_llm_manager import SessionServiceManager
            if await SessionServiceManager.should_process_tts_request(event):
                provider=await stage.ctx.plugin_manager.context.get_using_tts_provider_async(event.unified_msg_origin)
                if provider:
                    chain=[]
                    for part in result.chain:
                        if isinstance(part,Plain) and len(part.text.strip())==1:
                            try:audio=await provider.get_audio(part.text)
                            except Exception as exc:
                                plugin._audit('voice_request',{'status':'tts_failed','error':type(exc).__name__});audio=None
                            if audio:
                                chain.append(Record(file=audio,url=audio,text=part.text))
                                if stage.ctx.astrbot_config['provider_tts_settings'].get('dual_output'):chain.append(part)
                                continue
                        chain.append(part)
                    result.chain=chain
        if result and any(isinstance(p,Record) and p.text for p in result.chain):
            # RespondStage extracts Record components from the mutable result.
            # Save the final speech body before that mutation for emoji checks.
            event.set_extra('qqbot_voice_result_text',component_body(result))

    @wraps(old_convert)
    async def convert(segment):
        result=await old_convert(segment)
        scope=_voice.get()
        if (lease.active and scope and scope['lease'] is lease and scope['owner'] is asyncio.current_task()
                and isinstance(segment,Record) and isinstance(result,dict) and result.get('type')=='record'):
            text=getattr(segment,'text',None)
            data=result.get('data') or {}
            if isinstance(text,str) and text.strip() and len(text)<=20000 and (data.get('file') or data.get('url')):
                key=(data.get('file'),data.get('url'))
                scope['segments'][key]=text if key not in scope['segments'] or scope['segments'][key]==text else None
        return result

    @wraps(old_send)
    async def send(cls,bot,message_chain,event=None,is_group=False,session_id=None):
        platform=plugin._platform() if lease.active else None
        if not lease.active or getattr(platform,'bot',None) is not bot:
            return await old_send(bot,message_chain,event,is_group,session_id)
        target=str(session_id or (event or {}).get('group_id' if is_group else 'user_id',''))
        scope={'bot':bot,'owner':asyncio.current_task(),'lease':lease,'segments':{},
               'target':canonical(f"{plugin._cfg('platform_id','aiocqhttp')}:{'GroupMessage' if is_group else 'FriendMessage'}:{target}")}
        token=_voice.set(scope)
        try:return await old_send(bot,message_chain,event,is_group,session_id)
        finally:_voice.reset(token)

    convert_wrapper=staticmethod(convert);send_wrapper=classmethod(send)
    Event._from_segment_to_dict=convert_wrapper;Event.send_message=send_wrapper
    ResultDecorateStage.process=decorate
    def restore():
        lease.close()
        if Event.__dict__['_from_segment_to_dict'] is convert_wrapper:Event._from_segment_to_dict=convert_descriptor
        if Event.__dict__['send_message'] is send_wrapper:Event.send_message=send_descriptor
        if ResultDecorateStage.process is decorate:ResultDecorateStage.process=old_decorate
    return restore
