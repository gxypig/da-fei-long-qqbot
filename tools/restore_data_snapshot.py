"""Verify an hourly data snapshot and restore only into a NEW directory."""
import argparse
import importlib.util
import json
from pathlib import Path
import sqlite3
from contextlib import closing

ROOT=Path(__file__).resolve().parents[1]

def restore(manifest,destination):
    source=ROOT/'AstrBot/data/plugins/astrbot_plugin_proactive_agent/data_backup.py'
    spec=importlib.util.spec_from_file_location('qqbot_snapshot_restore',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=module.restore(manifest,destination)
    metadata=json.loads(Path(manifest).read_text('utf-8'))
    count=0
    for relative,entry in metadata['files'].items():
        if entry['kind']!='sqlite':continue
        path=(Path(destination)/relative).resolve()
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
                raise RuntimeError('Restored SQLite validation failed: '+relative)
        count+=1
    return {**result,'sqlite_verified':count}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest',help='Snapshot manifest.json; keep its sibling shared blobs directory')
    parser.add_argument('destination',help='New empty destination path that does not exist')
    args=parser.parse_args()
    print(json.dumps(restore(args.manifest,args.destination),ensure_ascii=False,indent=2))
