"""Primary public meme dictionary, adapted from in1t1a1/astrbot_plugin_memewiki.

Upstream 386dd63ed889251253576d44066c6de8d476f4f7 (v1.5.2), MIT.
See MEMEWIKI_LICENSE.md. The dataset belongs to cnmeme.wiki/contributors.
Keep upstream's full-title matching, local-first lookup and public API format;
route new-word network work through the existing idle learner, never preflight chat.
"""
import asyncio
from collections import OrderedDict
from contextlib import closing
import html
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import unicodedata
from urllib.parse import quote
import aiohttp

UPSTREAM='https://github.com/in1t1a1/astrbot_plugin_memewiki'
REVISION='386dd63ed889251253576d44066c6de8d476f4f7'
# The former upstream snapshot URL now returns 404. Bootstrap uses the public
# API updater or a checksum-verified offline package, never an unchecked mirror.
SNAPSHOT_URL=''
SNAPSHOT_SHA256='05d902ff642dbb894fe2fa61b7623ca9c094cc2e6d26d2f818986296cdacdef3'
API_BASE='https://cnmeme.wiki/api/v1/public/memes'


def folded(value):return unicodedata.normalize('NFKC',str(value or '')).casefold()


def plain_html(value):
    value=re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>','',str(value),flags=re.S|re.I)
    return html.unescape(re.sub(r'<[^>]+>',' ',value)).strip()


