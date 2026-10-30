"""Local consistent data snapshots with shared immutable assets and explicit restore."""
import asyncio,hashlib,json,os,shutil,sqlite3,threading,time,uuid
from contextlib import ExitStack,closing,contextmanager
from datetime import datetime,timezone,timedelta
from pathlib import Path

ZONE=timezone(timedelta(hours=8))
SKIP={'plugins','logs','temp','__pycache__','.cache'}
DATABASE={'.db','.sqlite','.sqlite3'}
_LOCKS={};_LOCKS_GUARD=threading.Lock()

@contextmanager
def backup_lock(root):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    with _LOCKS_GUARD:lock=_LOCKS.setdefault(str(root),threading.RLock())
    with lock:
        # Cross-process lock also protects an offline backup/GC run.
        with (root/'.coordination.lock').open('a+b') as stream:
            stream.seek(0);stream.write(b'0');stream.flush();stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
            try:yield
            finally:
                stream.seek(0)
                if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

def file_digest(path,check=lambda:None):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        while chunk:=stream.read(1024*1024):check();result.update(chunk)
    return result.hexdigest()

class SnapshotChanged(RuntimeError):pass

def stamp(path):
    stat=path.stat();return (stat.st_ino,stat.st_size,stat.st_mtime_ns)

def inventory(root):
    result={}
    for path in root.rglob('*'):
        relative=path.relative_to(root)
        if any(part in SKIP for part in relative.parts) or path.is_symlink() or not path.is_file():continue
        if path.name.endswith(('-wal','-shm','.tmp','.lock')):continue
        if not path.resolve().is_relative_to(root.resolve()):continue
        result[relative.as_posix()]=(path,stamp(path))
    return result

def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('w',encoding='utf-8') as out:json.dump(value,out,ensure_ascii=False);out.flush();os.fsync(out.fileno())
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)

def remove_snapshot(path,root):
    path=path.resolve();root=root.resolve()
    if path.parent!=root or path.is_symlink() or not (path/'manifest.json').is_file():raise ValueError('invalid owned snapshot directory')
    manifest=json.loads((path/'manifest.json').read_text('utf-8'))
    if manifest.get('format')!='qqbot-data-snapshot-v1':raise ValueError('unrecognized snapshot')
    shutil.rmtree(path)

def retain(root):
    with backup_lock(root):return _retain(root)

def _retain(root):
    root=Path(root);items=[]
    for path in (root/'snapshots').glob('*/manifest.json'):
        try:
            value=json.loads(path.read_text('utf-8'))
            if value.get('format')=='qqbot-data-snapshot-v1' and value.get('complete'):items.append((value['at'],path.parent,value))
        except (OSError,ValueError,KeyError):continue
    items.sort(reverse=True,key=lambda x:x[0]);keep={p for _,p,_ in items[:24]};days=set()
    for at,path,_ in items:
        day=datetime.fromtimestamp(at,ZONE).date().isoformat()
        if day not in days and len(days)<7:days.add(day);keep.add(path)
    for _,path,_ in items:
        if path not in keep:remove_snapshot(path,root/'snapshots')
    # Blobs are immutable and shared by retained snapshots; no other files are touched.
    # Preserve references in incomplete manifests too. An unreadable manifest
    # means references are unknown, so do not reclaim shared data this pass.
    referenced=set()
    for path in (root/'snapshots').glob('*/manifest.json'):
        try:
            m=json.loads(path.read_text('utf-8'))
            if m.get('format')!='qqbot-data-snapshot-v1':return len(keep)
            referenced.update(v['sha256'] for v in m['files'].values() if v.get('kind')=='blob')
        except (OSError,ValueError,KeyError):return len(keep)
    for path in (root/'blobs').glob('*'):
        if len(path.name)==64 and all(c in '0123456789abcdef' for c in path.name) and path.name not in referenced and path.is_file() and not path.is_symlink():path.unlink()
    return len(keep)

def snapshot(source,destination,*,cancel=None,clock=time.time,hook=None):
    with backup_lock(destination):return _snapshot(source,destination,cancel=cancel,clock=clock,hook=hook)

