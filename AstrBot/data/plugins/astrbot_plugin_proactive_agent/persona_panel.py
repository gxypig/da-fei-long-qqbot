"""Uses AstrBot's authenticated plugin API bridge, never a second settings file."""
from quart import request,jsonify
from .persona_registry import PersonaConflict

def register_persona_panel(plugin):
    async def state():
        registry=plugin._personas;pid=request.args.get('persona_id')
        try:
            data=await registry.inspect(pid) if pid else {'people':await registry.list_people(),'selected_persona_id':registry.owner_id}
            return jsonify(status='ok',data={**data,'capabilities':{'persona_editor':1,'atomic_versions':True}})
        except ValueError as exc:return jsonify(status='error',kind='missing_persona',message=str(exc)),404
        except Exception:return jsonify(status='error',kind='read_failure',message='人格读取失败，未恢复默认人格'),503
    async def action():
        body=await request.get_json(silent=True)
        if not isinstance(body,dict):return jsonify(status='error',message='请求格式错误'),400
        pid=body.get('persona_id');revision=body.get('revision');source=body.get('source_fingerprint');action_=body.get('action')
        if not isinstance(pid,str) or not 1<=len(pid)<=256 or type(revision) is not int or not isinstance(source,str):
            return jsonify(status='error',message='缺少人物、版本或来源信息'),400
        try:
            if action_=='draft':data=await plugin._personas.save_draft(pid,body.get('text'),revision,source)
            else:data=await plugin._personas.submit(pid,action_,revision,source,version=body.get('version'),request_id=body.get('request_id'))
            return jsonify(status='ok',data=data)
        except PersonaConflict as exc:return jsonify(status='error',kind='conflict',message=str(exc)),409
        except ValueError as exc:return jsonify(status='error',message=str(exc)),400
        except Exception:return jsonify(status='error',kind='persistence',message='人格操作未完成，请保留草稿并查看同步状态'),503
    plugin.context.register_web_api('/astrbot_plugin_proactive_agent/persona_state',state,['GET'],'人格及用途版本')
    plugin.context.register_web_api('/astrbot_plugin_proactive_agent/persona_action',action,['POST'],'保存、整理、同步或回退人格')
