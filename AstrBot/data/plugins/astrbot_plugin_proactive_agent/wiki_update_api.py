import base64,hashlib,json,re
from quart import jsonify,request
from .wiki_update import WikiConflict
from .version import VERSION

def config_revision(plugin):
    return hashlib.sha256(json.dumps([plugin._cfg('wiki_update_enabled',True),plugin._cfg('wiki_update_time','04:00')]).encode()).hexdigest()

async def api(plugin):
    service=getattr(plugin,'_wiki_updates',None)
    if service is None:return jsonify(status='error',message='词库更新服务尚未加载'),503
    try:
        if request.method=='POST':
            if request.content_length and request.content_length>35_000_000:raise ValueError('离线压缩包不应超过25MB')
            body=await request.get_json(silent=True)
            if not isinstance(body,dict) or set(body)-{'action','revision','version','enabled','time','config_revision','package','sha256'}:raise ValueError('参数格式错误')
            action=body.get('action')
            if action=='start':await service.begin(body.get('revision'))
            elif action=='cancel':await service.cancel(body.get('revision'))
            elif action=='rollback':await service.rollback(body.get('version'),body.get('revision'))
            elif action=='import':
                package=base64.b64decode(body.get('package',''),validate=True)
                await service.begin(body.get('revision'),offline=package,sha256=body.get('sha256'))
            elif action=='settings':
                if type(body.get('enabled')) is not bool or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',str(body.get('time',''))):raise ValueError('自动更新时间格式不正确')
                async with plugin._panel_lock:
                    if body.get('config_revision')!=config_revision(plugin):raise WikiConflict('自动更新设置已变化，草稿保留')
                    old=dict(plugin.config)
                    try:
                        plugin.config.update(wiki_update_enabled=body['enabled'],wiki_update_time=body['time'])
                        if plugin.config.save_config() is False:raise OSError('保存未确认')
                    except Exception:plugin.config.clear();plugin.config.update(old);raise
            else:raise ValueError('未知词库操作')
        return jsonify(status='ok',data={**service.status(),'program_version':VERSION,'config_revision':config_revision(plugin)})
    except WikiConflict as exc:return jsonify(status='error',message=str(exc)),409
    except (TypeError,ValueError) as exc:return jsonify(status='error',message=str(exc)),400
    except OSError:return jsonify(status='error',message='保存失败，原词库继续使用'),500
