"""Authenticated local dashboard client; never prints credentials or tokens."""
from pathlib import Path
import base64
import hashlib
import hmac
import json
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]

def api(path, payload=None, method=None, timeout=180):
    cfg = json.loads((ROOT/'AstrBot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
    dashboard = cfg['dashboard']
    encode = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=')
    head = encode(b'{"alg":"HS256","typ":"JWT"}')
    body = encode(json.dumps({'username': dashboard['username'], 'exp': int(time.time())+600}).encode())
    unsigned = head+b'.'+body
    token = (unsigned+b'.'+encode(hmac.new(dashboard['jwt_secret'].encode(),unsigned,hashlib.sha256).digest())).decode()
    req = urllib.request.Request('http://127.0.0.1:6185'+path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}, method=method)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as response:
        return json.load(response)

if __name__ == '__main__':
    import sys
    result=api(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv)>2 else None)
    print(json.dumps(result, ensure_ascii=False))
