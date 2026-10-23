"""Optional Bilibili QR login and narrowly scoped account transport.

QR flow adapted from dsh-bili-scroll/lib/login.mjs (MIT; see NOTICE.md).
Credentials never enter plugin settings, model prompts, API responses or logs.
"""
import asyncio
import base64
import ctypes
import io
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit, urlencode

import aiohttp
import qrcode

from .client import BiliUnavailable, PATHS

COOKIE_NAMES = {'SESSDATA', 'bili_jct', 'DedeUserID', 'buvid3'}
READ_PATHS = PATHS | {'/x/web-interface/nav', '/x/v2/reply/main', '/x/v2/reply/reply',
                     '/x/space/acc/info', '/x/v3/fav/folder/created/list-all'}
WRITE_PATHS = {'/x/v2/reply/add', '/medialist/gateway/coll/resource/deal', '/web_im/v1/web_im/send_msg'}


def protect(data, decrypt=False):
    """Windows DPAPI binds stored login to the current Windows account."""
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('无法读取或保护本机B站登录，请重新扫码')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(ctypes.cast(target.data, ctypes.c_void_p))


class CredentialStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {}
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if saved['format'] == 'dpapi':
                if os.name != 'nt':
                    return {}
                return json.loads(protect(base64.b64decode(saved['blob']), decrypt=True))
            return saved['cookies'] if saved['format'] == 'local-json' else {}
        except Exception:
            return {}

    def save(self, cookies):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == 'nt':
            saved = {'format': 'dpapi', 'blob': base64.b64encode(protect(json.dumps(cookies).encode())).decode()}
        else:
            saved = {'format': 'local-json', 'cookies': cookies}
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(saved, stream)
        os.replace(temp, self.path)
        if os.name != 'nt':
            os.chmod(self.path, 0o600)

    def clear(self):
        self.path.unlink(missing_ok=True)