def _snapshot(source,destination,*,cancel=None,clock=time.time,hook=None):
    source=Path(source).resolve();destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=True)
    cancel=cancel or threading.Event();started=clock();deadline=time.monotonic()+120
    def check():
        if cancel.is_set():raise SnapshotChanged('前台任务到来，备份延期')
        if time.monotonic()>deadline:raise TimeoutError('data snapshot deadline')
    files=inventory(source);name=datetime.fromtimestamp(started,ZONE).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    folder=destination/'snapshots'/name;folder.mkdir(parents=True,exist_ok=False)
    blobs=destination/'blobs';blobs.mkdir(exist_ok=True)
    manifest={'format':'qqbot-data-snapshot-v1','at':started,'complete':False,'files':{},
        'consistency':'all source SQLite data_version, file identity and inventory unchanged across the snapshot',
        'scope':'AstrBot/data excluding plugin source, logs, temporary/cache files; assets included as immutable shared blobs',
        'restore_policy':'restore into a new directory; never replay unknown deliveries or overwrite newer confirmed facts'}
    atomic_json(folder/'manifest.json',manifest)
    cachefile=destination/'blob-cache.json'
    try:cache=json.loads(cachefile.read_text('utf-8'))
    except (OSError,ValueError):cache={}
    newcache={}
    try:
        with ExitStack() as stack:
            databases={}
            for rel,(path,identity) in files.items():
                check()
                if path.suffix.lower() not in DATABASE:continue
                db=stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.1)))
                databases[rel]=(db,db.execute('PRAGMA data_version').fetchone()[0])
            for rel,(path,identity) in files.items():
                check()
                if rel in databases:
                    saved=folder/'databases'/(hashlib.sha256(rel.encode()).hexdigest()+'.sqlite3');saved.parent.mkdir(exist_ok=True)
                    with closing(sqlite3.connect(saved)) as target:
                        databases[rel][0].backup(target,pages=256,progress=lambda *args:check(),sleep=.01)
                        if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('invalid database backup')
                    # Windows _commit requires a writable descriptor, even when
                    # SQLite has already closed its connection to the snapshot.
                    with saved.open('rb+') as stream:os.fsync(stream.fileno())
                    manifest['files'][rel]={'kind':'sqlite','file':saved.relative_to(folder).as_posix(),'sha256':hashlib.sha256(saved.read_bytes()).hexdigest()}
                else:
                    previous=cache.get(rel,{})
                    digest=previous.get('sha256') if previous.get('stamp')==list(identity) else None
                    valid=bool(digest and (blobs/digest).is_file() and not (blobs/digest).is_symlink() and file_digest(blobs/digest,check)==digest)
                    if not valid:
                        temp=blobs/('pending-'+uuid.uuid4().hex);hasher=hashlib.sha256()
                        try:
                            with path.open('rb') as src,temp.open('xb') as dst:
                                while chunk:=src.read(1024*1024):check();hasher.update(chunk);dst.write(chunk)
                                dst.flush();os.fsync(dst.fileno())
                            digest=hasher.hexdigest()
                            # A pre-existing blob with this name may be corrupt.
                            os.replace(temp,blobs/digest)
                        finally:temp.unlink(missing_ok=True)
                    newcache[rel]={'stamp':list(identity),'sha256':digest}
                    manifest['files'][rel]={'kind':'blob','sha256':digest,'size':identity[1]}
            if hook:hook()
            check()
            current=inventory(source)
            if set(files)!=set(current) or any(identity!=current[rel][1] for rel,(_,identity) in files.items()):raise SnapshotChanged('备份期间数据文件发生变化')
            if any(db.execute('PRAGMA data_version').fetchone()[0]!=version for db,version in databases.values()):raise SnapshotChanged('备份期间数据库有新提交')
        for item in manifest['files'].values():
            if item['kind']=='blob' and file_digest(blobs/item['sha256'],check)!=item['sha256']:
                raise SnapshotChanged('共享素材校验失败，快照未标记完成')
        manifest.update(complete=True,completed_at=clock());atomic_json(folder/'manifest.json',manifest)
        atomic_json(cachefile,newcache);count=_retain(destination)
        return {'snapshot':name,'files':len(files),'retained':count,'at':started,'consistent':True}
    except BaseException:
        remove_snapshot(folder,destination/'snapshots');raise

