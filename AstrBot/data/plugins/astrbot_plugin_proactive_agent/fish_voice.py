"""Strict, bounded Fish calls using AstrBot's native provider and WAV pipeline."""
import asyncio
import contextvars
import hashlib
import re
import time
import os
import wave
from pathlib import Path
from functools import wraps
from .hook_lifecycle import HookLease

FREE='s2.1-pro-free'
PAID='s2.1-pro'
BASE='https://api.fish.audio/v1'
_call=contextvars.ContextVar('qqbot_voice_call',default=None)

class VoiceFailure(Exception):
    def __init__(self,reason):self.reason=reason;super().__init__(reason)

def validate(provider):
    config=provider.provider_config
    model=config.get('model')
    headers=getattr(provider,'headers',{})
    matching=[v for k,v in headers.items() if str(k).lower()=='model']
    if model not in {FREE,PAID} or matching!=[model] or provider.get_model()!=model:
        raise VoiceFailure('model_header_mismatch')
    if str(config.get('api_base','')).rstrip('/')!=BASE or str(provider.api_base).rstrip('/')!=BASE:
        raise VoiceFailure('unexpected_api_base')
    ref=config.get('fishaudio-tts-reference-id')
    if not isinstance(ref,str) or not re.fullmatch('[0-9a-f]{32}',ref) or provider.reference_id!=ref:
        raise VoiceFailure('invalid_reference_id')
    if not config.get('api_key'):raise VoiceFailure('missing_api_key')
    return model,ref

def error_kind(exc):
    if isinstance(exc,VoiceFailure):return exc.reason,False
    if isinstance(exc,(asyncio.TimeoutError,TimeoutError)):return 'timeout',False
    # Never keep upstream error bodies (they can contain credentials or input).
    match=re.search(r'\b(400|401|402|403|404|408|422|429|5\d\d)\b',str(exc))
    code=int(match[1]) if match else None
    return ({401:'authentication',402:'balance',403:'permission',404:'voice_unavailable',429:'rate_limit'}.get(code,
            'service_error' if code and code>=500 else 'request_failed'),code==429 or bool(code and code>=500))

