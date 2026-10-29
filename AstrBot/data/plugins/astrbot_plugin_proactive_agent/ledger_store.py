"""Bounded delivery bodies; complete lightweight index and durable cold history.

Eviction never deletes a SQLite row or its revision. Pending writes remain pinned.
Foreground lookups use a per-origin index; admin/recovery reads load cold batches
off the event loop. The mapping interface keeps older extensions compatible.
"""
import asyncio
from collections import OrderedDict,defaultdict
from collections.abc import MutableMapping
from contextlib import closing
import json,sqlite3
from .record_store import AsyncRecordStore

FIELDS=('state','origin','cid','at','result_at','confirmed_at','optional','purpose',
        'history_saved','submission_state','source_id','sender','text_hash','message_id','delivery_result','reason')
ANY=object()

def metadata(key,row):
    result={k:row.get(k) for k in FIELDS}
    kinds={a.get('kind') for a in row.get('attachments',[]) if isinstance(a,dict)}
    return {**result,'id':key,'revision':row.get('revision',0),'attachments':bool(row.get('attachments')),
            'voice':'record' in kinds,'image':'image' in kinds,'has_meme':bool(row.get('meme_plan')),
            'token':row.get('external_operation',{}).get('token')}

class LedgerRows(MutableMapping):
    def __init__(self,store):self.store=store
    def __len__(self):return len(self.store.index)
    def __iter__(self):return iter(self.store.index)
    def __contains__(self,key):return key in self.store.index
    def __getitem__(self,key):
        store=self.store
        if key not in store.index:raise KeyError(key)
        if key not in store.cache:store.cache[key]=store._read_many([key])[key]
        row=store.cache[key];store.cache.move_to_end(key);store._trim()
        return row
    def __setitem__(self,key,row):
        store=self.store;previous=store.index.get(key,{})
        store.origins[previous.get('origin')].discard(key)
        store.index[key]=metadata(key,row);store.origins[row.get('origin')].add(key)
        store.cache[key]=row;store.cache.move_to_end(key)
    def __delitem__(self,key):raise TypeError('Delivery facts may not be deleted by cache eviction')

class LedgerStore(AsyncRecordStore):
    def __init__(self,path,limit=5000,hot_limit=5000):
        super().__init__(path,limit)
        self.hot_limit=hot_limit;self.cache=OrderedDict();self.index={};self.origins=defaultdict(set)
        self.rows=LedgerRows(self)

    def _load(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        columns=','.join("json_extract(body,'$."+key+"')" for key in FIELDS)
        with closing(sqlite3.connect(self.path,timeout=1)) as db,db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL)')
            sql=f'''SELECT id,revision,{columns},COALESCE(json_array_length(body,'$.attachments'),0)>0,
                EXISTS(SELECT 1 FROM json_each(body,'$.attachments') WHERE json_extract(value,'$.kind')='record'),
                EXISTS(SELECT 1 FROM json_each(body,'$.attachments') WHERE json_extract(value,'$.kind')='image'),
                json_type(body,'$.meme_plan') IS NOT NULL,json_extract(body,'$.external_operation.token') FROM records'''
            fields=('id','revision',*FIELDS,'attachments','voice','image','has_meme','token')
            index={values[0]:dict(zip(fields,values)) for values in db.execute(sql)}
            ids=sorted(index,key=lambda k:(index[k].get('at') or 0,k),reverse=True)[:self.hot_limit]
            cache=self._read_many(ids,db)
            return index,cache

    def _install_loaded(self,loaded):
        self.index,cache=loaded;self.cache=OrderedDict(cache)
        self.persisted={k:m['revision'] for k,m in self.index.items()}
        self.origins=defaultdict(set)
        for k,m in self.index.items():self.origins[m.get('origin')].add(k)
        self.rows=LedgerRows(self)

    def _read_many(self,keys,db=None):
        if not keys:return {}
        if db is None:
            with closing(sqlite3.connect(self.path,timeout=1)) as connection:return self._read_many(keys,connection)
        result={}
        for offset in range(0,len(keys),400):
            batch=keys[offset:offset+400]
            result.update((k,json.loads(body)) for k,body in db.execute('SELECT id,body FROM records WHERE id IN ('+','.join('?' for _ in batch)+')',batch))
        return result

    def _trim(self):
        excess=len(self.cache)-self.hot_limit
        if excess<=0:return
        for key in list(self.cache):
            if key in self.pending or key not in self.persisted:continue
            del self.cache[key];excess-=1
            if excess<=0:break

    def select_ids(self,*,origin=ANY,states=None,predicate=None,sort='at',reverse=False,offset=0,limit=None):
        keys=self.index if origin is ANY else self.origins.get(origin,())
        keys=[k for k in keys if (states is None or self.index[k].get('state') in states)
              and (predicate is None or predicate(self.index[k]))]
        if sort:keys.sort(key=lambda k:(self.index[k].get(sort) or 0,k),reverse=reverse)
        return keys[max(0,offset):None if limit is None else max(0,offset)+limit]

    def select(self,**kwargs):return [self.rows[k] for k in self.select_ids(**kwargs)]

    def iter_select(self,**kwargs):
        for key in self.select_ids(**kwargs):yield self.rows[key]

    async def fetch(self,keys):
        cold=[k for k in keys if k not in self.cache]
        loaded=await asyncio.to_thread(self._read_many,cold) if cold else {}
        result=[]
        for key in keys:
            if key in self.cache:row=self.cache[key]
            elif key in loaded and loaded[key].get('revision')==self.index[key]['revision']:
                row=loaded[key];self.cache[key]=row
            else:row=self.rows[key] # newer completion won while disk I/O was pending
            result.append(row)
        self._trim();return result

    async def iterate(self,**kwargs):
        keys=self.select_ids(**kwargs)
        for offset in range(0,len(keys),100):
            for row in await self.fetch(keys[offset:offset+100]):yield row
            await asyncio.sleep(0)

    def snapshot(self):
        return {**super().snapshot(),'records':len(self.index),'hot_bodies':len(self.cache),
                'hot_limit':self.hot_limit,'pinned_pending':len(self.pending),'history_persistent':True}
