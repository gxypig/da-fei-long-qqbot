"""Evidence-only, repeatable quarantine of historical platform-rejected prose.

The framework conversation remains byte-for-byte intact. A private sidecar marks
an exact conversation position + text + predecessor hash, not every occurrence
of the same sentence. Ambiguous matches are reported, never guessed.
"""
import argparse
import ast
from collections import Counter,defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
ANSI=re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
LITERAL=re.compile(r'''text=("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''',re.S)

def digest(value):return hashlib.sha256(str(value).encode()).hexdigest()

def prose(row):
    content=row.get('content','')
    return content if isinstance(content,str) else '\n'.join(p.get('text','') for p in content or [] if isinstance(p,dict) and p.get('type')=='text')

def fingerprint(row):return digest(json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':')))

def failures(paths):
    records={}
    for path in paths:
        text=ANSI.sub('',path.read_text('utf-8',errors='replace'))
        for match in re.finditer(r'Failed to send the message chain:',text):
            block=text[match.start():match.start()+18000]
            end=block.find('echo=')
            if end<0:continue
            block=block[:end+100]
            if "status='failed'" not in block or 'ActionFailed' not in block:continue
            seq=re.search(r"['\"]seq['\"]:\s*(\d+)",block)
            if not seq:continue
            parts=[]
            for raw in LITERAL.findall(block):
                try:parts.append(ast.literal_eval(raw))
                except (SyntaxError,ValueError):pass
            if not parts:continue
            wording=re.search(r"wording='(.*?)',\s*echo",block,re.S)
            reason=wording[1] if wording else 'explicit_platform_rejection'
            before=text[max(0,match.start()-80):match.start()]
            stamp=re.findall(r'\[(\d\d:\d\d:\d\d\.\d+)\]',before)
            # The same failure printed twice across a copied boundary is one send.
            key=digest((stamp[-1] if stamp else '')+'|'+seq[1]+'|'+repr(parts))
            records[key]={'id':key,'file':path.name,'offset':match.start(),'time':stamp[-1] if stamp else '',
                          'seq':seq[1],'text':'\n'.join(parts),'text_hash':digest('\n'.join(parts)),'reason':reason}
    return list(records.values())

def scan(root):
    logs=[p for p in (root/'logs').glob('astrbot.out.log*') if p.is_file()]
    failed=failures(logs);by_hash=defaultdict(list)
    for item in failed:by_hash[item['text_hash']].append(item)
    matches=defaultdict(list)
    database=root/'AstrBot/data/data_v4.db'
    with closing(sqlite3.connect(f'file:{database.as_posix()}?mode=ro',uri=True)) as db:
        for cid,platform,origin,content in db.execute('SELECT conversation_id,platform_id,user_id,content FROM conversations'):
            if not content:continue
            rows=json.loads(content);rows=json.loads(rows) if isinstance(rows,str) else rows
            if not isinstance(rows,list):continue
            for index,row in enumerate(rows):
                if row.get('role')!='assistant' or row.get('tool_calls'):continue
                text=prose(row);hash_=digest(text)
                if hash_ not in by_hash:continue
                matches[hash_].append({'cid':cid,'origin':origin,'platform':platform,'position':index,
                    'text_hash':hash_,'row_hash':fingerprint(row),'previous_hash':fingerprint(rows[index-1]) if index else None})
    candidates=[];ambiguous=[]
    for hash_,items in by_hash.items():
        found=matches.get(hash_,[])
        # Repeated text can be quarantined only if every historical occurrence
        # is accounted for by a distinct explicit platform failure.
        if found and len(found)==len(items):
            for row,evidence in zip(found,items):candidates.append({**row,'evidence':evidence})
        elif found:ambiguous.append({'text_hash':hash_,'failures':len(items),'history_occurrences':len(found)})
    return {'created_at':time.time(),'rejected_sends':len(failed),'candidates':candidates,
            'ambiguous':ambiguous,'unmatched_failures':sum(len(v) for k,v in by_hash.items() if k not in matches),
            'reasons':dict(Counter('nonfriend' if '好友' in x['reason'] else 'identity' if 'device message' in x['reason'] or 'verify identify' in x['reason'] else 'other' for x in failed)),
            'failures':failed}

def apply(root,report):
    destination=root/'AstrBot/data/plugin_data/astrbot_plugin_proactive_agent/delivery-ledger.sqlite3'
    with closing(sqlite3.connect(destination,timeout=2)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,body TEXT NOT NULL)')
        count=0
        for item in report['candidates']:
            key='legacy:'+digest(item['cid']+':'+str(item['position'])+':'+item['row_hash'])
            row={**item,'id':key,'state':'legacy_undelivered','revision':1,'at':time.time()}
            count+=db.execute('INSERT OR IGNORE INTO records VALUES(?,?,?)',(key,1,json.dumps(row,ensure_ascii=False))).rowcount
    # Cancel/rebuild derived summaries only, preserving originals in the backup.
    cache=root/'AstrBot/data/plugin_data/astrbot_plugin_proactive_agent/context-summaries.json'
    if cache.exists():
        values=json.loads(cache.read_text('utf-8'))
        for item in report['candidates']:values.pop(digest(item['cid']),None)
        temp=cache.with_name(cache.name+'.'+uuid.uuid4().hex+'.migration.tmp')
        try:temp.write_text(json.dumps(values,ensure_ascii=False),encoding='utf-8');temp.replace(cache)
        finally:temp.unlink(missing_ok=True)
    return count

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--root',type=Path,default=ROOT)
    args=parser.parse_args();report=scan(args.root)
    path=args.root/'logs/delivery-history-migration.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    summary={k:len(v) if isinstance(v,list) else v for k,v in report.items() if k!='failures'}
    if args.apply:summary['inserted']=apply(args.root,report)
    print(json.dumps(summary,ensure_ascii=False))