def restore(manifest_path,destination):
    manifest_path=Path(manifest_path).resolve();folder=manifest_path.parent;root=folder.parent.parent
    manifest=json.loads(manifest_path.read_text('utf-8'));destination=Path(destination).resolve()
    if manifest.get('format')!='qqbot-data-snapshot-v1' or not manifest.get('complete'):raise ValueError('incomplete backup')
    if destination.exists():raise FileExistsError('restore destination must be a new directory')
    entries=[]
    for relative,item in manifest['files'].items():
        target=(destination/relative).resolve()
        if not target.is_relative_to(destination):raise ValueError('unsafe restore path')
        source=(folder/item['file']).resolve() if item['kind']=='sqlite' else (root/'blobs'/item['sha256']).resolve()
        if not source.is_relative_to(root) or hashlib.sha256(source.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('backup integrity mismatch')
        entries.append((source,target))
    destination.mkdir()
    for source,target in entries:target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    return {'files':len(entries),'directory':str(destination),'replayed':False}

class HourlyBackup:
    def __init__(self,plugin):
        self.plugin=plugin;self.source=plugin._data_dir.parent.parent
        self.destination=self.source.parent.parent/'backups'/'data-hourly'
        self.task=None;self.io=None;self.cancel=threading.Event();self.state={'status':'waiting','last_success':0,'first_started':time.time()}
        try:self.state.update(json.loads((self.destination/'state.json').read_text('utf-8')))
        except (OSError,ValueError):pass
    async def start(self):self.task=asyncio.create_task(self._worker(),name='hourly-consistent-backup')
    async def run_once(self):
        self.cancel.clear();self.state.update(status='running',error=None)
        try:
            # Bring memory-first facts up to a durable checkpoint before the
            # multi-database consistency window. A new chat postpones the copy.
            async with asyncio.timeout(5):
                for name in ('_delivery','_image_memory','_ai_profiles','_life','_meme_usage'):
                    store=getattr(getattr(self.plugin,name,None),'store',None)
                    if store and not store.closed:
                        for key in list(store.pending):await store.flush(key)
                for name in ('_learning','_sent_memory'):
                    persistence=getattr(getattr(self.plugin,name,None),'_persistence',None)
                    if persistence and await persistence.flush() is False:raise RuntimeError('snapshot checkpoint not durable')
            if self.plugin._work.busy():raise SnapshotChanged('前台任务到来，备份延期')
            self.io=asyncio.create_task(asyncio.to_thread(snapshot,self.source,self.destination,cancel=self.cancel))
            while not self.io.done():
                if self.plugin._work.busy():self.cancel.set()
                await asyncio.wait({self.io},timeout=.25)
            result=await self.io;self.state.update(status='ready',last_success=time.time(),result=result)
        except SnapshotChanged as exc:self.state.update(status='deferred',error=str(exc))
        except Exception as exc:self.state.update(status='failed',error=type(exc).__name__)
        await asyncio.to_thread(atomic_json,self.destination/'state.json',self.state)
    async def _worker(self):
        last_gc=0
        while True:
            await asyncio.sleep(30)
            if time.time()-last_gc>=3600 and not self.plugin._work.busy():
                await asyncio.to_thread(retain,self.destination);last_gc=time.time()
            if not self.plugin._cfg('hourly_data_backup_enabled',True) or self.plugin._work.busy():continue
            if time.time()-self.state.get('last_success',0)>=3600:await self.run_once()

    def health(self):
        now=time.time();last=self.state.get('last_success',0);first=self.state.get('first_started',now)
        age=now-(last or first)
        stopped=bool(self.task and self.task.done())
        level='abnormal' if stopped or age>=21600 else 'warning' if age>=7200 else 'grace' if not last and age<5400 else 'observing' if not last else 'ok'
        return {**self.state,'health':level,'age_seconds':age,'task_alive':bool(self.task and not self.task.done()),'auto_restart':False}
    async def close(self):
        self.cancel.set()
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        if self.io:
            try:await asyncio.wait_for(asyncio.shield(self.io),8)
            except SnapshotChanged:pass