def normalize_wav(path):
    """Native streaming WAV uses an unknown RIFF length. Finalize, never trim."""
    path=Path(path)
    if path.stat().st_size>32*1024*1024:raise VoiceFailure('audio_size_limit')
    with wave.open(str(path),'rb') as source:
        channels,width,rate,declared,compression,_=source.getparams()
        if compression!='NONE' or width not in {1,2,3,4} or channels not in {1,2}:raise VoiceFailure('invalid_wav')
        data=source.readframes(min(declared,32*1024*1024//(channels*width)))
    actual=len(data)//(channels*width)
    if not actual or len(data)%(channels*width):raise VoiceFailure('empty_or_partial_audio')
    if actual<declared and declared*channels*width<0x7f000000:raise VoiceFailure('truncated_audio')
    if declared!=actual:
        temporary=path.with_suffix('.finalizing.wav')
        try:
            with wave.open(str(temporary),'wb') as target:
                target.setnchannels(channels);target.setsampwidth(width);target.setframerate(rate);target.writeframes(data)
            os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)
    return {'duration_seconds':actual/rate,'sample_rate':rate,'channels':channels,'header_finalized':declared!=actual}

async def synthesize(provider,text,*,explicit=False,budget=None,expression=False,instruction='',intensity='lively'):
    """One total deadline, including the sole possible 429/5xx retry."""
    from .voice_expression import plan,MODELS
    started=time.monotonic()
    delivery=plan(text,enabled=expression and provider.get_model() in MODELS,instruction=instruction,intensity=intensity)
    spoken=delivery.pop('text')
    scope={'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'bytes':len(spoken.encode('utf-8')),'transcript_bytes':len(text.encode('utf-8')),
        'expression':delivery,'model':str(provider.get_model()),'voice':getattr(provider,'reference_id',None),
        'provider_id':getattr(provider,'provider_config',{}).get('id'),
        'attempts':0,'state':'generating','at':time.time()}
    token=_call.set(scope)
    try:
        async with asyncio.timeout(budget if budget is not None else 15 if explicit else 8):
            audio=await provider.get_audio(spoken)
            if audio and str(provider.get_model()) in {FREE,PAID}:
                scope.update(await asyncio.to_thread(normalize_wav,audio))
        if not audio:raise VoiceFailure('empty_audio')
        scope.update(state='synthesized',elapsed_ms=round((time.monotonic()-started)*1000))
        return audio,dict(scope)
    except Exception as exc:
        reason,_=error_kind(exc);scope.update(state='failed',error=reason,elapsed_ms=round((time.monotonic()-started)*1000))
        return None,dict(scope)
    finally:
        _call.reset(token)
        from .chain_metrics import add,span
        ended=time.monotonic()
        add('voice_synthesis_ms',(ended-started)*1000)
        span('voice_synthesis',started,ended)

def install_fish_guard(plugin):
    from astrbot.core.provider.sources.fishaudio_tts_api_source import ProviderFishAudioTTSAPI
    lease=HookLease('fish_free_guard',plugin);original=ProviderFishAudioTTSAPI.get_audio
    @wraps(original)
    async def guarded(provider,text):
        if not lease.active:return await original(provider,text)
        model,ref=validate(provider)
        scope=_call.get();own=scope is None
        if own:scope={'attempts':0}
        async def perform():
            for attempt in range(2):
                validate(provider)  # Recheck immediately before every network request.
                scope['attempts']+=1
                try:return await original(provider,text)
                except Exception as exc:
                    reason,retry=error_kind(exc)
                    if not retry or attempt:raise VoiceFailure(reason) from None
                    await asyncio.sleep(.2)
        if own:
            async with asyncio.timeout(15):return await perform()
        return await perform()
    ProviderFishAudioTTSAPI.get_audio=guarded
    def restore():
        lease.close()
        if ProviderFishAudioTTSAPI.get_audio is guarded:ProviderFishAudioTTSAPI.get_audio=original
    return restore

def paragraphs(text,limit=200):
    """Preserve every character and order, preferring natural boundaries."""
    remaining=text;out=[]
    while len(remaining)>limit:
        matches=list(re.finditer(r'\n|[。！？!?；;]',remaining[:limit]))
        end=matches[-1].end() if matches else limit
        out.append(remaining[:end]);remaining=remaining[end:]
    if remaining:out.append(remaining)
    return out

async def transform(plugin,stage,event,*,force=False,forbidden=False):
    from astrbot.api.message_components import Plain,Record
    result=event.get_result()
    if event.get_extra('qqbot_send_path')=='template_receipt':return
    if forbidden or not result or not result.is_llm_result():return
    cfg=stage.ctx.astrbot_config.get('provider_tts_settings',{})
    if not cfg.get('enable'):return
    text=''.join(p.text for p in result.chain if isinstance(p,Plain))
    if not text.strip() or any(isinstance(p,Record) for p in result.chain):return
    import random
    if not force and (len(text)>200 or random.random()>=float(getattr(stage,'tts_trigger_probability',cfg.get('trigger_probability',.05)))):return
    from astrbot.core.star.session_llm_manager import SessionServiceManager
    if not await SessionServiceManager.should_process_tts_request(event):return
    provider=await stage.ctx.plugin_manager.context.get_using_tts_provider_async(event.unified_msg_origin)
    if provider is None:return
    chain=[];texts=[];records=[]
    expression=cfg.get('fish_expression_enabled',True)
    deadline=time.monotonic()+(15 if force else 8)
    async def flush():
        if not texts:return
        merged=''.join(texts);texts.clear()
        for paragraph in paragraphs(merged) if force else [merged]:
            budget=15 if force and len(text)>200 else max(.001,deadline-time.monotonic())
            path,meta=await synthesize(provider,paragraph,explicit=force,budget=budget,
                expression=expression,instruction=event.get_message_str(),intensity=cfg.get('fish_expression_intensity','lively'))
            records.append(meta)
            if path:
                chain.append(Record(file=path,url=path,text=paragraph))
                if cfg.get('dual_output'):chain.append(Plain(paragraph))
            else:chain.append(Plain(paragraph))
    for part in result.chain:
        if isinstance(part,Plain):texts.append(part.text)
        else:await flush();chain.append(part)
    await flush();result.chain=chain
    event.set_extra('qqbot_voice_synthesis',records)
    # Local record is synthesis telemetry, never a delivery fact.
    plugin._voice_recent=(getattr(plugin,'_voice_recent',[])+records)[-20:]
    for meta in records:plugin._audit('voice_synthesis',meta)

async def replace_stale_audio(plugin,event,parts,text,origin):
    """A changed approved transcript invalidates audio, with at most one resynthesis."""
    from astrbot.api.message_components import Record
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent as Event
    attempted=event.get_extra('qqbot_voice_resynthesized') if event else True
    audio=None
    if not attempted:
        event.set_extra('qqbot_voice_resynthesized',True)
        provider=await plugin.context.get_using_tts_provider_async(umo=origin)
        history=event.get_extra('qqbot_voice_synthesis') or []
        # Do not silently change voice/model if settings changed in flight.
        previous=next((r for r in reversed(history) if r.get('state')=='synthesized'),None)
        if (provider and previous and previous.get('model')==provider.get_model()
                and previous.get('voice')==getattr(provider,'reference_id',None)):
            # Recompute from the approved rewrite; stale offsets cannot leak in.
            cfg=plugin.context.get_config().get('provider_tts_settings',{})
            audio,meta=await synthesize(provider,text,explicit=True,
                expression=cfg.get('fish_expression_enabled',True),instruction=event.get_message_str(),intensity=cfg.get('fish_expression_intensity','lively'))
            event.set_extra('qqbot_voice_synthesis',history+[meta])
    replacement=(await Event._from_segment_to_dict(Record(file=audio,url=audio,text=text))) if audio else {'type':'text','data':{'text':text}}
    out=[];inserted=False
    for part in parts:
        if part.get('type') in {'record','text'}:
            if not inserted:out.append(replacement);inserted=True
        else:out.append(part)
    if not inserted:out.append(replacement)
    if event:event.set_extra('qqbot_voice_result_text',text if audio else None)
    return out
