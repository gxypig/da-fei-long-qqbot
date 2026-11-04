"""Authenticated native-config voice controls. Secrets are never returned."""
import asyncio,base64,copy,hashlib,json,re,time
from pathlib import Path
from quart import request,jsonify
from .fish_voice import FREE,PAID,BASE,validate,synthesize,error_kind

def profiles(config):
    values=config.get('provider',[])
    return {mode:next((p for p in values if p.get('type')=='fishaudio_tts_api' and p.get('model')==model),None)
            for mode,model in (('free',FREE),('paid',PAID))} | {
        'mimo':next((p for p in values if p.get('type')=='mimo_tts_api'),None)}

def revision(config):
    values={'settings':config.get('provider_tts_settings',{}),'profiles':profiles(config)}
    return hashlib.sha256(json.dumps(values,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def view(plugin):
    config=plugin.context.get_config();values=profiles(config);selected=config.get('provider_tts_settings',{}).get('provider_id')
    mode=next((k for k,v in values.items() if v and v.get('id')==selected),'other')
    current=next((p for p in config.get('provider',[]) if p.get('id')==selected),{})
    recent=getattr(plugin,'_voice_recent',[])[-20:]
    return {'revision':revision(config),'mode':mode,'enabled':config.get('provider_tts_settings',{}).get('enable',False),
        'voice':current.get('display_name') or current.get('fishaudio-tts-character') or current.get('id'),
        'model':current.get('model'),'key_configured':bool(current.get('api_key')),
        'available':{k:bool(v) for k,v in values.items()},'recent':recent,
        'price':{'free':0,'paid_usd_per_million_utf8_bytes':15},
        'usage_note':'按请求UTF-8字节估算，失败请求及重试是否计费以服务商账单为准；合成成功不等于已发送',
        'estimated_paid_usd':sum(r.get('bytes',0)*max(1,r.get('attempts',1))*15/1000000 for r in recent if r.get('model')==PAID),
        'auto_probability':config.get('provider_tts_settings',{}).get('trigger_probability',.05)}

def register_voice_panel(plugin):
    lock=asyncio.Lock();audition_lock=asyncio.Lock();submitted={}
    async def status():return jsonify(status='ok',data=view(plugin))
    async def action():
        body=await request.get_json(silent=True)
        if not isinstance(body,dict) or request.content_length and request.content_length>8192:return jsonify(status='error',message='请求格式错误'),400
        kind=body.get('action');mode=body.get('mode','free')
        if mode not in {'free','paid','mimo'} or kind not in {'check','audition','switch'}:return jsonify(status='error',message='语音操作不支持'),400
        if mode=='paid' and body.get('confirm_paid') is not True:return jsonify(status='error',message='付费模式需明确确认：15美元／百万UTF-8字节；免费失败不会自动转付费'),400
        config=plugin.context.get_config();profile=profiles(config).get(mode)
        if not profile:return jsonify(status='error',message='请先在AstrBot语音提供商中配置该模式'),400
        try:
            if kind=='switch':
                async with lock:
                    if body.get('revision')!=revision(config):return jsonify(status='error',message='语音配置已变化，请刷新后再切换'),409
                    if getattr(plugin,'_work',None) and plugin._work.busy():return jsonify(status='error',message='正在回复，请稍后切换；当前任务继续使用原音色'),409
                    manager=plugin.context.provider_manager
                    if not profile.get('enable'):
                        profile={**profile,'enable':True};await manager.update_provider(profile['id'],profile)
                    instance=manager.inst_map.get(profile['id'])
                    if not instance:return jsonify(status='error',message='语音提供商尚未加载'),503
                    if mode!='mimo':validate(instance)
                    previous=copy.deepcopy(config.get('provider_tts_settings',{}))
                    try:
                        config.setdefault('provider_tts_settings',{}).update(enable=True,provider_id=profile['id'])
                        if config.save_config() is False:raise OSError('save failed')
                    except Exception:
                        config['provider_tts_settings']=previous;raise
                    manager.provider_tts_settings=config['provider_tts_settings'];manager.curr_tts_provider_inst=instance
                    return jsonify(status='ok',data=view(plugin))
            if kind=='check':
                if mode=='mimo':return jsonify(status='ok',data={'state':'configured','mode':mode})
                ref=profile.get('fishaudio-tts-reference-id','')
                if not re.fullmatch('[0-9a-f]{32}',ref):return jsonify(status='error',message='音色ID格式不正确'),400
                import httpx
                async with httpx.AsyncClient(proxy=profile.get('proxy') or None,timeout=8) as client:
                    response=await client.get('https://api.fish.audio/model/'+ref);response.raise_for_status();data=response.json()
                # Public metadata only; neither account info nor API key is sent.
                return jsonify(status='ok',data={k:data.get(k) for k in ('_id','title','state','visibility','languages')})
            if audition_lock.locked():return jsonify(status='error',message='上一条试听仍在生成，请稍候'),409
            text=body.get('text','本龙在呢，听得到吗')
            if not isinstance(text,str) or not text.strip() or len(text)>200:return jsonify(status='error',message='试听文字请填1至200字'),400
            reqid=body.get('request_id')
            if not isinstance(reqid,str) or not re.fullmatch('[a-zA-Z0-9_-]{8,80}',reqid):return jsonify(status='error',message='缺少试听请求编号'),400
            if reqid in submitted:return jsonify(status='error',message='本次试听已经提交，请勿重复生成'),409
            async with audition_lock:
                if reqid in submitted:return jsonify(status='error',message='本次试听已经提交，请勿重复生成'),409
                submitted[reqid]=time.time()
                while len(submitted)>100:submitted.pop(next(iter(submitted)))
                if mode in {'free','paid'}:
                    from astrbot.core.provider.sources.fishaudio_tts_api_source import ProviderFishAudioTTSAPI
                    provider=ProviderFishAudioTTSAPI(copy.deepcopy(profile),{})
                    validate(provider)
                else:provider=plugin.context.provider_manager.inst_map.get(profile['id'])
                if provider is None:return jsonify(status='error',message='语音提供商未加载'),503
                path,meta=await synthesize(provider,text,explicit=True)
                plugin._voice_recent=(getattr(plugin,'_voice_recent',[])+[meta])[-20:]
                if not path:return jsonify(status='error',message='试听未生成：'+meta.get('error','unknown'),data=meta),503
                data=await asyncio.to_thread(Path(path).read_bytes)
                if len(data)>8*1024*1024:return jsonify(status='error',message='试听音频过大'),400
                return jsonify(status='ok',data={'mime':'audio/wav','audio':base64.b64encode(data).decode('ascii'),'metrics':meta})
        except asyncio.CancelledError:raise
        except Exception as exc:
            reason,_=error_kind(exc)
            return jsonify(status='error',message='语音操作未完成：'+reason),503
    for name,handler,methods in [('panel_voice',status,['GET']),('panel_voice_action',action,['POST'])]:
        plugin.context.register_web_api('/astrbot_plugin_proactive_agent/'+name,handler,methods,'语音设置与本地试听')
