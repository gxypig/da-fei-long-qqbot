"""Idle-only, atomic MemeWiki versions. Public data never blocks a chat lookup."""
import asyncio,copy,gzip,hashlib,io,json,os,re,shutil,sqlite3,time,uuid
from contextlib import closing
from datetime import datetime,timezone,timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote
import aiohttp
from .memewiki_engine import MemeWikiEngine,API_BASE,plain_html,SNAPSHOT_SHA256
from .data_backup import atomic_json,file_digest

ZONE=timezone(timedelta(hours=8))
SCHEMA='CREATE TABLE IF NOT EXISTS memes (title TEXT PRIMARY KEY,slug TEXT,tags TEXT,usage TEXT,origin TEXT,other TEXT,summary TEXT)'

class WikiConflict(ValueError):pass

def validate_db(path):
    path=Path(path).resolve()
    if not path.is_file() or path.stat().st_size>100_000_000:raise ValueError('词库文件缺失或超过100MB')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('词库校验失败')
        names={r[1] for r in db.execute('PRAGMA table_info(memes)')}
        if not {'title','slug','tags','usage','origin','other','summary'}<=names:raise ValueError('词库字段不完整')
        count=db.execute('SELECT count(*) FROM memes').fetchone()[0]
        if not 1<=count<=100000:raise ValueError('词条数量不正确')
        if db.execute("SELECT 1 FROM memes WHERE title IS NULL OR length(trim(title))=0 LIMIT 1").fetchone():raise ValueError('词库包含空标题')
    return count

async def threaded(fn,*args):
    # A cancelled to_thread keeps writing: join before touching its staging files.
    task=asyncio.create_task(asyncio.to_thread(fn,*args))
    try:return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task,return_exceptions=True);raise

