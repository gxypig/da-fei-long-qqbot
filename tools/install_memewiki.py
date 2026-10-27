"""Install the pinned public dictionary; never replace an existing database.

Code: in1t1a1/astrbot_plugin_memewiki (MIT). Dataset: cnmeme.wiki and
contributors; see THIRD_PARTY_NOTICES.md. No account or chat data is downloaded.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from urllib.request import Request, urlopen

REVISION = '386dd63ed889251253576d44066c6de8d476f4f7'
URL = f'https://raw.githubusercontent.com/in1t1a1/astrbot_plugin_memewiki/{REVISION}/data/memes.db'
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


def install(root=ROOT, source=None):
    target = Path(root) / RELATIVE
    if target.exists():
        return {'status': 'preserved_existing', 'entries': validate(target)}
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.memewiki-', dir=target.parent) as directory:
        temporary = Path(directory) / 'memes.db'
        if source:
            if Path(source).stat().st_size > MAX_BYTES:raise ValueError('Dictionary too large')
            shutil.copyfile(source, temporary)
        else:
            request = Request(URL, headers={'User-Agent': 'QQBot-dictionary-installer'})
            with urlopen(request, timeout=45) as response, temporary.open('wb') as output:
                size = 0
                while chunk := response.read(256 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:raise ValueError('Dictionary exceeds size limit')
                    output.write(chunk)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        if digest != SHA256:raise ValueError('Dictionary SHA256 mismatch; refusing installation')
        count = validate(temporary)
        # Publish a fully verified file atomically without replacing a winner.
        # Temporary and destination files are on the same filesystem.
        try:
            os.link(temporary, target)
        except FileExistsError:
            return {'status': 'preserved_existing', 'entries': validate(target)}
        (target.parent / 'source.json').write_text(json.dumps({
            'upstream': 'https://github.com/in1t1a1/astrbot_plugin_memewiki',
            'revision': REVISION, 'sha256': digest, 'dataset': 'cnmeme.wiki/contributors',
            'entries': count}, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'status': 'installed', 'entries': count, 'revision': REVISION}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--source', type=Path, help='Offline copy of the exact pinned public memes.db')
    args = parser.parse_args()
    print(json.dumps(install(args.root, args.source), ensure_ascii=False))