class MemeWikiEngine:
    def __init__(self,path):
        self.path=Path(path);self.titles={};self.cache=OrderedDict();self.signature=None
        self.local_entries={};self.by_slug={};self.index=None
        self.stats={'local_hits':0,'remote_hits':0,'snapshot_ready':False}

    async def start(self):
        # Only local startup work; deployment installs the pinned snapshot.
        await asyncio.to_thread(self.refresh)

    def refresh(self):
        try:
            stat=self.path.stat();signature=(stat.st_mtime_ns,stat.st_size)
        except OSError as exc:
            self.stats.update(last_error=type(exc).__name__)
            return False
        if signature==self.signature:return self.stats['snapshot_ready']
        try:
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory=sqlite3.Row
                data=[dict(row) for row in db.execute('SELECT * FROM memes')]
                titles={folded(row['title']):row['title'] for row in data}
                entries={folded(entry['phrase']):entry for row in data if (entry:=self.entry(row))}
                from .meme_index import MemeIndex
                index=MemeIndex(entries.values())
        except (OSError,sqlite3.Error) as exc:
            self.stats.update(last_error=type(exc).__name__)
            return False
        self.titles=dict(sorted(titles.items(),key=lambda item:-len(item[0])))
        self.local_entries=entries;self.by_slug={r['wiki_slug']:r for r in entries.values() if r.get('wiki_slug')};self.index=index
        self.cache.clear();self.signature=signature;self.stats['snapshot_ready']=True
        return True

    @staticmethod
    def entry(data):
        title=str(data.get('title') or '')
        meaning=str(data.get('summary') or data.get('other') or data.get('usage') or data.get('origin') or '')
        usage=str(data.get('usage') or '')
        if not title or not meaning.strip():return None
        url='https://cnmeme.wiki/词条/'+quote(str(data.get('slug') or title),safe='')
        result={'phrase':title,'meaning':plain_html(meaning)[:300],'usage':plain_html(usage)[:240],
                'examples':[],'sources':[{'title':title+' · cnmeme.wiki','url':url}],
                'provenance':'memewiki','verified_at':time.time(),'wiki_slug':data.get('slug',''),
                'source_hash':hashlib.sha256(json.dumps({k:data.get(k) for k in ('title','slug','summary','origin','usage','other','tags')},sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()}
        for key in ('aliases','tags'):
            value=data.get(key,[])
            if isinstance(value,str):
                try:value=json.loads(value)
                except ValueError:value=re.split(r'[,，;；|]',value)
            if isinstance(value,list):result[key]=[plain_html(v)[:40] for v in value[:16] if isinstance(v,str) and v.strip()]
        return result

    def _read(self,title):
        if not self.stats['snapshot_ready']:return None
        if folded(title) in self.local_entries:return dict(self.local_entries[folded(title)])
        if title in self.cache:
            self.cache.move_to_end(title);return dict(self.cache[title])
        try:
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory=sqlite3.Row
                row=db.execute('SELECT * FROM memes WHERE title=?',(title,)).fetchone()
        except (OSError,sqlite3.Error) as exc:
            self.stats.update(last_error=type(exc).__name__)
            return None
        entry=self.entry(dict(row)) if row else None
        if entry:
            self.cache[title]=entry
            while len(self.cache)>128:self.cache.popitem(last=False)
        return dict(entry) if entry else None

    def match(self,text,limit=3):
        if not self.stats['snapshot_ready']:return []
        result=self.index.search(text,limit=limit,direct_only=True) if self.index else []
        self.stats['local_hits']+=bool(result)
        return result

    def exact(self,phrase):
        if not self.stats['snapshot_ready']:return None
        title=self.titles.get(folded(phrase))
        return self._read(title) if title else None

    def explicit_known(self,text):
        query=text.strip().strip('？?！!。')
        query=re.sub(r'^(?:什么是|解释(?:一下)?|[查问](?:一下)?(?:梗)?[：: ]*)','',query)
        query=re.sub(r'(?:是?(?:什么|啥)(?:意思|梗)|是什么意思)$','',query).strip(' “”"')
        row=self.exact(query)
        return [row['phrase']] if row and row['phrase'] in text and len(row['phrase'])>=2 else None

    async def remote(self,phrase):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),headers={'User-Agent':'AstrBot-memewiki/1.5.2 QQBot-integration'}) as session:
            async with session.get(API_BASE,params={'q':phrase,'limit':5}) as response:
                response.raise_for_status();data=await response.json()
            items=data.get('items',[]) if isinstance(data,dict) else []
            picked=next((x for x in items if x.get('title')==phrase),None)
            if picked is None:
                picked=next((x for x in items if len(str(x.get('title','')))>=2 and x['title'] in phrase),None)
            if not picked:return None
            slug=picked.get('slug')
            if slug:
                async with session.get(API_BASE+'/'+quote(str(slug),safe='')) as response:
                    response.raise_for_status();detail=await response.json()
                picked=(detail.get('item') if isinstance(detail,dict) else None) or picked
            body=plain_html(picked.get('content_html',''))
            row=self.entry({**picked,'other':body,'usage':picked.get('usage','') or body})
            if row:self.stats['remote_hits']+=1
            return row

    def catalog(self,query='',exclude=(),offset=0,limit=30):
        if not self.stats['snapshot_ready']:return {'total':0,'rows':[]}
        excluded={folded(x) for x in exclude}
        # SQL restricts the search; only the selected page is materialized as entries.
        where='';args=[]
        if query:
            where=" WHERE instr(lower(title || ' ' || coalesce(usage,'') || ' ' || coalesce(origin,'') || ' ' || coalesce(summary,'')), ?) > 0"
            args=[query.casefold()]
        try:
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)) as db:
                titles=[r[0] for r in db.execute('SELECT title FROM memes'+where+' ORDER BY title',args) if folded(r[0]) not in excluded]
        except (OSError,sqlite3.Error) as exc:
            self.stats.update(last_error=type(exc).__name__)
            return {'total':0,'rows':[]}
        rows=[row for title in titles[offset:offset+limit] if (row:=self._read(title))]
        return {'total':len(titles),'rows':rows}

    def status(self):
        return {**self.stats,'entries':len(self.titles),'upstream':UPSTREAM,'revision':REVISION,
                'mode':'local_dictionary_and_idle_learning'}

    def detail(self,phrase):
        if not self.stats['snapshot_ready']:return None
        title=self.titles.get(folded(phrase))
        if not title:return None
        try:
            with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)) as db:
                db.row_factory=sqlite3.Row
                row=db.execute('SELECT * FROM memes WHERE title=?',(title,)).fetchone()
        except (OSError,sqlite3.Error):return None
        return {k:plain_html(row[k])[:20000] for k in ('title','summary','origin','usage','other') if k in row.keys() and row[k]} if row else None
