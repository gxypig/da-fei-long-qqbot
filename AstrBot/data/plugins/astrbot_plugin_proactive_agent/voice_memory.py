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
        if re.search(r'(?:发|来|录|回|给).{0,7}(?:语音|音频)|(?:用|以|换成|改成).{0,3}(?:语音|声音|音频)(?:.{0,6}(?:说|回|讲|读|答)|(?:吧|呗|可以吗|好吗)?\s*$)|(?:语音|音频).{0,4}(?:回复|回答|回我|说|读出来)|想听.{0,5}(?:声音|语音)',part):return True
    return False


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
