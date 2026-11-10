"""Same authenticated AstrBot bridge; local management never sends a message."""
import asyncio,copy,json,time
from quart import request,jsonify
from .long_memory_store import digest

def register_memory_panel(plugin):
    service=plugin._memory_life
    async def listing():
        if not service.ready:return jsonify(status='error',message='记忆存储暂不可用，后台将自动重试',data={'service':service.snapshot()}),503
        try:
            kind=request.args.get('kind','memory');q=request.args.get('q','')[:200];cursor=request.args.get('cursor','')
            if kind=='memory':data=await asyncio.to_thread(service.db.listing,q,request.args.get('scope',''),int(cursor or 0),25,request.args.get('status','active'))
            else:
                if kind=='experience':rows=[{**r,'id':k} for k,r in plugin._life.store.rows.items() if r.get('record_type') in {'event','reflection'}]
                elif kind=='reading':rows=[r for r in service.store.rows.values() if r.get('type')=='material']
                elif kind=='work':rows=[r for r in service.store.rows.values() if r.get('type') in {'input','read'} and r.get('state')!='done']
                else:raise ValueError('未知列表')
                rows=sorted(rows,key=lambda r:(r.get('at',0),r.get('id','')),reverse=True)
                if q:rows=[r for r in rows if q.casefold() in json.dumps(r,ensure_ascii=False).casefold()]
                if cursor:
                    stamp,key=json.loads(cursor);rows=[r for r in rows if (r.get('at',0),r.get('id',''))<(stamp,key)]
                page=rows[:25];data={'rows':[{k:v for k,v in r.items() if k not in {'content','material','proposals','text'}} for r in page],
                    'next_cursor':json.dumps([page[-1].get('at',0),page[-1]['id']]) if len(rows)>25 else None}
            return jsonify(status='ok',data={**data,'service':service.snapshot()})
        except (ValueError,TypeError,KeyError) as exc:return jsonify(status='error',message=str(exc)),400
    async def detail():
        if not service.ready:return jsonify(status='error',message='记忆存储暂不可用'),503
        try:
            kind=request.args.get('kind','memory');id_=request.args.get('id')
            if kind=='memory':data=await asyncio.to_thread(service.db.detail,id_)
            elif kind=='experience':data=copy.deepcopy(plugin._life.store.rows.get(id_))
            elif kind in {'reading','work'}:data=copy.deepcopy(service.store.rows.get(id_))
            else:raise ValueError('未知详情类型')
            if data is None:return jsonify(status='error',message='记录不存在'),404
            return jsonify(status='ok',data=data)
        except (ValueError,TypeError):return jsonify(status='error',message='详情参数无效'),400
    async def action():
        if not service.ready:return jsonify(status='error',message='记忆存储暂不可用，未执行操作'),503
        try:
            if request.content_length and request.content_length>16000:raise ValueError('请求过长')
            body=await request.get_json(silent=True)
            if not isinstance(body,dict):raise ValueError('操作格式不完整')
            kind=body.get('kind');op=body.get('action');rid=str(body.get('request_id',''))
            if not 8<=len(rid)<=100:raise ValueError('请求编号无效')
            async with service.lock:
                if kind=='memory':
                    data=await asyncio.to_thread(service.db.action,body);service.cache.clear()
                    if op in {'edit','correct'}:
                        for key in service.retire_derived(int(body['id'])):await plugin._life.store.flush(key)
                    if op=='forget':
                        forgotten=set(data.get('forgotten_ids',[int(body['id'])]));event_ids=set();changed_keys=[]
                        for k,r in list(plugin._life.store.rows.items()):
                            if r.get('memory_id') in forgotten:
                                event_ids.add(r.get('id'))
                                changed_keys.append(k)
                                plugin._life.store.put(k,{key:value for key,value in {**r,'summary':'已遗忘','archived_at':service.clock(),'status':'forgotten'}.items() if key not in {'history','source_ids'}})
                        for k,r in list(plugin._life.store.rows.items()):
                            if r.get('record_type')=='reflection' and r.get('event_id') in event_ids:
                                changed_keys.append(k)
                                plugin._life.store.put(k,{**r,'thought':'已遗忘','basis':'','history':[],'previous_thought':None,'next_interest':'','status':'forgotten','archived_at':service.clock()})
                        plugin._life.reflection.refresh()
                        for key in changed_keys:await plugin._life.store.flush(key)
                elif op=='preview':
                    origin=str(body.get('origin',''));candidate=plugin._life.candidate(origin,preview=True,extended_only=True)
                    if candidate:candidate={**candidate,'preview':True}
                    data={'candidate':candidate,**plugin._life.decide(candidate),'sent':False,'preview':True}
                else:
                    request_key='action:'+rid;fingerprint=digest(body);saved=service.store.rows.get(request_key)
                    if saved:
                        if saved['fingerprint']!=fingerprint:raise RuntimeError('version_conflict')
                        return jsonify(status='ok',data=saved['result'])
                    if op=='discover':
                        if not service.enabled('active_reading_enabled') or not service.cfg('reading_discovery_enabled'):raise ValueError('请先启用主动阅读和自主发现')
                        service.discovery.report(next_at=service.clock(),state='waiting',reason='已预约一次空闲探索，仍受每日预算限制')
                        await service.store.flush('__discovery__');data=service.discovery.snapshot()
                    elif op=='read':data=await service.queue_read(body.get('reader'),str(body.get('value','')),str(body.get('visibility','public')),rid)
                    elif kind=='experience':
                        key=str(body.get('id'));row=plugin._life.store.rows.get(key)
                        if not row or row.get('record_type') not in {'reflection','event'}:raise ValueError('经历或看法不存在')
                        if body.get('version')!=row.get('revision'):raise RuntimeError('version_conflict')
                        if op not in {'correct','revoke','archive'}:raise ValueError('未知经历操作')
                        updated=copy.deepcopy(row);field='thought' if row['record_type']=='reflection' else 'summary'
                        history=list(row.get('history',[]));history.append({'at':service.clock(),'action':op,'previous':row[field],'reason':str(body.get('reason',''))[:250]})
                        updated.update(history=history,changed_at=service.clock())
                        if op=='correct':
                            content=body.get('content')
                            if not isinstance(content,str) or not 2<=len(content)<=300:raise ValueError('更正内容须为2至300字')
                            updated[field]=content;updated['corrected_by']='admin'
                        else:
                            updated['status']='revoked' if op=='revoke' else 'archived';updated['archived_at']=service.clock()
                        data=plugin._life.store.put(key,updated);plugin._life.reflection.refresh();await plugin._life.store.flush(key)
                    elif kind=='work' and op=='retry':
                        key=str(body.get('id'));row=service.store.rows.get(key)
                        if not row or row.get('state') not in {'failed','quota','retry'}:raise ValueError('该任务无需重试')
                        if body.get('version')!=row.get('revision'):raise RuntimeError('version_conflict')
                        data=service.store.put(key,{**row,'state':'retry','attempts':0,'retry_at':service.clock()});await service.store.flush(key)
                    else:raise ValueError('未知操作')
                    service.store.put(request_key,{'fingerprint':fingerprint,'result':data});await service.store.flush(request_key)
            return jsonify(status='ok',data=data)
        except RuntimeError as exc:
            return jsonify(status='error',message='记录已改变，请重新读取后操作' if str(exc)=='version_conflict' else '保存暂不可用，请用原请求重试'),409 if str(exc)=='version_conflict' else 503
        except (ValueError,TypeError,KeyError) as exc:return jsonify(status='error',message=str(exc)),400
        except (TimeoutError,OSError):return jsonify(status='error',message='保存尚未确认，可用原请求重试'),503
    for name,handler,methods in [('panel_memory',listing,['GET']),('panel_memory_detail',detail,['GET']),('panel_memory_action',action,['POST'])]:
        plugin.context.register_web_api('/astrbot_plugin_proactive_agent/'+name,handler,methods,'学习与记忆')
