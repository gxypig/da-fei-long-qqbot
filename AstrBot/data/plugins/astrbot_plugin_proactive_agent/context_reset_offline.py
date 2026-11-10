"""Explicit OFFLINE reset; caller must drain/stop the bot and verify a backup.

Delivery receipts, unknown outcomes, contact timers and opaque dedup hashes
survive. Old conversations cannot be rehydrated by a source-only rollback.
"""
import json,sqlite3,time
from contextlib import closing
from pathlib import Path
from .data_backup import atomic_json

def reset(data_root,*,backup_manifest,at=None):
    root=Path(data_root).resolve();manifest=Path(backup_manifest).resolve()
    meta=json.loads(manifest.read_text('utf-8'))
    if meta.get('format')!='qqbot-data-snapshot-v1' or not meta.get('complete'):raise ValueError('需要已经验证的数据备份')
    if not root.is_dir() or not (root/'data_v4.db').is_file():raise ValueError('未找到AstrBot运行数据库')
    plugin=root/'plugin_data/astrbot_plugin_proactive_agent';marker=plugin/'context-reset.json'
    at=float(at or time.time());report={'at':at,'counts':{},'backup':str(manifest),'state':'resetting'}
    atomic_json(marker,report)
    def db_apply(relative,action):
        path=(root/relative).resolve()
        if not path.is_relative_to(root):raise ValueError('重置路径越界')
        if not path.is_file():return
        with closing(sqlite3.connect(path,timeout=5)) as db,db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            spool=path.with_suffix('.pending.json')
            if spool.is_file():
                pending=json.loads(spool.read_text('utf-8'))
                if pending and 'records' not in tables:raise ValueError('未识别的待恢复记录，停止重置')
                for key,body in pending.items():
                    db.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,body=excluded.body WHERE excluded.revision>records.revision',(key,body['revision'],json.dumps(body,ensure_ascii=False)))
            before=db.total_changes;action(db,tables);report['counts'][relative]=db.total_changes-before
        spool.unlink(missing_ok=True)
        atomic_json(marker,report)
    def core(db,tables):
        if 'conversations' in tables:db.execute("UPDATE conversations SET content='[]',title='',token_usage=0")
        if 'platform_message_history' in tables:db.execute('DELETE FROM platform_message_history')
        if 'preferences' in tables:db.execute("DELETE FROM preferences WHERE key IN ('history','proactive_recent_content')")
    db_apply('data_v4.db',core)
    prefix='plugin_data/astrbot_plugin_proactive_agent/'
    def delete_tables(*names):
        return lambda db,tables:[db.execute('DELETE FROM "'+n+'"') for n in names if n in tables]
    for name,tables in [('live-messages.sqlite3',('messages',)),('meme-usage.sqlite3',('records',)),
        ('memory-work.sqlite3',('records',)),('long-memory.sqlite3',('memories','memory_terms','changes','actions','quota')),
        ('idle-learning.sqlite3',('jobs','cases','reports'))]:
        db_apply(prefix+name,delete_tables(*tables))
    # Keep learned memes/style strategies; their old raw pending messages are gone.
    def records(db,tables,transform):
        if 'records' not in tables:return
        for key,revision,body in db.execute('SELECT id,revision,body FROM records').fetchall():
            row=transform(key,json.loads(body))
            if row is None:db.execute('DELETE FROM records WHERE id=?',(key,))
            else:
                row['revision']=revision+1
                db.execute('UPDATE records SET revision=?,body=? WHERE id=?',(revision+1,json.dumps(row,ensure_ascii=False),key))
    def ledger(key,row):
        if row.get('at',0)>=at:return row
        row['cid']='context-reset:'+str(int(at))+':'+str(row.get('cid') or '')
        row['history_saved']=True;row['context_archived_at']=at
        for field in ('source_text','meme_plan'):row.pop(field,None)
        return row
    db_apply(prefix+'delivery-ledger.sqlite3',lambda d,t:records(d,t,ledger))
    db_apply(prefix+'sent-memory.sqlite3',lambda d,t:d.execute("UPDATE sent SET cid=? || COALESCE(cid,'') WHERE at<?",('context-reset:'+str(int(at))+':',at)) if 'sent' in t else None)
    def life(key,row):
        if key.startswith('contact:'):return row # cooldown/unanswered/consumed remain
        if row.get('status') in {'confirmed','unknown','submitting'}:
            return {k:v for k,v in row.items() if k in {'id','origin','status','delivery_id','created_at','expires_at','retry_at','revision'}}
        return None # no old inbox, candidate, event, reflection or virtual chapter
    db_apply(prefix+'proactive-life.sqlite3',lambda d,t:records(d,t,life))
    def accounts(key,row):
        if 'account' not in row:return row
        for field in ('observations','evidence','counterevidence','probes'):row[field]=[]
        row.update(outstanding=None,analysis_pending=False,source_sync_pending=False,analyzed_at=at,updated_at=at,skip_reason='上下文已重置，等待新证据')
        return row # keep admin settings, explicit certifications and quotas
    db_apply(prefix+'ai-profiles.sqlite3',lambda d,t:records(d,t,accounts))
    def pictures(key,row):
        if key.startswith(('recent:','match:','task:receipt:')):return None
        return row # owned gallery facts and corrections stay reusable
    db_apply(prefix+'image-memory/semantics.sqlite3',lambda d,t:records(d,t,pictures))
    atomic_json(plugin/'context-summaries.json',{})
    # Temporary incoming pictures are conversation context, never the gallery.
    temporary=(plugin/'incoming-images').resolve()
    if temporary.is_relative_to(plugin.resolve()) and temporary.name=='incoming-images' and temporary.is_dir():
        for file in temporary.rglob('*'):
            if file.is_file() and not file.is_symlink() and file.resolve().is_relative_to(temporary):file.unlink()
    report['state']='complete';report['completed_at']=time.time();atomic_json(marker,report)
    return report