class WikiUpdater:
    def __init__(self,plugin,clock=time.time):
        self.plugin=plugin;self.memory=plugin._memory;self.engine=self.memory.wiki;self.clock=clock
        self.root=self.engine.path.parent/'versions';self.root.mkdir(parents=True,exist_ok=True)
        self.state_path=self.root/'state.json';self.lock=asyncio.Lock();self.task=None;self.scheduler=None
        self.closed=False;self.last_request=0.;self.idle_since=None;self.state={'active':None,'versions':[],'job':None,'last_check':None,'last_success':None,'last_schedule_day':None}
        try:self.state.update(json.loads(self.state_path.read_text('utf-8')))
        except FileNotFoundError:pass
        self.state['revision']=self.state.get('revision',0)

    async def start(self):
        active=next((v for v in self.state['versions'] if v['id']==self.state['active']),None)
        if active:
            path=self.version_path(active['id']);await threaded(validate_db,path)
            if await threaded(file_digest,path)!=active['sha256']:raise ValueError('当前词库版本校验失败，原始词库继续使用')
            prepared=await threaded(self.prepare,path);self.activate(prepared)
        job=self.state.get('job')
        if job and job.get('state') in {'running','queued','paused','validating','publishing'}:
            job.update(state='interrupted',reason='上次更新中断，当前完整词库继续使用')
            await self.save()
        self.scheduler=asyncio.create_task(self.loop(),name='memewiki-update-clock')

    def version_path(self,id_):
        if not isinstance(id_,str) or not re.fullmatch(r'[a-f0-9]{32}',id_):raise ValueError('词库版本编号错误')
        return self.root/(id_+'.db')

    async def save(self):await threaded(atomic_json,self.state_path,self.state)

    def status(self):
        return {**copy.deepcopy(self.state),'dictionary':self.engine.status(),
                'automatic':bool(self.plugin._cfg('wiki_update_enabled',True)),
                'time':self.plugin._cfg('wiki_update_time','04:00'),'timezone':'Asia/Hong_Kong',
                'busy':bool(self.task and not self.task.done()),'network_concurrency':1,'minimum_interval':1,
                'offline_sha256':SNAPSHOT_SHA256}

    async def begin(self,expected=None,*,offline=None,sha256=None):
        async with self.lock:
            if self.task and not self.task.done():return self.status()
            if expected is not None and expected!=self.state['revision']:raise WikiConflict('词库状态已变化，请刷新后重试')
            if self.closed:raise ValueError('词库服务正在停止')
            self.state['revision']+=1
            self.state['job']={'id':uuid.uuid4().hex,'state':'queued','started':self.clock(),'pages':0,'read':0,'updated':0,'added':0,'reason':''}
            self.state['last_check']=self.clock();await self.save()
            self.task=asyncio.create_task(self.run(offline,sha256),name='memewiki-atomic-update')
            return self.status()

    async def cancel(self,expected=None):
        async with self.lock:
            if expected is not None and expected!=self.state['revision']:raise WikiConflict('词库状态已变化')
            task=self.task
            if task and not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
            # Cancellation can happen before run() has entered its try/finally.
            job=self.state.get('job')
            if job and job.get('state') in {'queued','running','paused','validating'}:
                job.update(state='cancelled',reason='更新已取消，原词库继续使用')
                self.state['revision']+=1;await self.save()
            return self.status()

    async def wait_idle(self):
        while True:
            if self.closed:raise asyncio.CancelledError()
            busy=getattr(self.plugin,'_work',None)
            if busy and busy.busy():self.idle_since=None;self.state['job']['state']='paused'
            elif self.idle_since is None:self.idle_since=time.monotonic()
            elif time.monotonic()-self.idle_since>=5:
                self.state['job']['state']='running';return
            await asyncio.sleep(.2)

    async def fetch(self,session,url,params=None):
        for attempt in range(3):
            await self.wait_idle()
            await asyncio.sleep(max(0,1-(time.monotonic()-self.last_request)))
            # Foreground work may have arrived while rate limiting slept.
            await self.wait_idle()
            self.last_request=time.monotonic()
            try:
                async with session.get(url,params=params,timeout=aiohttp.ClientTimeout(total=20)) as response:
                    if response.status in {429,503}:
                        retry=response.headers.get('Retry-After','')
                        try:delay=max(1,float(retry))
                        except ValueError:
                            try:delay=max(1,parsedate_to_datetime(retry).timestamp()-self.clock())
                            except (ValueError,TypeError):delay=2**attempt
                        if attempt==2:response.raise_for_status()
                        self.state['job'].update(state='paused',reason='服务端限流，等待后继续',retry_at=self.clock()+delay)
                        await asyncio.sleep(delay);continue
                    response.raise_for_status();value=await response.json()
                    if not isinstance(value,dict):raise ValueError('百科接口返回格式错误')
                    return value
            except (aiohttp.ClientError,TimeoutError):
                if attempt==2:raise
                await asyncio.sleep(2**attempt)
        raise RuntimeError('百科重试耗尽')

    def clone(self,path):
        with closing(sqlite3.connect(path)) as target:
            if self.engine.path.is_file():
                with closing(sqlite3.connect(self.engine.path.as_uri()+'?mode=ro',uri=True)) as source:source.backup(target)
            target.execute(SCHEMA)
            target.execute('CREATE TABLE IF NOT EXISTS wiki_sync (slug TEXT PRIMARY KEY,updated TEXT)');target.commit()

    def merge(self,path,rows):
        with closing(sqlite3.connect(path)) as db,db:
            for row in rows:
                detail=row['item'];title=str(detail.get('title') or '')
                if not title:raise ValueError('词条详情没有标题')
                slug=str(detail.get('slug') or title)
                existed=db.execute('SELECT 1 FROM memes WHERE title=? OR slug=?',(title,slug)).fetchone()
                # Explicit stable-ID revision may rename a title. This is not
                # deletion by absence from a partial catalog page.
                db.execute('DELETE FROM memes WHERE slug=? AND title<>?',(slug,title))
                body=plain_html(detail.get('content_html',''))
                db.execute('INSERT OR REPLACE INTO memes(title,slug,tags,usage,origin,other,summary) VALUES(?,?,?,?,?,?,?)',
                    (title,str(detail.get('slug') or title),json.dumps(detail.get('tags',[]),ensure_ascii=False),
                     str(detail.get('usage') or body),str(detail.get('origin') or ''),body,str(detail.get('summary') or '')))
                db.execute('INSERT OR REPLACE INTO wiki_sync VALUES(?,?)',(str(detail.get('slug') or title),str(row['updated'])))
                self.state['job']['updated' if existed else 'added']+=1

    def prepare(self,path):
        validate_db(path);engine=MemeWikiEngine(path)
        if not engine.refresh():raise ValueError('词库索引构建失败')
        from .meme_index import MemeIndex,fold
        entries=copy.deepcopy(self.memory.entries);generation=self.memory._index_generation
        rows=[]
        for row in entries.values():
            public=engine.local_entries.get(fold(row['phrase'])) if row.get('provenance')!='custom' else None
            rows.append(self.memory._with_understanding(public or row))
        return engine,MemeIndex(rows),generation

    def activate(self,prepared):
        engine,index,generation=prepared
        self.engine.path=engine.path;self.engine.titles=engine.titles;self.engine.local_entries=engine.local_entries;self.engine.by_slug=engine.by_slug
        self.engine.index=engine.index;self.engine.signature=engine.signature;self.engine.cache.clear()
        self.engine.stats.update(engine.stats)
        if generation==self.memory._index_generation:
            self.memory._local_index=index
            self.memory._index_key=(id(self.memory.entries),len(self.memory.entries),generation,engine.signature)
        else:
            # A custom edit during durable commit must not make the next chat
            # rebuild an index. Keep its usable local index until background swap.
            async def refresh_local():
                while not self.closed:
                    rebuilt,key=await threaded(self.memory.rebuild_local_index)
                    current=(id(self.memory.entries),len(self.memory.entries),self.memory._index_generation,self.engine.signature)
                    if key==current:
                        self.memory._local_index=rebuilt;self.memory._index_key=key;return
            self.local_rebuild=asyncio.create_task(refresh_local(),name='memewiki-local-index')

    async def publish(self,path):
        count=await threaded(validate_db,path);digest=await threaded(file_digest,path)
        before=copy.deepcopy(self.state)
        if not self.state['versions'] and self.engine.path.is_file():
            old=uuid.uuid4().hex;oldpath=self.version_path(old)
            await threaded(shutil.copy2,self.engine.path,oldpath)
            self.state['versions'].append({'id':old,'sha256':await threaded(file_digest,oldpath),'at':self.clock(),'entries':len(self.engine.titles),'baseline':True})
        # All potentially slow preparation precedes the commit. Local entries can
        # change while the state file is flushing; activate schedules that rebuild.
        while True:
            prepared=await threaded(self.prepare,path)
            if prepared[2]==self.memory._index_generation:break
        version=path.stem
        self.state['versions'].append({'id':version,'sha256':digest,'at':self.clock(),'entries':count})
        self.state.update(active=version,last_success=self.clock(),revision=self.state['revision']+1)
        self.state['job'].update(state='completed',finished=self.clock(),reason='完整校验后切换成功')
        # State file is the commit point. No await between commit completion and pointer swap.
        task=asyncio.create_task(asyncio.to_thread(atomic_json,self.state_path,self.state))
        cancelled=False
        try:await asyncio.shield(task)
        except asyncio.CancelledError:cancelled=True;await task
        except BaseException:self.state=before;raise
        self.activate(prepared)
        if cancelled:return
        try:await self.retain()
        except Exception as exc:self.state['maintenance_error']='旧版本清理待重试：'+type(exc).__name__

    async def retain(self):
        versions=self.state['versions'];keep=versions[-3:]
        if self.state['active'] not in {v['id'] for v in keep}:
            active=next(v for v in versions if v['id']==self.state['active']);keep=[active,*[v for v in keep if v['id']!=active['id']][-2:]]
        self.state['versions']=keep;await self.save()
        for row in versions:
            if row not in keep:await threaded(self.version_path(row['id']).unlink)

    async def run(self,offline=None,sha256=None):
        path=self.version_path(uuid.uuid4().hex)
        try:
            if offline is not None:
                if not isinstance(offline,bytes) or len(offline)>100_000_000:raise ValueError('离线包过大')
                if not isinstance(sha256,str) or hashlib.sha256(offline).hexdigest()!=sha256:raise ValueError('离线包校验值不匹配')
                if offline[:2]==b'\x1f\x8b':
                    with gzip.GzipFile(fileobj=io.BytesIO(offline)) as stream:offline=stream.read(100_000_001)
                    if len(offline)>100_000_000:raise ValueError('离线包解压后过大')
                await threaded(path.write_bytes,offline)
            else:
                await threaded(self.clone,path);offset=0;seen=set()
                async with aiohttp.ClientSession(headers={'User-Agent':'AstrBot-MemeWiki-Updater/1.0 (public dictionary)'}) as session:
                    while True:
                        data=await self.fetch(session,API_BASE,{'limit':100,'offset':offset})
                        items=data.get('items');more=data.get('has_more')
                        if not isinstance(items,list) or type(more) is not bool or data.get('offset',offset)!=offset or more and not items:raise ValueError('分页不完整，旧词库保留')
                        rows=[]
                        with closing(sqlite3.connect(path)) as db:known=dict(db.execute('SELECT slug,updated FROM wiki_sync'))
                        for item in items:
                            if not isinstance(item,dict) or not item.get('slug'):raise ValueError('词条列表缺少编号')
                            slug=str(item['slug'])
                            if slug in seen:raise ValueError('列表分页重复，未切换词库')
                            seen.add(slug);self.state['job']['read']+=1
                            if known.get(slug)==str(item.get('updated_at') or ''):continue
                            detail=await self.fetch(session,API_BASE+'/'+quote(slug,safe=''))
                            if not isinstance(detail.get('item'),dict) or str(detail['item'].get('slug') or '')!=slug:raise ValueError('词条详情不完整或编号不匹配')
                            rows.append({'item':detail['item'],'updated':item.get('updated_at') or ''})
                            if len(rows)>=16:await threaded(self.merge,path,rows);rows=[]
                        if rows:await threaded(self.merge,path,rows)
                        self.state['job']['pages']+=1;await self.save()
                        if not more:break
                        offset+=len(items)
            self.state['job']['state']='validating';await self.publish(path)
        except asyncio.CancelledError:
            if self.state['active']!=path.stem:self.state['job'].update(state='cancelled',reason='更新已取消，原词库继续使用')
        except Exception as exc:
            self.state['job'].update(state='failed',reason=type(exc).__name__+': '+str(exc)[:250])
        finally:
            if self.state['active']!=path.stem and path.exists():await threaded(path.unlink)
            self.state['revision']+=1;await self.save()

    async def rollback(self,version,expected):
        async with self.lock:
            if self.task and not self.task.done():raise WikiConflict('更新任务进行中，请先取消')
            if expected!=self.state['revision']:raise WikiConflict('词库版本已变化')
            row=next((v for v in self.state['versions'] if v['id']==version),None)
            if not row:raise ValueError('该词库版本未保留')
            path=self.version_path(version)
            if await threaded(file_digest,path)!=row['sha256']:raise ValueError('回退版本校验失败')
            prepared=await threaded(self.prepare,path)
            before=copy.deepcopy(self.state)
            self.state.update(active=version,revision=self.state['revision']+1)
            task=asyncio.create_task(asyncio.to_thread(atomic_json,self.state_path,self.state))
            try:await asyncio.shield(task)
            except asyncio.CancelledError:
                try:await task
                except BaseException:self.state=before;raise
            except BaseException:self.state=before;raise
            self.activate(prepared);return self.status()

    async def loop(self):
        while True:
            await asyncio.sleep(20)
            if not self.plugin._cfg('wiki_update_enabled',True):continue
            now=datetime.fromtimestamp(self.clock(),ZONE)
            when=str(self.plugin._cfg('wiki_update_time','04:00'))
            if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',when):continue
            due=now if now.strftime('%H:%M')>=when else now-timedelta(days=1)
            day=due.date().isoformat()
            if self.state.get('last_schedule_day')==day:continue
            if self.task and not self.task.done():continue
            self.state['last_schedule_day']=day;await self.save();await self.begin()

    async def close(self):
        self.closed=True
        if self.scheduler:self.scheduler.cancel();await asyncio.gather(self.scheduler,return_exceptions=True)
        await self.cancel()
        task=getattr(self,'local_rebuild',None)
        if task:task.cancel();await asyncio.gather(task,return_exceptions=True)
