"""Authenticated projections of the one active scheduler; preview never mutates it."""
import time
from quart import jsonify,request
from .fixed_schedule import plans,STATE_KEY


async def proactive_status(p):
    state=await p.get_kv_data(STATE_KEY,{})
    platform=p._cfg('platform_id','aiocqhttp');rules=[];scopes={}
    for lane,kind in [('group','GroupMessage'),('private','FriendMessage')]:
        selected=p._id_set(p._cfg(lane+'_ids',''))
        fixed=p._id_set(p._cfg('fixed_'+lane+'_ids','')) if p._cfg('fixed_messages_enabled',False) else set()
        actual=[{'id':uid,'name':row['name'],'availability':p._targets.status(f'{platform}:{kind}:{uid}')}
            for uid,row in p._targets.contacts.get(kind,{}).items() if (not selected or uid in selected) and uid not in fixed]
        for uid in selected-fixed-{r['id'] for r in actual}:
            actual.append({'id':uid,'name':'当前列表未找到','availability':p._targets.status(f'{platform}:{kind}:{uid}')})
        scopes[lane]={'mode':'selected' if selected else 'all','label':'指定对象' if selected else '全部对象',
            'selected':sorted(selected),'actual':actual,'excludes_fixed':p._cfg('fixed_messages_enabled',False)}
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
    delivery=p._delivery.health();queue=p._reply_queue
    delivery['reply_backlog']=len(queue.pending)
    delivery['oldest_reply_wait_seconds']=max((queue.clock()-queue.jobs[id_].received for id_ in queue.pending),default=0)
    return {'enabled':p._cfg('enable',True),'quiet_now':p._quiet_now(),'scope':scopes,'rules':rules,
        'life':p._life.activity(),'decisions':p._life.decisions(),'targets':p._targets.snapshot(),
        'pending_replies':len(p._reply_queue.pending),'delivery':delivery,
        'next_free_check':getattr(p,'_next_free_check',None),'clock':time.time()}


def register_round2_panel(plugin):
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
        try:offset=max(0,int(request.args.get('offset',0)))
        except ValueError:return jsonify(status='error',message='页码错误'),400
        rows=sorted((r for r in plugin._delivery.store.rows.values() if r.get('state') in {'confirmed','failed','unknown','sending'}),key=lambda r:r.get('at',0),reverse=True)
        return jsonify(status='ok',data={'rows':rows[offset:offset+50],'total':len(rows),'offset':offset,'health':plugin._delivery.health()})
    for name,handler,methods in [('panel_proactive',status,['GET']),('panel_proactive_preview',preview,['POST']),('panel_image_memory',images,['GET','POST']),('panel_reply_summary',summary,['GET']),('panel_delivery',deliveries,['GET'])]:
        plugin.context.register_web_api('/astrbot_plugin_proactive_agent/'+name,handler,methods,'第二轮控制面板')
