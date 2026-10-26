"""Native authenticated admin API for queue and independently reversible features."""
import asyncio
import hashlib
import json
from quart import jsonify, request
from .reply_jobs import QueueConflict

FEATURES={'reply_queue_enabled':False,'reply_queue_concurrency':2,
          'adaptive_context_enabled':False,'image_memory_enabled':False,
          'ai_profiles_enabled':False,'ai_probe_enabled':False}


def feature_settings(plugin):
    values={k:plugin._cfg(k,v) for k,v in FEATURES.items()}
    revision=hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()
    return {'values':values,'revision':revision}


def register_fullchain_panel(plugin):
    lock=asyncio.Lock()

    async def queue_get():
        try:
            version=request.args.get('version');version=int(version) if version is not None else None
            data=plugin._reply_queue.snapshot(version,request.args.get('instance'),
                int(request.args.get('offset',0)),int(request.args.get('limit',50)))
            data['enabled']=plugin._cfg('reply_queue_enabled',False)
            return jsonify(status='ok',data=data)
        except (ValueError,TypeError):return jsonify(status='error',message='分页或版本格式错误'),400

    async def queue_action():
        try:
            body=await request.get_json(silent=True)
            if request.content_length and request.content_length>4096:raise ValueError('操作内容过长')
            # No await between validating the version and changing admission order.
            data=plugin._reply_queue.action(body)
            return jsonify(status='ok',data=data)
        except QueueConflict as exc:
            return jsonify(status='error',message=str(exc),data=plugin._reply_queue.snapshot()),409
        except (ValueError,TypeError):return jsonify(status='error',message='队列操作格式错误'),400

    async def features():
        if request.method=='GET':return jsonify(status='ok',data=feature_settings(plugin))
        body=await request.get_json(silent=True)
        if not isinstance(body,dict) or not isinstance(body.get('values'),dict):
            return jsonify(status='error',message='设置格式不完整'),400
        values=body['values']
        if set(values)!=set(FEATURES) or any(type(values[k]) is not type(v) for k,v in FEATURES.items()):
            return jsonify(status='error',message='设置字段或类型不完整'),400
        if not 1<=values['reply_queue_concurrency']<=8:
            return jsonify(status='error',message='回复并发须为1至8'),400
        async with lock:
            if body.get('revision')!=feature_settings(plugin)['revision']:
                return jsonify(status='error',message='设置已变化，请刷新'),409
            previous={k:plugin.config.get(k) for k in FEATURES};missing=set(FEATURES)-set(plugin.config)
            try:
                plugin.config.update(values)
                if plugin.config.save_config() is False:raise OSError('save rejected')
            except Exception:
                for k,v in previous.items():
                    if k in missing:plugin.config.pop(k,None)
                    else:plugin.config[k]=v
                return jsonify(status='error',message='保存失败，内存设置已恢复'),500
            plugin._reply_queue.pump()
            return jsonify(status='ok',data=feature_settings(plugin))

    async def profiles():
        service=getattr(plugin,'_ai_profiles',None)
        if service is None:return jsonify(status='error',message='识别服务尚未就绪'),503
        try:
            return jsonify(status='ok',data=service.snapshot(request.args.get('q','')[:100],
                request.args.get('state',''),int(request.args.get('offset',0))))
        except ValueError:return jsonify(status='error',message='分页参数错误'),400

    async def live_messages():
        origin=request.args.get('origin','')
        if len(origin)>300:return jsonify(status='error',message='会话标识过长'),400
        try:return jsonify(status='ok',data=plugin._live_journal.delta(origin,int(request.args.get('after',0)),request.args.get('instance')))
        except (ValueError,TypeError):return jsonify(status='error',message='会话或游标格式错误'),400

    async def profile_action():
        service=getattr(plugin,'_ai_profiles',None)
        if service is None:return jsonify(status='error',message='识别服务尚未就绪'),503
        try:
            body=await request.get_json(silent=True)
            data=service.action(body)
            if body.get('action')!='settings':
                from .ai_profiles import identity
                await service.store.flush(identity(body['platform'],body['account']))
            await service.store.flush('__control__')
            return jsonify(status='ok',data=data)
        except QueueConflict as exc:return jsonify(status='error',message=str(exc),data=service.snapshot()),409
        except (ValueError,TypeError) as exc:return jsonify(status='error',message=str(exc)),400
        except (TimeoutError,OSError):return jsonify(status='error',message='设置已进入保存队列，请刷新确认，勿换请求ID重复操作'),503

    async def drain():
        # Stop producers before draining their already admitted replies. The
        # durable pre-send reservation still covers cancellation during I/O.
        companions=[plugin]
        getter=getattr(plugin.context,'get_registered_star',None)
        meta=getter('astrbot_plugin_bili_scroll') if getter else None
        if meta and meta.activated:companions.append(meta.star_cls)
        tasks=[]
        for owner in companions:
            for name in ('_task','_fixed_task','_account_task'):
                task=getattr(owner,name,None)
                if task and not task.done():task.cancel();tasks.append(task)
            task=getattr(getattr(owner,'actions',None),'_task',None)
            if task and not task.done():task.cancel();tasks.append(task)
        if tasks:await asyncio.wait(tasks,timeout=.5)
        await plugin._reply_queue.drain(8)
        stores=[];waiters=[]
        for owner in companions:
            for name in ('_delivery','_image_memory','_ai_profiles'):
                store=getattr(getattr(owner,name,None),'store',None)
                if store:stores.append(store)
            store=getattr(owner,'_share_store',None)
            if store:stores.append(store)
            for name in ('_learning','_sent_memory'):
                persistence=getattr(getattr(owner,name,None),'_persistence',None)
                if persistence:waiters.append(persistence.flush())
        async def saved(store):
            while store.pending:await store.flush(next(iter(store.pending)))
        waiters.extend(saved(store) for store in stores)
        for name in ('_live_journal','_audit_writer'):
            queue=getattr(getattr(plugin,name,None),'queue',None)
            if queue:waiters.append(queue.join())
        if waiters:
            try:
                async with asyncio.timeout(3):await asyncio.gather(*waiters,return_exceptions=True)
            except TimeoutError:pass
        return jsonify(status='ok',data={'drained':not plugin._reply_queue.active,'replay':False,
            'pending_persistence':sum(len(store.pending) for store in stores)})

    async def model_probe():
        from .work_scheduler import model_work
        work=getattr(plugin,'_work',None)
        if work and work.busy():return jsonify(status='ok',data={'state':'deferred','reason':'正在处理聊天，能力检查留待空闲'})
        try:
            with model_work('background'):
                async with asyncio.timeout(25):
                    provider=await plugin.context.get_using_provider_async()
                    response=await provider.text_chat(prompt='健康检查，只回复OK',max_tokens=16)
            return jsonify(status='ok',data={'state':'verified' if getattr(response,'completion_text','').strip() else 'empty'})
        except asyncio.CancelledError:raise
        except Exception as exc:return jsonify(status='error',message=type(exc).__name__),503

    for name,handler,methods in [('panel_reply_queue',queue_get,['GET']),('panel_reply_queue_action',queue_action,['POST']),
        ('panel_drain',drain,['POST']),
        ('panel_model_probe',model_probe,['POST']),
        ('panel_live_messages',live_messages,['GET']),
        ('panel_fullchain_settings',features,['GET','POST']),('panel_ai_profiles',profiles,['GET']),
        ('panel_ai_profile_action',profile_action,['POST']),('panel_ai_profile_settings',profile_action,['POST'])]:
        plugin.context.register_web_api('/astrbot_plugin_proactive_agent/'+name,handler,methods,'大肥龙控制面板')
