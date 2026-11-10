"""Scoped, source-backed SQLite memory. All disk work runs outside the event loop."""
import hashlib,json,re,sqlite3,threading,time
from datetime import datetime,timezone,timedelta
from pathlib import Path

KINDS={'fact','preference','experience','relationship','knowledge'}
PROVENANCE={'user_statement','tool_confirmed','virtual','opinion'}
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def terms(text):
    text=str(text).casefold();out=set(re.findall(r'[a-z0-9_]+',text))
    for word in re.findall(r'[\u3400-\u9fff]+',text):
        out.update(word[i:i+2] for i in range(len(word)-1))
        if len(word)==1:out.add(word)
    return sorted(out)[:300]
def valid_scope(scope):return bool(re.fullmatch(r'[^:\s]+:(?:FriendMessage|GroupMessage):[^:\s]+',str(scope)))
def day(at):return datetime.fromtimestamp(at,timezone(timedelta(hours=8))).date().isoformat()

class MemoryStore:
    def __init__(self,path,clock=time.time):
        self.path=Path(path);self.clock=clock;self.lock=threading.RLock();self.db=None;self.generation=0
    def start(self):
        with self.lock:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            self.db=sqlite3.connect(self.path,check_same_thread=False,timeout=2)
            self.db.row_factory=sqlite3.Row
            self.db.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS memories(id INTEGER PRIMARY KEY,scope TEXT NOT NULL,subject TEXT NOT NULL,fact_key TEXT NOT NULL,kind TEXT NOT NULL,provenance TEXT NOT NULL,content TEXT NOT NULL,sources TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,source_at REAL NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,previous INTEGER);
