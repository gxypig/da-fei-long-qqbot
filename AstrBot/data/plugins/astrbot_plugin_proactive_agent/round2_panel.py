"""Authenticated projections of the one active scheduler; preview never mutates it."""
import time
import json
from quart import jsonify,request
from .fixed_schedule import plans,STATE_KEY


async def proactive_status(p):
    from .reading_agency import enabled
    autonomous=enabled(p)
    state=await p.get_kv_data(STATE_KEY,{})
    platform=p._cfg('platform_id','aiocqhttp');rules=[];scopes={}
    for lane,kind in [('group','GroupMessage'),('private','FriendMessage')]:
        selected=p._id_set(p._cfg(lane+'_ids',''))
        fixed=p._id_set(p._cfg('fixed_'+lane+'_ids','')) if p._cfg('fixed_messages_enabled',False) else set()
        actual=[{'id':uid,'name':row['name'],'availability':p._targets.status(f'{platform}:{kind}:{uid}')}
            for uid,row in p._targets.contacts.get(kind,{}).items() if (not selected or uid in selected or autonomous and uid in fixed) and (autonomous or uid not in fixed)]
        for uid in selected-fixed-{r['id'] for r in actual}:
            actual.append({'id':uid,'name':'当前列表未找到','availability':p._targets.status(f'{platform}:{kind}:{uid}')})
        scopes[lane]={'mode':'selected' if selected else 'all','label':'指定对象' if selected else '全部对象',
            'selected':sorted(selected|fixed if autonomous and selected else selected),'actual':actual,'excludes_fixed':not autonomous and p._cfg('fixed_messages_enabled',False)}
        rules.append({'id':'free:'+lane,'type':'free','lane':lane,'targets':scopes[lane],
            'enabled':p._cfg('enable',True) and p._cfg('discretionary_enabled',True) and p._cfg('allow_'+lane,False),
            'interval':p._cfg('interval_minutes',30)*60,'next_at':getattr(p,'_next_free_check',None),'time_label':'下次检查',
            'status':'quiet' if p._quiet_now() else 'ready','reason':'空白白名单表示全部可用对象；固定勾选不会限制自由模式'})
    rules.append({'id':'free:qzone','type':'free','lane':'qzone','targets':{'label':'QQ动态'},
        'enabled':p._cfg('enable',True) and p._cfg('discretionary_enabled',True) and p._cfg('allow_qzone',True) and not p._cfg('fixed_qzone_enabled',False),
        'interval':p._cfg('interval_minutes',30)*60,'next_at':getattr(p,'_next_free_check',None),'time_label':'下次检查','status':'ready'})
    enabled_plans=plans(p)
    for key,plan in plans(p,include_disabled=True).items():
        lane,target=key.split(':',1);origin='qzone' if lane=='qzone' else f"{platform}:{'GroupMessage' if lane=='group' else 'FriendMessage'}:{target}"
        entry=state.get(key,{});required=p._cfg('fixed_qzone_required' if lane=='qzone' else 'fixed_message_required',False)
        available=p._targets.status(origin)
        rules.append({'id':key,'type':'required' if required else 'opportunity','lane':lane,'origin':origin,'targets':{'label':target or 'QQ动态'},
            'enabled':p._cfg('enable',True) and key in enabled_plans,'interval':list(plan) if isinstance(plan,tuple) else plan,
            'next_at':max(entry.get('due',0),entry.get('retry_at',0)) or None,'time_label':'计划发送' if required else '下次检查',
            'status':entry.get('status','waiting') if available['available'] else 'unavailable',
            'reason':available['reason'] or entry.get('reason',''),'availability':available})
    from .health_projection import runtime_health
    delivery=runtime_health(p)
    if autonomous:
        for rule in rules:
            if rule['type']!='required':rule.update(enabled=False,status='autonomous',next_at=None,reason='原参数已保留，目前由自主决策选择时机与对象')
    extension=getattr(p,'_memory_life',None)
    return {'enabled':p._cfg('enable',True),'quiet_now':p._quiet_now(),'scope':scopes,'rules':rules,
        'agency':extension.agency.snapshot() if extension and hasattr(extension,'agency') else {'enabled':False},
        'life':p._life.activity(),'public_experiences':p._life.public.snapshot(),'reflections':p._life.reflection.snapshot(),'decisions':p._life.decisions(),'targets':p._targets.snapshot(),
        'pending_replies':len(p._reply_queue.pending),'delivery':delivery,
        'next_free_check':getattr(p,'_next_free_check',None),'clock':time.time()}


