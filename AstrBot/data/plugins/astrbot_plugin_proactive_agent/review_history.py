"""Local, bounded review evidence. All SQLite work runs outside the event loop.

No model calls, no transport calls, no inference from timing or latest drafts.
Only an explicit review_id links approval and actual delivery receipts.
"""
import asyncio,base64,copy,hashlib,json,re,sqlite3,time,uuid
from contextlib import closing
from contextvars import ContextVar
from .record_store import AsyncRecordStore

_link=ContextVar('qqbot_review_history_link',default=None)
_SECRET=re.compile(r'(?i)sk-[a-z0-9_-]{16,}|Bearer\s+[^\s"<>]+|(?:api[_-]?key|access[_-]?token|password)\s*[=:]\s*[^\s,;&"<>]+')
_THINK=re.compile(r'(?is)<(?:think|analysis)>.*?(?:</(?:think|analysis)>|$)')

def sanitize(value):
    if isinstance(value,str):return _SECRET.sub('[凭据已隐藏]',_THINK.sub('[内部分析未保存]',value))
    if isinstance(value,list):return [sanitize(v) for v in value]
    if isinstance(value,dict):return {k:sanitize(v) for k,v in value.items() if str(k).lower() not in {'reasoning','reasoning_content','chain_of_thought','hidden_thoughts'}}
    return value

def set_link(origin,review_id,request_id=None):
    _link.set({'origin':origin,'review_id':review_id,'request_id':request_id})

def linked(origin):
    value=_link.get()
    if not value:return None
    from .route_guard import canonical
    try:
        if canonical(value['origin'])!=canonical(origin):return None
    except ValueError:
        if value['origin']!=origin:return None
    from .conversation_review import request_scope
    scope=request_scope(origin) or {}
    if value.get('request_id') and value['request_id']!=scope.get('request_id'):return None
    return value['review_id']

def reset_link():_link.set(None)

def observe_format(plugin,origin,original,formatted,state,mode,*,final_unreviewed=False):
    from .conversation_review import request_scope
    from .reply_jobs import current_job
    scope=request_scope(origin)
    if scope is None:return
    previous=scope.get('review_draft',{})
    if previous.get('formatted')==original:original=previous.get('original',original)
    scope['review_draft']={'original':original,'formatted':formatted}
    history=getattr(plugin,'_review_history',None)
    settings=scope.get('review_settings') or plugin.review_settings()
    if history and (not settings.get('enabled') or final_unreviewed) and original!=formatted and not linked(origin):
        id_=history.begin(original,formatted,origin=origin,state=state,mode=mode,settings=settings,scope=scope,job=current_job.get(),kind='format_only')
        if id_:history.update(id_,approved=formatted,elapsed_ms=0)

def delivery_state(row):
    sends=list(row.get('sends',{}).values())
    if not sends:return 'cancelled' if row.get('task_outcome') in {'cancelled','ignored','superseded','failed'} else 'not_sent'
    states={s['state'] for s in sends}
    if 'confirmed' in states:
        if states&{'unknown','sending','submitting','preparing'}:return 'partial'
        if any(s['state']=='failed' and s.get('attachments') and not s.get('optional') for s in sends):return 'partial'
        if any(s.get('purpose')=='error_notification' for s in sends):return 'error_receipt_confirmed'
        actual=''.join(s.get('text','') for s in sends if s['state']=='confirmed' and s.get('text'))
        approved=row.get('approved')
        if approved and ''.join(actual.split())!=''.join(approved.split()):return 'partial'
        return 'confirmed'
    if states&{'unknown','sending','submitting'}:return 'unknown'
    if 'failed' in states:return 'failed'
    return 'not_sent'

