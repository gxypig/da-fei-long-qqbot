"""Audit the source tree and verify the bundled plugin source hashes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
SKIP={'.git','.venv','local','logs','backups','__pycache__','dist'}
ALLOWED_DATA='AstrBot/data/plugins/'


def files(root=ROOT):
    for p in root.rglob('*'):
        if not p.is_file(): continue
        rel=p.relative_to(root)
        if any(part in SKIP for part in rel.parts): continue
        name=rel.as_posix()
        if name.startswith('AstrBot/data/') and not name.startswith(ALLOWED_DATA): continue
        if name.startswith(ALLOWED_DATA) and 'data' in rel.parts[3:]: continue
        if p.suffix in {'.pyc','.log','.db'}: continue
        yield p


def manifest(root=ROOT):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files(root) if p.relative_to(root).as_posix().startswith(ALLOWED_DATA)}


def audit(root=ROOT):
    errors=[]
    for p in files(root):
        rel=p.relative_to(root).as_posix()
        if p.suffix.lower() in {'.exe','.dll','.node','.db','.pem','.key'} or p.name=='.env':
            errors.append('Forbidden runtime/credential file: '+rel)
        text=p.read_text(encoding='utf-8-sig',errors='replace')
        if re.search(r'(?<![A-Za-z0-9])(?:ghp_|github_pat_|sk-proj-)[A-Za-z0-9_-]{20,}',text):
            errors.append('Possible credential: '+rel)
        if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',text):
            errors.append('Private key: '+rel)
        if re.search(r'[A-Z]:[\\/]养大肥鱼',text):
            errors.append('Private deployment path: '+rel)
    # Check tracked files too: ignore rules do not untrack a leaked file.
    if (root/'.git').exists():
        result=subprocess.run(['git','ls-files','-z'],cwd=root,capture_output=True,check=True)
        allowed={p.relative_to(root).as_posix() for p in files(root)}
        for item in result.stdout.decode().split('\0'):
            if item and item not in allowed: errors.append('Tracked private/runtime file: '+item)
    return errors


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--write-manifest',action='store_true');args=parser.parse_args()
    errors=audit()
    path=ROOT/'SOURCE_MANIFEST.json'
    actual=manifest()
    if args.write_manifest and not errors:
        path.write_text(json.dumps({'format':1,'files':actual},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if not path.exists() or json.loads(path.read_text(encoding='utf-8'))['files']!=actual:
        errors.append('Plugin manifest is missing or changed; review source then regenerate')
    if errors: raise SystemExit('\n'.join(errors))
    print(f'Source audit passed; {len(actual)} bundled plugin files verified. No runtime data included.')
