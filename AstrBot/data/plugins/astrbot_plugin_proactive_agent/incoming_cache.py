"""One-hour incoming image cache. Never owns gallery or arbitrary user files."""
import asyncio,base64,hashlib,json,re,time
from pathlib import Path

class IncomingCache:
    def __init__(self,root,clock=time.time,temp_root=None):
        self.root=Path(root).resolve();self.clock=clock;self.lock=asyncio.Lock();self.rows={};self.task=None
        self.temp_root=Path(temp_root).resolve() if temp_root else None
        self.stats={'retained':0,'expired':0,'errors':0};self.uri_keys={}
    def _load(self):
        self.root.mkdir(parents=True,exist_ok=True)
        try:
            data=json.loads((self.root/'index.json').read_text('utf-8'))
            self.rows={k:v for k,v in data.items() if isinstance(v,dict) and isinstance(v.get('expires_at'),(int,float)) and re.fullmatch(r'[0-9a-f]{64}\.[a-z]+',k)}
            self.uri_keys={key:k for k,row in self.rows.items() for key in row.get('uri_keys',[]) if isinstance(key,str)}
        except FileNotFoundError:pass
        except (ValueError,OSError,AttributeError):self.rows={};self.stats['errors']+=1
    def _save(self):
        path=self.root/'index.json';temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.rows,ensure_ascii=False),'utf-8');temp.replace(path)
    async def start(self):
        await asyncio.to_thread(self._load);await self.cleanup()
        self.task=asyncio.create_task(self._worker(),name='incoming-images-expiry')
    async def retain(self,path):
        async with self.lock:
            def copy():
                if Path(path).stat().st_size>24*1024*1024:raise ValueError('incoming_image_size_limit')
                raw=Path(path).read_bytes()
                if len(raw)>24*1024*1024:return str(path)
                digest=hashlib.sha256(raw).hexdigest();suffix=Path(path).suffix.lower()
                if suffix not in {'.png','.jpg','.jpeg','.gif','.webp','.bmp'}:suffix='.img'
                target=self.root/(digest+suffix)
                if not target.exists():target.write_bytes(raw)
                sources=self.rows.get(target.name,{}).get('sources',[])
                original=Path(path).resolve()
                if self.temp_root and original.is_relative_to(self.temp_root) and original!=target:
                    sources=list(dict.fromkeys([*sources,str(original)]))
                self.rows[target.name]={**self.rows.get(target.name,{}),'expires_at':self.clock()+3600,'bytes':len(raw),'sources':sources,'removed':False};self._save()
                self.stats['retained']+=1
                return str(target)
            return await asyncio.to_thread(copy)
    async def capture_message(self,message):
        """Only CURRENT incoming content. Persist bytes once, never an extra model call."""
        if message.get('role')!='user' or not isinstance(message.get('content'),list):return
        parts=[p for p in message['content'] if p.get('type')=='image_url']
        if not parts:return
        async with self.lock:
            def capture():
                changed=False
                for part in parts:
                    uri=part.get('image_url',{}).get('url','')
                    if not isinstance(uri,str) or not uri.startswith('data:image/') or ';base64,' not in uri:continue
                    key=hashlib.sha256(uri.encode()).hexdigest()
                    if key in self.uri_keys:continue
                    head,encoded=uri.split(';base64,',1)
                    if len(encoded)>34*1024*1024:continue
                    raw=base64.b64decode(encoded,validate=True);digest=hashlib.sha256(raw).hexdigest()
                    existing=next((n for n in self.rows if n.startswith(digest+'.')),None)
                    suffix={'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp'}.get(head[5:],'img')
                    name=existing or digest+'.'+suffix;path=self.root/name
                    if not existing:
                        path.write_bytes(raw);self.rows[name]={'expires_at':self.clock()+3600,'bytes':len(raw),'sources':[]}
                    row=self.rows[name];row.setdefault('uri_keys',[]).append(key);self.uri_keys[key]=name;changed=True
                if changed:self._save()
            await asyncio.to_thread(capture)
    def portable_history(self,history):
        """Replace only registered incoming bytes; keep gallery/assistant images intact."""
        if not isinstance(history,list) or not self.uri_keys:return history
        result=[]
        for message in history:
            if message.get('role')!='user' or not isinstance(message.get('content'),list):result.append(message);continue
            parts=[]
            for part in message['content']:
                uri=part.get('image_url',{}).get('url') if part.get('type')=='image_url' else None
                name=self.uri_keys.get(hashlib.sha256(uri.encode()).hexdigest()) if isinstance(uri,str) and uri.startswith('data:image/') else None
                if name:
                    row=self.rows[name]
                    part=({'type':'image_url','image_url':{**part['image_url'],'url':str(self.root/name)}}
                        if row['expires_at']>self.clock() else {'type':'text','text':'[聊天图片已过1小时缓存期，原图不可用]'})
                parts.append(part)
            result.append({**message,'content':parts})
        return result
    async def cleanup(self):
        async with self.lock:
            def clean():
                changed=False
                for name,row in list(self.rows.items()):
                    if row.get('expires_at',0)>self.clock():continue
                    path=(self.root/name).resolve()
                    # Check resolved containment and filename before EVERY delete.
                    if path.parent!=self.root or not __import__('re').fullmatch(r'[0-9a-f]{64}\.[a-z]+',name):
                        self.stats['errors']+=1;continue
                    for source in row.get('sources',[]):
                        original=Path(source).resolve()
                        if (self.temp_root and original.is_relative_to(self.temp_root) and original.is_file()
                            and hashlib.sha256(original.read_bytes()).hexdigest()==name.split('.')[0]):original.unlink()
                    path.unlink(missing_ok=True)
                    if not row.get('removed'):row['removed']=True;row['sources']=[];changed=True;self.stats['expired']+=1
                    # A bounded tombstone prevents a late history save from recreating expired bytes.
                    if row['expires_at']+86400<self.clock():
                        self.rows.pop(name);changed=True
                        for key in row.get('uri_keys',[]):self.uri_keys.pop(key,None)
                # Recover orphaned cache files after an interrupted atomic index write.
                for path in self.root.iterdir():
                    if (path.name not in self.rows and re.fullmatch(r'[0-9a-f]{64}\.[a-z]+',path.name)
                        and path.resolve().parent==self.root and path.stat().st_mtime+3600<=self.clock()):path.unlink()
                if changed:self._save()
            await asyncio.to_thread(clean)
    async def _worker(self):
        while True:
            await asyncio.sleep(30)
            try:await self.cleanup()
            except asyncio.CancelledError:raise
            except Exception:self.stats['errors']+=1
    def stop(self):
        if self.task:self.task.cancel()