class HistoryStore(AsyncRecordStore):
    def __init__(self,path,cfg):
        super().__init__(path,limit=256);self.cfg=cfg;self.hot_limit=256

    def _expire(self,db):
        days=max(1,min(365,int(self.cfg('review_history_days',30))))
        count=max(100,min(100000,int(self.cfg('review_history_limit',10000))))
        db.execute('DELETE FROM history WHERE at<? OR seq IN (SELECT seq FROM history ORDER BY seq DESC LIMIT -1 OFFSET ?)',(time.time()-days*86400,count))
        db.execute("DELETE FROM records WHERE substr(id,1,2)!='__' AND id NOT IN (SELECT review_id FROM history)")

    def prune(self):
        with closing(sqlite3.connect(self.path,timeout=1)) as db,db:self._expire(db)

    def _load(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path,timeout=1)) as db,db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS history(seq INTEGER PRIMARY KEY AUTOINCREMENT,review_id TEXT UNIQUE,
                at REAL,origin TEXT,result TEXT,delivery TEXT,ai_changed INTEGER,format_changed INTEGER,
                original_summary TEXT,approved_summary TEXT,search TEXT)''')
            db.execute('CREATE INDEX IF NOT EXISTS history_time ON history(at,seq)')
            db.execute('CREATE INDEX IF NOT EXISTS history_origin ON history(origin,seq)')
            self._expire(db)
            return {k:(rev,json.loads(body)) for k,rev,body in db.execute('SELECT id,revision,body FROM records ORDER BY rowid DESC LIMIT 257')}

    def _write(self,rows):
        with closing(sqlite3.connect(self.path,timeout=1)) as db,db:
            for key,(revision,row) in rows.items():
                changed=db.execute('INSERT INTO records VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,body=excluded.body WHERE excluded.revision>records.revision',
                    (key,revision,json.dumps(row,ensure_ascii=False))).rowcount
                if not changed or key.startswith('__'):continue
                values=(key,row['at'],row['origin'],row['result'],delivery_state(row),int(row.get('ai_changed',False)),int(row.get('format_changed',False)),
                        row['original'][:120],(row.get('approved') or '')[:120],
                        '\n'.join([row.get('trigger',''),row['original'],row.get('approved') or '']))
                db.execute('''INSERT INTO history(review_id,at,origin,result,delivery,ai_changed,format_changed,original_summary,approved_summary,search)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(review_id) DO UPDATE SET result=excluded.result,delivery=excluded.delivery,
                    ai_changed=excluded.ai_changed,format_changed=excluded.format_changed,original_summary=excluded.original_summary,
                    approved_summary=excluded.approved_summary,search=excluded.search''',values)
            self._expire(db)

    def _trim(self):
        if len(self.rows)<=self.hot_limit:return
        for key in list(self.rows):
            if len(self.rows)<=self.hot_limit:break
            if key in self.pending or key.startswith('__') or self.rows[key].get('result')=='running':continue
            self.rows.pop(key,None);self.persisted.pop(key,None)

class ReviewHistory:
    def __init__(self,path,cfg=lambda k,d:d):
        self.store=HistoryStore(path,cfg);self.cfg=cfg;self.missing=0;self.closed=False;self.ready=False;self.recovery=None;self.cold={};self.retention_task=None

    async def start(self):
        try:await self.store.start()
        except Exception as exc:
            self.store.stats.update(last_error=type(exc).__name__,last_error_at=time.time())
            if not self.recovery:self.recovery=asyncio.create_task(self._recover(),name='review-history-recover')
            return
        self.ready=True
        if not self.retention_task:self.retention_task=asyncio.create_task(self._retain(),name='review-history-retention')
        for key,row in list(self.store.rows.items()):
            if row.get('result')=='running':self.update(key,result='cancelled',failure='restart_interrupted',finished_at=time.time())

    async def _recover(self):
        while not self.closed and not self.ready:
            await asyncio.sleep(5)
            await self.start()

    async def _retain(self):
        while not self.closed:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(self.store.prune)
                if self.store.stats.get('last_error') and not self.store.pending:
                    self.store.stats.update(recovered_at=time.time(),latest_error=self.store.stats['last_error'],last_error=None)
            except Exception as exc:self.store.stats.update(last_error=type(exc).__name__,last_error_at=time.time())

    def _put(self,id_,row):
        if not self.ready:self.missing+=1;return None
        try:
            safe=sanitize(row)
            if safe!=row:safe['redacted']=True
            self.store.put(id_,safe);return id_
        except Exception as exc:
            self.missing+=1
            self.store.stats.update(last_error=type(exc).__name__,last_error_at=time.time())
            return None

    def begin(self,text,formatted,*,origin,state,mode,settings,scope,job=None,kind='running'):
        if self.closed:return None
        id_=uuid.uuid4().hex
        event=getattr(job,'event',None)
        # Keep only the evidence needed by a human, never provider payloads or reasoning.
        row={'id':id_,'at':time.time(),'origin':origin,'task_id':getattr(job,'id',None) or scope.get('request_id') or uuid.uuid4().hex,
             'source_id':getattr(job,'source_id',''),'sender':getattr(job,'sender',''),
             'trigger':getattr(job,'text','') or str(scope.get('trigger','')),
             'context':copy.deepcopy(list(scope.get('recent',[]))[-6:]),'persona_version':state.get('persona_version'),
             'mode':mode,'review_settings':copy.deepcopy(settings),'original':text,'formatted':formatted,
             'original_sha256':hashlib.sha256(text.encode()).hexdigest(),
             'approved':None,'rounds':[],'result':kind,'format_changed':formatted!=text,'ai_changed':False,
             'sends':{},'exchange':copy.deepcopy(state.get('exchange',{})),'elapsed_ms':None}
        result=self._put(id_,row)
        if result:
            set_link(origin,id_,scope.get('request_id'))
            if job:
                ids=getattr(job,'review_history_ids',None)
                if ids is None:job.review_history_ids=ids=[]
                ids.append(id_)
        return result

    def update(self,id_,**changes):
        if not id_ or self.closed:return
        row=self.store.rows.get(id_)
        if row is None:self.missing+=1;return
        self._put(id_,{**row,**changes})

    def round(self,id_,candidate,verdict,elapsed_ms):
        row=self.store.rows.get(id_,{})
        # Structured explanations only; never retain a model's chain of thought.
        evidence={k:copy.deepcopy(verdict[k]) for k in ('pass','natural','chat_fit','reason','replacement','dimensions') if k in verdict}
        self.update(id_,rounds=row.get('rounds',[])+[{'input':candidate,'verdict':evidence,'elapsed_ms':elapsed_ms}])

    def delivered(self,row):
        id_=row.get('review_id')
        if not id_ or self.closed:return
        current=self.store.rows.get(id_)
        if current is None:
            if len(self.cold)>=256:self.missing+=1;return
            entry=self.cold.get(id_)
            if entry:entry['rows'][row['id']]=copy.deepcopy(row)
            else:
                entry={'rows':{row['id']:copy.deepcopy(row)}};self.cold[id_]=entry
                entry['task']=asyncio.create_task(self._cold_delivery(id_),name='review-history-receipt')
            return
        sends={**current.get('sends',{}),row['id']:{k:copy.deepcopy(row.get(k)) for k in
            ('id','state','delivery_result','message_id','text','attachments','purpose','optional','reason','at','confirmed_at')}}
        self.update(id_,sends=sends)

    async def _cold_delivery(self,id_):
        try:
            saved=await asyncio.to_thread(self._read,detail=id_)
            if not saved:self.missing+=1;return
            self.store.rows[id_]=saved
            for row in self.cold[id_]['rows'].values():self.delivered(row)
            if self.cold[id_].get('changes'):self.update(id_,**self.cold[id_]['changes'])
        except Exception:self.missing+=1
        finally:self.cold.pop(id_,None)

    def end_job(self,job):
        if self.closed:return
        ids=set(getattr(job,'review_history_ids',[]))|{k for k,r in self.store.rows.items() if r.get('task_id')==job.id}
        changes=dict(task_outcome=job.state,task_reason=getattr(job,'reason',''),finished_at=time.time())
        for id_ in ids:
            if id_ in self.store.rows:self.update(id_,**changes)
            elif id_ in self.cold:self.cold[id_]['changes']=changes
            elif len(self.cold)<256:
                entry={'rows':{},'changes':changes};self.cold[id_]=entry
                entry['task']=asyncio.create_task(self._cold_delivery(id_),name='review-history-job-outcome')
            else:self.missing+=1

    def status(self):
        snapshot=self.store.snapshot()
        return {**snapshot,'ready':self.ready,'current_fault':not self.ready or not snapshot['worker_running'] or bool(snapshot.get('last_error')),
                'missing_records':self.missing,'days':self.cfg('review_history_days',30),
                'limit':self.cfg('review_history_limit',10000),'legacy_note':'旧版未留存完整对照；不按时间推断原稿'}

    def _read(self,detail=None,*,cursor=None,origin='',keyword='',result='ai_rewritten',start=None,end=None,limit=50):
        with closing(sqlite3.connect(self.store.path,timeout=1)) as db:
            db.row_factory=sqlite3.Row
            cutoff=time.time()-max(1,min(365,int(self.cfg('review_history_days',30))))*86400
            if detail:
                found=db.execute('SELECT body FROM records JOIN history ON records.id=history.review_id WHERE records.id=? AND history.at>=?',(detail,cutoff)).fetchone()
                return json.loads(found[0]) if found else None
            clauses=['at>=?','at<=?'];values=[max(cutoff,start if start is not None else time.time()-48*3600),end if end is not None else time.time()]
            if cursor:
                try:
                    before=int(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)).decode())
                    if before<1:raise ValueError()
                except Exception:raise ValueError('分页游标无效')
                clauses.append('seq<?');values.append(before)
            if origin:clauses.append('origin=?');values.append(origin)
            if keyword:clauses.append('instr(lower(search),lower(?))>0');values.append(keyword[:200])
            if result=='ai_rewritten':clauses.append('ai_changed=1')
            elif result=='passed':clauses.append("result='passed' AND ai_changed=0 AND format_changed=0")
            elif result=='format_only':clauses.append('ai_changed=0 AND format_changed=1')
            elif result=='failed':clauses.append("(result IN ('failed','cancelled') OR delivery IN ('failed','partial','unknown','cancelled','error_receipt_confirmed'))")
            elif result not in {'','all'}:raise ValueError('未知结果筛选')
            columns='seq,review_id,at,origin,result,delivery,ai_changed,format_changed,original_summary,approved_summary'
            rows=[dict(r) for r in db.execute('SELECT '+columns+' FROM history WHERE '+' AND '.join(clauses)+' ORDER BY seq DESC LIMIT ?',values+[limit+1])]
            more=len(rows)>limit;rows=rows[:limit]
            bounds=db.execute('SELECT MIN(at),MAX(at),COUNT(*) FROM history WHERE at>=?',(cutoff,)).fetchone()
            return {'rows':rows,'next_cursor':base64.urlsafe_b64encode(str(rows[-1]['seq']).encode()).decode().rstrip('=') if more else None,
                    'coverage':{'from':bounds[0],'to':bounds[1],'records':bounds[2]}}

    async def list(self,**filters):return {**await asyncio.to_thread(self._read,**filters),'health':self.status()}
    async def detail(self,id_):
        row=self.store.rows.get(id_) if id_ in self.store.pending else None
        row=copy.deepcopy(row) if row else await asyncio.to_thread(self._read,detail=id_)
        if row and row.get('at',0)<time.time()-max(1,min(365,int(self.cfg('review_history_days',30))))*86400:return None
        if row:row['delivery_state']=delivery_state(row)
        return row
    async def close(self):
        if self.retention_task:
            self.retention_task.cancel();await asyncio.gather(self.retention_task,return_exceptions=True)
        if self.recovery:
            self.recovery.cancel();await asyncio.gather(self.recovery,return_exceptions=True)
        if self.cold:await asyncio.gather(*(v['task'] for v in list(self.cold.values())),return_exceptions=True)
        if self.ready:await self.store.close()
        self.closed=True

def register(plugin):
    from quart import request,jsonify
    async def listing():
        try:
            args=request.args
            values={k:args.get(k,'') for k in ('cursor','origin','keyword')}
            values['result']=args.get('result','ai_rewritten')
            for key in ('start','end'):
                if args.get(key):values[key]=float(args[key])
            values['limit']=50
            return jsonify(status='ok',data=await plugin._review_history.list(**values))
        except ValueError as exc:return jsonify(status='error',message=str(exc)),400
        except Exception:return jsonify(status='error',message='审阅记录暂不可读，未伪造空记录'),503
    async def detail():
        id_=request.args.get('review_id','')
        if not id_ or len(id_)>100:return jsonify(status='error',message='记录编号无效'),400
        try:row=await plugin._review_history.detail(id_)
        except Exception:return jsonify(status='error',message='审阅记录读取失败，稍后重试'),503
        return (jsonify(status='ok',data=row) if row else (jsonify(status='error',message='记录不存在或已超出保留范围；旧版未留存完整对照'),404))
    plugin.context.register_web_api('/astrbot_plugin_proactive_agent/panel_review_history',listing,['GET'],'审阅历史摘要')
    plugin.context.register_web_api('/astrbot_plugin_proactive_agent/panel_review_detail',detail,['GET'],'审阅原稿与发送对照')