CREATE INDEX IF NOT EXISTS mem_scope ON memories(scope,status,id);
CREATE UNIQUE INDEX IF NOT EXISTS mem_current ON memories(scope,subject,fact_key) WHERE status='active';
CREATE TABLE IF NOT EXISTS memory_terms(term TEXT NOT NULL,memory_id INTEGER NOT NULL,PRIMARY KEY(term,memory_id));
CREATE INDEX IF NOT EXISTS term_mem ON memory_terms(memory_id);
CREATE TABLE IF NOT EXISTS tombstones(key TEXT PRIMARY KEY,at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS quota(day TEXT PRIMARY KEY,count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY,memory_id INTEGER,at REAL,kind TEXT,body TEXT);
CREATE TABLE IF NOT EXISTS actions(request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
''');self.db.commit()
    def _index(self,id_,content):
        self.db.execute('DELETE FROM memory_terms WHERE memory_id=?',(id_,))
        self.db.executemany('INSERT OR IGNORE INTO memory_terms VALUES(?,?)',[(x,id_) for x in terms(content)])
    def _forgotten(self,key,sources):
        keys=[digest(['fact',*key]),*[digest(['source',key[0],s['id']]) for s in sources]]
        return any(self.db.execute('SELECT 1 FROM tombstones WHERE key=?',(k,)).fetchone() for k in keys)
    def remember(self,row,limit=80):
        """Return quota without consuming the candidate. Corrections bypass quota."""
        scope=row['scope'];subject=str(row['subject']);fact_key=str(row['fact_key']).strip().casefold()
        if not valid_scope(scope) or not subject or not 1<=len(fact_key)<=80:raise ValueError('invalid memory identity')
        if row['kind'] not in KINDS or row['provenance'] not in PROVENANCE:raise ValueError('invalid memory type')
        content=row['content'];sources=row['sources']
        if not isinstance(content,str) or not 2<=len(content)<=260 or not sources:raise ValueError('memory needs bounded source evidence')
        now=self.clock();key=(scope,subject,fact_key)
        with self.lock,self.db:
            if self._forgotten(key,sources):return {'status':'forgotten'}
            old=self.db.execute("SELECT * FROM memories WHERE scope=? AND subject=? AND fact_key=? AND status='active'",key).fetchone()
            source_at=float(row['source_at'])
            if old and source_at<old['source_at']:return {'status':'stale','id':old['id']}
            if old and old['content']==content:return {'status':'existing','id':old['id']}
            used=self.db.execute('SELECT count FROM quota WHERE day=?',(day(now),)).fetchone()
            if not old and used and used[0]>=max(1,int(limit)):return {'status':'quota','resume_day':day(now+86400)}
            if old:
                self.db.execute("UPDATE memories SET status='superseded',revision=revision+1 WHERE id=?",(old['id'],))
                self.db.execute('DELETE FROM memory_terms WHERE memory_id=?',(old['id'],))
            cur=self.db.execute('INSERT INTO memories(scope,subject,fact_key,kind,provenance,content,sources,created,updated,source_at,status,previous) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (*key,row['kind'],row['provenance'],content,json.dumps(sources,ensure_ascii=False),now,now,source_at,'active',old['id'] if old else None))
            id_=cur.lastrowid;self._index(id_,content+' '+fact_key)
            if not old:self.db.execute('INSERT INTO quota VALUES(?,1) ON CONFLICT(day) DO UPDATE SET count=count+1',(day(now),))
            self.db.execute('INSERT INTO changes(memory_id,at,kind,body) VALUES(?,?,?,?)',(id_,now,'corrected' if old else 'added',json.dumps({'previous':old['id'] if old else None})))
            self.generation+=1
            return {'status':'corrected' if old else 'added','id':id_,'previous':old['id'] if old else None}
    def recall(self,scope,query,limit=5):
        tokens=terms(query)
        if not valid_scope(scope) or not tokens:return []
        with self.lock:
            rows=self.db.execute(f"""SELECT m.*,COUNT(*) AS score FROM memory_terms t JOIN memories m ON m.id=t.memory_id
WHERE m.scope=? AND m.status='active' AND t.term IN ({','.join('?' for _ in tokens)})
GROUP BY m.id ORDER BY score DESC,m.source_at DESC LIMIT ?""",(scope,*tokens,min(5,max(1,limit)))).fetchall()
            return [self._row(r) for r in rows]
    @staticmethod
    def _row(row):
        if not row:return None
        row=dict(row);row['sources']=json.loads(row['sources']);return row
    def listing(self,query='',scope='',cursor=0,limit=25,status='active'):
        limit=min(100,max(1,int(limit)));clauses=['id<?'];params=[int(cursor) if cursor else 2**63-1]
        if scope:clauses.append('scope=?');params.append(scope)
        if status:clauses.append('status=?');params.append(status)
        if query:clauses.append("(content LIKE ? ESCAPE '\\' OR fact_key LIKE ? ESCAPE '\\' OR subject=?)");q='%'+str(query).replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%';params.extend((q,q,query))
        with self.lock:
            rows=self.db.execute('SELECT * FROM memories WHERE '+' AND '.join(clauses)+' ORDER BY id DESC LIMIT ?',(*params,limit+1)).fetchall()
            counts=dict(self.db.execute('SELECT status,COUNT(*) FROM memories GROUP BY status').fetchall())
            return {'rows':[self._row(r) for r in rows[:limit]],'next_cursor':rows[limit-1]['id'] if len(rows)>limit else None,'counts':counts}
    def detail(self,id_):
        with self.lock:
            row=self._row(self.db.execute('SELECT * FROM memories WHERE id=?',(int(id_),)).fetchone())
            if row:row['history']=[dict(r) for r in self.db.execute('SELECT * FROM changes WHERE memory_id=? ORDER BY id DESC',(int(id_),)).fetchall()]
            return row
    def action(self,body):
        action=body.get('action');rid=str(body.get('request_id',''));fingerprint=digest(body)
        if action not in {'edit','correct','forget'} or not 8<=len(rid)<=100:raise ValueError('invalid memory operation')
        with self.lock,self.db:
            prior=self.db.execute('SELECT * FROM actions WHERE request_id=?',(rid,)).fetchone()
            if prior:
                if prior['fingerprint']!=fingerprint:raise ValueError('request_id reused with different content')
                return json.loads(prior['result'])
            row=self.db.execute('SELECT * FROM memories WHERE id=?',(int(body['id']),)).fetchone()
            if not row:raise ValueError('memory not found')
            if int(body.get('version',-1))!=row['revision']:raise RuntimeError('version_conflict')
            if row['status']!='active':raise ValueError('memory is not active')
            now=self.clock()
            if action=='forget':
                key=(row['scope'],row['subject'],row['fact_key']);sources=json.loads(row['sources'])
                related=self.db.execute('SELECT id,sources FROM memories WHERE scope=? AND subject=? AND fact_key=?',key).fetchall()
                marks=[digest(['fact',*key])]
                for item in related:
                    marks.extend(digest(['source',key[0],s['id']]) for s in json.loads(item['sources']))
                    self.db.execute('DELETE FROM memory_terms WHERE memory_id=?',(item['id'],))
                    self.db.execute('DELETE FROM changes WHERE memory_id=?',(item['id'],))
                self.db.executemany('INSERT OR IGNORE INTO tombstones VALUES(?,?)',[(k,now) for k in marks])
                self.db.execute("UPDATE memories SET content='',sources='[]',fact_key='已遗忘',subject='',status='forgotten',revision=revision+1,updated=? WHERE scope=? AND subject=? AND fact_key=?",(now,*key))
            else:
                content=body.get('content','')
                if not isinstance(content,str) or not 2<=len(content.strip())<=260:raise ValueError('记忆内容须为2至260字')
                self.db.execute('INSERT INTO changes(memory_id,at,kind,body) VALUES(?,?,?,?)',(row['id'],now,action,json.dumps({'before':row['content'],'after':content,'reason':str(body.get('reason',''))[:200]},ensure_ascii=False)))
                self.db.execute('UPDATE memories SET content=?,revision=revision+1,updated=?,source_at=? WHERE id=?',(content.strip(),now,max(now,row['source_at']),row['id']))
                self._index(row['id'],content+' '+row['fact_key'])
            result={'id':row['id'],'version':row['revision']+1,'status':'forgotten' if action=='forget' else 'active'}
            if action=='forget':result['forgotten_ids']=[r['id'] for r in related]
            self.db.execute('INSERT INTO actions VALUES(?,?,?)',(rid,fingerprint,json.dumps(result)));self.generation+=1
            return result
    def close(self):
        with self.lock:
            if self.db:self.db.close();self.db=None
