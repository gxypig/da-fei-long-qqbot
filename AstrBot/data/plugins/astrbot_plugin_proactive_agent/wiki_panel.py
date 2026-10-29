"""One wiki UI over the existing dictionary and local imports; no duplicate store."""
import hashlib
import json
from quart import jsonify,request
from .meme_memory import normalize


def revision(row):return hashlib.sha256(json.dumps(row,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


async def wiki_api(plugin):
    memory=plugin._memory
    try:
        if request.method=='GET' and request.args.get('view')=='usage':
            usage=getattr(plugin,'_meme_usage',None)
            if not usage:return jsonify(status='error',message='当前版本不支持使用记录',capabilities={'usage_trace':False}),501
            cursor=json.loads(request.args['before']) if request.args.get('before') else None
            if cursor and (not isinstance(cursor,list) or len(cursor)!=2 or not isinstance(cursor[0],(float,int)) or not isinstance(cursor[1],str)):raise ValueError('invalid cursor')
            return jsonify(status='ok',data=usage.page(cursor))
        if request.method=='POST':
            if request.content_length and request.content_length>20000:raise ValueError('单条词条内容过大')
            body=await request.get_json(silent=True)
            if not isinstance(body,dict) or set(body)-{'action','entry','phrase','revision'}:raise ValueError('请求字段错误')
            action=body.get('action');entry=body.get('entry',{})
            phrase=normalize(entry.get('phrase') if action=='save' else body.get('phrase'))
            key=phrase.casefold()
            async with plugin._panel_lock:
                old=memory.entries.get(key)
                if body.get('revision')!=revision(old):return jsonify(status='error',message='词条已发生变化，草稿保留，请重新打开词条核对'),409
                if action=='save':memory.import_entries([{**entry,'phrase':phrase}],True)
                elif action=='delete':
                    if not old or old.get('provenance')!='custom':raise ValueError('只能删除本地自定义词条；公共词典保持只读')
                    before=dict(memory.entries);memory.entries.pop(key)
                    try:memory._save()
                    except OSError:memory.entries=before;raise
                else:raise ValueError('未知操作')
            return jsonify(status='ok',data={'revision':revision(memory.entries.get(key))})
        query=request.args.get('q','').strip();source=request.args.get('source','all')
        if len(query)>100 or source not in {'all','wiki','custom','learned'}:raise ValueError('搜索或来源不正确')
        if request.args.get('phrase'):
            phrase=normalize(request.args['phrase']);local=memory.entries.get(phrase.casefold())
            entry=local if local and local.get('provenance')=='custom' else memory.wiki.exact(phrase) if memory.wiki else local
            entry=entry or local
            if not entry:return jsonify(status='error',message='未找到词条'),404
            return jsonify(status='ok',data={'entry':memory._with_understanding(entry),'detail':memory.wiki.detail(phrase) if memory.wiki else None,
                                            'revision':revision(local)})
        offset=max(0,int(request.args.get('offset',0)));limit=30
        if source=='all':data=memory.repository(query,offset,limit)
        elif source=='wiki':data={**(memory.wiki.catalog(query,offset=offset,limit=limit) if memory.wiki else {'total':0,'rows':[]}),'offset':offset}
        else:
            rows=[r for r in memory.entries.values() if (r.get('provenance')=='custom')==(source=='custom') and (not query or query.casefold() in json.dumps(r,ensure_ascii=False).casefold())]
            rows.sort(key=lambda r:r['phrase']);data={'total':len(rows),'rows':rows[offset:offset+limit],'offset':offset}
        data['dictionary']=memory.wiki.status() if memory.wiki else None
        data['custom_count']=sum(r.get('provenance')=='custom' for r in memory.entries.values())
        data['learned_count']=sum(r.get('provenance')!='custom' for r in memory.entries.values())
        data['empty_revision']=revision(None)
        data['rows']=[{**r,'revision':revision(memory.entries.get(r['phrase'].casefold()))} for r in data['rows']]
        return jsonify(status='ok',data=data)
    except (ValueError,TypeError,AttributeError):return jsonify(status='error',message='词条格式、必填含义/用法或分页参数不正确'),400
    except OSError:return jsonify(status='error',message='保存失败，原词条已保留'),500