def register_round2_panel(plugin):
    async def share_experience():
        body=await request.get_json(silent=True)
        if not isinstance(body,dict) or body.get('confirm_public') is not True:return jsonify(status='error',message='请确认这段摘要可以公开'),400
        try:
            row=plugin._life.approve_public_experience(body.get('event_id'),body.get('summary'))
            await plugin._life.store.flush('event:'+row['id'])
            return jsonify(status='ok',data={'id':row['id'],'sent':False})
        except ValueError as exc:return jsonify(status='error',message=str(exc)),400
    async def status():
        await plugin._targets.refresh()
        return jsonify(status='ok',data=await proactive_status(plugin))
    async def preview():
        body=await request.get_json(silent=True) or {}
        if not isinstance(body,dict):return jsonify(status='error',message='预览参数错误'),400
        origin=body.get('origin');mode=body.get('mode','free')
        if mode not in {'free','fixed'} or origin is not None and (not isinstance(origin,str) or len(origin)>250):
            return jsonify(status='error',message='对象或模式不正确'),400
        if origin:
            if origin!='qzone' and (origin.count(':')!=2 or origin.split(':')[1] not in {'GroupMessage','FriendMessage'} or not origin.rsplit(':',1)[-1].isdigit()):
                return jsonify(status='error',message='对象必须是有效的群聊或私聊会话'),400
            required=mode=='fixed' and plugin._cfg('fixed_qzone_required' if origin=='qzone' else 'fixed_message_required',False)
            result=await plugin._life.opportunity(origin,mode,required,dry_run=True)
        else:result=await plugin._life.free_opportunity(dry_run=True)
        return jsonify(status='ok',data=result)
    async def images():
        if request.method=='POST':return jsonify(status='ok',data={'requeued':plugin._image_memory.retry_failed()})
        rows=sorted((r for r in plugin._image_memory.store.rows.values() if 'hash' in r),key=lambda r:r.get('last_seen',0),reverse=True)
        try:offset=max(0,int(request.args.get('offset',0)))
        except ValueError:return jsonify(status='error',message='页码错误'),400
        return jsonify(status='ok',data={**plugin._image_memory.snapshot(),'rows':rows[offset:offset+30],'offset':offset})
    async def summary():
        return jsonify(status='ok',data={'pending':len(plugin._reply_queue.pending),'running':len(plugin._reply_queue.active)})
    async def deliveries():
        try:
            offset=max(0,int(request.args.get('offset',0)))
            before=json.loads(request.args['before']) if request.args.get('before') else None
            if before is not None and (not isinstance(before,list) or len(before)!=2 or type(before[0]) not in (int,float) or not isinstance(before[1],str)):raise ValueError()
        except ValueError:return jsonify(status='error',message='页码错误'),400
        store=plugin._delivery.store
        allkeys=store.select_ids(states={'confirmed','failed','unknown','sending','preparing','submitting'},reverse=True)
        keys=[k for k in allkeys if before is None or (store.index[k].get('at') or 0,k)<tuple(before)]
        page=keys[offset:offset+50];rows=await store.fetch(page)
        memory=plugin._image_memory
        rows=[{**row,'emoji_details':memory.store.rows.get('match:'+str(row.get('emoji_match')),{})} if row.get('emoji_match') else row for row in rows]
        cursor=[store.index[page[-1]].get('at') or 0,page[-1]] if len(keys)>offset+50 and page else None
        return jsonify(status='ok',data={'rows':rows,'total':len(allkeys),'offset':offset,'next_cursor':cursor,'health':plugin._delivery.health()})
    async def image_feedback():
        body=await request.get_json(silent=True) or {}
        if not isinstance(body,dict):return jsonify(status='error',message='反馈格式错误'),400
        try:
            data=plugin._image_memory.feedback(str(body.get('id','')),body.get('kind'))
            return jsonify(status='ok',data=data)
        except ValueError as exc:return jsonify(status='error',message=str(exc)),400
    async def match_image():
        import base64,asyncio
        from pathlib import Path
        decision=plugin._image_memory.store.rows.get('match:'+request.args.get('id',''),{})
        digest=decision.get('content_hash');asset=plugin._image_memory.store.rows.get(digest,{})
        path=asset.get('path')
        if not path:return jsonify(status='error',message='原图尚未缓存'),404
        def read():
            target=Path(path)
            if not target.is_file() or target.stat().st_size>8*1024*1024:raise ValueError('图片过大或已不可用')
            data=target.read_bytes()
            import hashlib
            if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('图片内容已变化')
            mime='image/png' if data.startswith(b'\x89PNG') else 'image/gif' if data.startswith(b'GIF') else 'image/jpeg' if data.startswith(b'\xff\xd8') else 'image/webp'
            return {'mime':mime,'data':base64.b64encode(data).decode('ascii')}
        try:return jsonify(status='ok',data=await asyncio.to_thread(read))
        except (OSError,ValueError):return jsonify(status='error',message='原图不可用'),404
    for name,handler,methods in [('panel_life_public',share_experience,['POST']),('panel_proactive',status,['GET']),('panel_proactive_preview',preview,['POST']),('panel_image_memory',images,['GET','POST']),('panel_reply_summary',summary,['GET']),('panel_delivery',deliveries,['GET']),('panel_image_feedback',image_feedback,['POST']),('panel_match_image',match_image,['GET'])]:
        plugin.context.register_web_api('/astrbot_plugin_proactive_agent/'+name,handler,methods,'第二轮控制面板')
