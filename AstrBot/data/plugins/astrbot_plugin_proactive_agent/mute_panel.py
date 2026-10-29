from quart import request,jsonify
from .session_mute import MuteConflict,object_id

def register_mute_panel(plugin):
    async def state():
        kind=request.args.get('kind','GroupMessage');query=request.args.get('q','').casefold();only=request.args.get('only_muted')=='true'
        if kind not in {'GroupMessage','FriendMessage'}:return jsonify(status='error',message='对象分类错误'),400
        try:offset=max(0,int(request.args.get('offset',0)))
        except ValueError:return jsonify(status='error',message='页码错误'),400
        await plugin._targets.refresh();platform=plugin._cfg('platform_id','aiocqhttp');mutes=plugin._mutes
        contacts=plugin._targets.contacts.get(kind,{});keys={f'{platform}:{kind}:{uid}' for uid in contacts}
        keys.update(k for k in mutes.data['rules'] if k.startswith(platform+':'+kind+':'))
        rows=[]
        for origin in sorted(keys):
            uid=origin.rsplit(':',1)[-1];name=contacts.get(uid,{}).get('name','未在当前列表找到')
            row={**mutes.status(origin),'id':uid,'name':name}
            if only and not row['muted'] or query and query not in (uid+' '+name).casefold():continue
            rows.append(row)
        return jsonify(status='ok',data={'rows':rows[offset:offset+30],'offset':offset,'total':len(rows),'capabilities':{'session_mute':1},'contact_warning':plugin._targets.errors.get(kind)})
    async def change():
        body=await request.get_json(silent=True)
        if not isinstance(body,dict):return jsonify(status='error',message='参数格式错误'),400
        try:
            origin=object_id(body.get('origin',''))
            if origin.split(':')[0]!=plugin._cfg('platform_id','aiocqhttp'):raise ValueError('对象不属于当前机器人实例')
            result=await plugin._mutes.change(origin,body.get('muted'),body.get('until'),body.get('revision'),body.get('request_id'))
            return jsonify(status='ok',data=result)
        except MuteConflict as exc:return jsonify(status='error',kind='conflict',message=str(exc)),409
        except (ValueError,AttributeError,TypeError) as exc:return jsonify(status='error',message=str(exc)),400
        except Exception:return jsonify(status='error',kind='persistence',message='静默状态未能可靠保存，该对象暂时停止发送；请保留页面并重试'),503
    plugin.context.register_web_api('/astrbot_plugin_proactive_agent/mute_state',state,['GET'],'会话管理与静默状态')
    plugin.context.register_web_api('/astrbot_plugin_proactive_agent/mute_action',change,['POST'],'独立保存静默或恢复')
