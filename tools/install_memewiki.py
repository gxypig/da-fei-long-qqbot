"""Install the public catalog or a verified offline dictionary, without overwrite.

Code: in1t1a1/astrbot_plugin_memewiki (MIT). Dataset: cnmeme.wiki and
contributors; see THIRD_PARTY_NOTICES.md. No account or chat data is downloaded.
"""
import argparse
from contextlib import closing
import hashlib
import gzip
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from urllib.error import HTTPError,URLError
from urllib.request import Request, urlopen

REVISION = '386dd63ed889251253576d44066c6de8d476f4f7'
URL = 'https://cnmeme.wiki/api/v1/public/memes'
SHA256 = '05d902ff642dbb894fe2fa61b7623ca9c094cc2e6d26d2f818986296cdacdef3'
ROOT = Path(__file__).resolve().parents[1]
RELATIVE = 'AstrBot/data/plugin_data/astrbot_plugin_proactive_agent/memewiki/memes.db'
MAX_BYTES = 32 * 1024 * 1024


def validate(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Dictionary integrity check failed; existing file was not changed')
        columns = {row[1] for row in db.execute('PRAGMA table_info(memes)')}
        if not {'title', 'slug', 'summary', 'usage', 'origin', 'other'} <= columns:
            raise ValueError('Unexpected dictionary schema; existing file was not changed')
        return db.execute('SELECT count(*) FROM memes').fetchone()[0]


def bootstrap_catalog(path):
    """Download public titles/definitions; full revisions are learned in idle time.

    The old GitHub binary URL disappeared. Do not install an unverified mirror.
    A failed or repeated page aborts this staging DB, never the installed one.
    """
    offset=0;seen=set();last=0
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE memes(title TEXT PRIMARY KEY,slug TEXT,tags TEXT,summary TEXT,usage TEXT,origin TEXT,other TEXT)')
        while True:
            for attempt in range(3):
                time.sleep(max(0,1-(time.monotonic()-last)));last=time.monotonic()
                try:
                    request=Request(URL+'?limit=100&offset='+str(offset),headers={'User-Agent':'QQBot-dictionary-installer/2.0'})
                    with urlopen(request,timeout=20) as response:
                        raw=response.read(MAX_BYTES+1)
                        if len(raw)>MAX_BYTES:raise ValueError('Dictionary response too large')
                        data=json.loads(raw)
                    break
                except HTTPError as exc:
                    if attempt==2 or exc.code not in {429,500,502,503,504}:raise
                    retry=exc.headers.get('Retry-After','')
                    try:delay=max(1,float(retry))
                    except ValueError:
                        from email.utils import parsedate_to_datetime
                        try:delay=max(1,parsedate_to_datetime(retry).timestamp()-time.time())
                        except (TypeError,ValueError):delay=2**attempt
                    time.sleep(delay)
                except (URLError,TimeoutError):
                    if attempt==2:raise
                    time.sleep(2**attempt)
            items=data.get('items');more=data.get('has_more')
            if not isinstance(items,list) or type(more) is not bool or data.get('offset',offset)!=offset or more and not items:
                raise ValueError('Incomplete dictionary pagination; nothing installed')
            for item in items:
                if not isinstance(item,dict) or not item.get('slug') or not item.get('title') or item['slug'] in seen:
                    raise ValueError('Invalid/repeated dictionary entry; nothing installed')
                seen.add(item['slug'])
                if len(seen)>100000:raise ValueError('Too many dictionary entries')
                db.execute('INSERT OR REPLACE INTO memes VALUES (?,?,?,?,?,?,?)',
                    (item['title'],item['slug'],json.dumps(item.get('tags',[]),ensure_ascii=False),item.get('summary',''),'','',''))
            if not more:break
            offset+=len(items)
    if not seen:raise ValueError('Empty dictionary response; nothing installed')

def install(root=ROOT, source=None, sha256=None):
    target = Path(root) / RELATIVE
    if target.exists():
        return {'status': 'preserved_existing', 'entries': validate(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.memewiki-', dir=target.parent) as directory:
        temporary = Path(directory) / 'memes.db'
        if source:
            if Path(source).stat().st_size > MAX_BYTES:raise ValueError('Dictionary too large')
            data=Path(source).read_bytes()
            if data[:2]==b'\x1f\x8b':
                with gzip.open(source,'rb') as stream:data=stream.read(MAX_BYTES+1)
            if len(data)>MAX_BYTES:raise ValueError('Uncompressed dictionary too large')
            temporary.write_bytes(data)
        else:
            bootstrap_catalog(temporary)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        if source and digest != (sha256 or SHA256):raise ValueError('Dictionary SHA256 mismatch; refusing installation')
        count = validate(temporary)
        # Publish a fully verified file atomically without replacing a winner.
        # Temporary and destination files are on the same filesystem.
        try:
            os.link(temporary, target)
        except FileExistsError:
            return {'status': 'preserved_existing', 'entries': validate(target)}
        (target.parent / 'source.json').write_text(json.dumps({
            'upstream': 'https://github.com/in1t1a1/astrbot_plugin_memewiki',
            'revision': REVISION if source else 'public-api-catalog', 'sha256': digest, 'dataset': 'cnmeme.wiki/contributors',
            'downloaded_at':time.time(),'source':str(source) if source else URL,'content': 'offline snapshot' if source else 'titles and summaries; full content updated in background',
            'entries': count}, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'status': 'installed', 'entries': count, 'revision': REVISION if source else 'public-api-catalog'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--source', type=Path, help='Verified offline memes.db or .db.gz')
    parser.add_argument('--sha256',help='Trusted SHA256 of the uncompressed offline database; defaults to the legacy verified snapshot')
    args = parser.parse_args()
    print(json.dumps(install(args.root, args.source,args.sha256), ensure_ascii=False))