class AccountSession:
    def __init__(self, path):
        self.store = CredentialStore(path)
        saved = self.store.load()
        self.cookies = {k: v for k, v in saved.items() if k in COOKIE_NAMES and isinstance(v, str)
                        and not any(c in v for c in '\r\n;')} if isinstance(saved, dict) else {}
        self.invalid = False
        self.name = ''
        self.generation = 0
        self.qr = None
        self.session = None
        self.lock = asyncio.Lock()

    @property
    def ready(self):
        return bool(not self.invalid and self.cookies.get('SESSDATA') and self.cookies.get('bili_jct')
                    and str(self.cookies.get('DedeUserID', '')).isdigit())

    def identity(self):
        return (self.cookies.get('DedeUserID', ''), self.generation) if self.ready else ('anonymous', self.generation)

    async def close(self):
        if self.session:
            await self.session.close()

    async def _http(self, method, url, *, params=None, data=None, cookies=None):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),
                cookie_jar=aiohttp.DummyCookieJar(), headers={'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://www.bilibili.com/'})
        try:
            async with self.session.request(method, url, params=params, data=data, cookies=cookies or {},
                                            allow_redirects=False) as response:
                if response.status != 200:
                    raise BiliUnavailable(f'B站接口HTTP {response.status}，本次停止')
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 2_000_000:
                        raise BiliUnavailable('B站响应过大')
                result = json.loads(raw)
                received = {k: morsel.value for k, morsel in response.cookies.items() if k in COOKIE_NAMES}
                return result, received
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise BiliUnavailable('B站请求结果未确认，请稍后查看记录；不自动重复发送') from exc

    @staticmethod
    def _data(result):
        if result.get('code') != 0:
            raise BiliUnavailable(f'B站拒绝请求（code={result.get("code")}），请检查登录或风控提示')
        return result.get('data') or {}

    async def get(self, path, params=None, *, anonymous=False):
        if path not in READ_PATHS:
            raise ValueError('未授权的B站读取接口')
        if not anonymous and not self.ready:
            raise BiliUnavailable('B站未登录或登录已过期，请扫码')
        result, _ = await self._http('GET', 'https://api.bilibili.com' + path, params=params,
                                     cookies={} if anonymous else dict(self.cookies))
        if not anonymous and result.get('code') == -101:
            self.invalid = True
            self.generation += 1
        return self._data(result)

    async def post(self, path, data, *, expected_identity, final_check=None):
        if path not in WRITE_PATHS:
            raise ValueError('未授权的B站写入接口')
        if not self.ready or self.identity() != expected_identity:
            raise BiliUnavailable('账号已变更或登录失效，取消本次操作')
        csrf = self.cookies['bili_jct']
        host = 'https://api.vc.bilibili.com' if path.endswith('/send_msg') else 'https://api.bilibili.com'
        params = None
        if path.endswith('/send_msg'):
            nav = await self.get('/x/web-interface/nav')
            params = sign_dm(nav, data['msg[sender_uid]'], data['msg[receiver_id]'])
        if final_check:
            final_check()
        if not self.ready or self.identity() != expected_identity:
            raise BiliUnavailable('发送前账号已变更，已取消')
        result, _ = await self._http('POST', host + path,
            params=params, data={**data, 'csrf': csrf, 'csrf_token': csrf}, cookies=dict(self.cookies))
        if result.get('code') == -101:
            self.invalid = True
            self.generation += 1
        return self._data(result)

    async def status(self, refresh=False):
        if refresh and self.ready:
            try:
                data = await self.get('/x/web-interface/nav')
                if not data.get('isLogin') or str(data.get('mid')) != self.cookies.get('DedeUserID'):
                    self.invalid = True
                else:
                    self.name = str(data.get('uname') or '')[:80]
            except BiliUnavailable:
                return {'logged_in': self.ready, 'uid': self.cookies.get('DedeUserID', ''),
                        'name': self.name, 'verified': False, 'message': '暂未能核验登录状态'}
        return {'logged_in': self.ready, 'uid': self.cookies.get('DedeUserID', '') if self.ready else '',
                'name': self.name if self.ready else '', 'verified': self.ready and bool(self.name),
                'message': '已登录' if self.ready else '未登录或登录过期，可使用匿名浏览'}

    async def login_start(self):
        async with self.lock:
            result, _ = await self._http('GET', 'https://passport.bilibili.com/x/passport-login/web/qrcode/generate',
                                         params={'source': 'main-fe-header'})
            data = self._data(result)
            url = str(data.get('url') or '')
            if urlsplit(url).scheme != 'https' or urlsplit(url).hostname not in {'account.bilibili.com', 'passport.bilibili.com', 'www.bilibili.com', 'bilibili.com'}:
                raise BiliUnavailable('B站登录二维码地址异常')
            if not isinstance(data.get('qrcode_key'), str) or not data['qrcode_key']:
                raise BiliUnavailable('B站未返回登录二维码')
            self.qr = {'key': data['qrcode_key'], 'expires': time.monotonic() + 180}
            output = io.BytesIO()
            qrcode.make(url).save(output, format='PNG')
            return {'state': 'waiting', 'image': 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode(),
                    'expires_seconds': 180}

    async def login_poll(self):
        async with self.lock:
            if not self.qr or time.monotonic() >= self.qr['expires']:
                self.qr = None
                return {'state': 'expired'}
            result, cookies = await self._http('GET', 'https://passport.bilibili.com/x/passport-login/web/qrcode/poll',
                params={'qrcode_key': self.qr['key'], 'source': 'main-fe-header'})
            data = self._data(result)
            code = data.get('code')
            if code != 0:
                return {'state': {86101: 'waiting', 86090: 'scanned', 86038: 'expired'}.get(code, 'unknown')}
            if (not all(cookies.get(k) for k in ('SESSDATA', 'bili_jct', 'DedeUserID'))
                    or not cookies['DedeUserID'].isdigit()
                    or any(any(c in value for c in '\r\n;') for value in cookies.values())):
                raise BiliUnavailable('登录回执不完整，请重新扫码；未保存凭证')
            nav, _ = await self._http('GET', 'https://api.bilibili.com/x/web-interface/nav', cookies=cookies)
            nav = self._data(nav)
            if not nav.get('isLogin') or str(nav.get('mid')) != cookies['DedeUserID']:
                raise BiliUnavailable('登录账号核验失败，未保存凭证')
            self.store.save(cookies)
            self.cookies, self.name, self.invalid = cookies, str(nav.get('uname', ''))[:80], False
            self.generation += 1
            self.qr = None
            return {'state': 'ok', **await self.status()}

    async def logout(self):
        async with self.lock:
            self.store.clear()
            self.cookies, self.name, self.qr = {}, '', None
            self.generation += 1
            self.invalid = False


def sign_dm(nav, sender, receiver):
    """Normal WBI request signing; no retries or fingerprint/risk-control bypass.

    Adapted from bilibili-api-python 17.4.1 utils/network.py (GPL-3.0).
    See BILIBILI_API_LICENSE and NOTICE.md.
    """
    order = (46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13)
    try:
        pair = nav['wbi_img']
        raw = ''.join(urlsplit(pair[k]).path.rsplit('/', 1)[-1].split('.')[0] for k in ('img_url', 'sub_url'))
        key = ''.join(raw[i] for i in order)
    except (KeyError, IndexError, TypeError):
        raise BiliUnavailable('B站未提供私信所需签名参数，本轮不发送')
    params = {'w_sender_uid': sender, 'w_receiver_id': receiver, 'wts': int(time.time()), 'web_location': 1550101}
    params['w_rid'] = hashlib.md5((urlencode(sorted(params.items())) + key).encode()).hexdigest()
    return params
