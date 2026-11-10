"""Isolated, ordinary browser reader. No personal browser profile or stealth patches.

Run in the dedicated web-reader environment. Login/verification is performed by
the owner in --authorize mode; unattended mode never solves a challenge.
"""
import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.request import getproxies

from article_body import article_key, extract_article, site_for

FAMILIES = {'tieba': ('baidu.com', 'bdstatic.com', 'bdimg.com', 'bcebos.com'),
            'zhihu': ('zhihu.com', 'zhimg.com')}


def allowed_host(host, site):
    host = host.lower().rstrip('.')
    return any(host == base or host.endswith('.'+base) for base in FAMILIES[site])


def readonly_post(url, site):
    """Tieba's current public page fetches its thread body using POST, not GET."""
    parsed=urlsplit(url)
    return site=='tieba' and parsed.hostname=='tieba.baidu.com' and parsed.path=='/c/f/pb/page_pc'


def public_addresses(host):
    addresses = list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('阅读器拒绝连接内网地址')
    return addresses


def configured_proxy():
    value=getproxies().get('https') or getproxies().get('http')
    if not value:return None
    parsed=urlsplit(value if '://' in value else 'http://'+value)
    if parsed.scheme!='http' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('当前系统代理类型需要额外适配；未忽略系统网络设置或导出凭据')
    return parsed.hostname,parsed.port or 80


def connect_pinned(ip,upstream=None):
    if not upstream:return socket.create_connection((ip,443),timeout=6)
    remote=socket.create_connection(upstream,timeout=6)
    try:
        authority=('['+ip+']' if ':' in ip else ip)+':443'
        remote.sendall(('CONNECT '+authority+' HTTP/1.1\r\nHost: '+authority+'\r\n\r\n').encode('ascii'))
        data=b''
        while b'\r\n\r\n' not in data:
            chunk=remote.recv(4096)
            if not chunk or len(data)>16000:raise OSError('系统代理未建立隧道')
            data+=chunk
        if data.split(b' ',2)[1]!=b'200':raise OSError('系统代理拒绝连接目标')
        return remote
    except BaseException:
        remote.close();raise


class SafeTunnel(socketserver.ThreadingTCPServer):
    """Only CONNECT to approved public site families; DNS is pinned per tunnel.

    Chromium retains TLS/certificate verification. This is not an HTTPS MITM.
    """
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, site):
        self.site = site
        self.connections = 0
        self.bytes = 0
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.upstream = configured_proxy()
        super().__init__(('127.0.0.1', 0), TunnelHandler)


class TunnelHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(8)
        try:
            data = b''
            while b'\r\n\r\n' not in data:
                part = client.recv(4096)
                if not part or len(data) > 12000:
                    return
                data += part
            method, authority, _ = data.split(b'\r\n', 1)[0].decode('ascii').split(' ', 2)
            host, port = authority.rsplit(':', 1)
            if method != 'CONNECT' or port != '443' or not allowed_host(host, self.server.site):
                client.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
                return
            with self.server.lock:
                self.server.connections += 1
                if self.server.connections > 200 or self.server.bytes > 24*1024*1024:
                    return
            addresses = public_addresses(host)
            remote = None
            for ip in addresses[:3]:
                try:
                    remote = connect_pinned(ip,self.server.upstream)
                    break
                except OSError:
                    pass
            if remote is None:
                return
            with remote:
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                while not self.server.stopping.is_set():
                    ready, _, _ = select.select([client, remote], [], [], .5)
                    for src in ready:
                        chunk = src.recv(65536)
                        if not chunk:
                            return
                        with self.server.lock:
                            self.server.bytes += len(chunk)
                            if self.server.bytes > 24*1024*1024:
                                return
                        (remote if src is client else client).sendall(chunk)
        except (OSError, ValueError, UnicodeError):
            return


@contextlib.contextmanager
def profile_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder/'reader.lock').open('a+b')
    try:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            if (folder/'reader.lock').stat().st_size == 0:
                handle.write(b'0'); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except OSError as exc:
        raise RuntimeError('专用阅读窗口正被使用，请完成登录后关闭窗口') from exc
    finally:
        handle.close()


def write_status(folder, **values):
    path = folder/'status.json'
    old = {}
    try:
        old = json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        pass
    temp = folder/('status-'+str(os.getpid())+'.tmp')
    temp.write_text(json.dumps({**old, **values, 'at': time.time()}, ensure_ascii=False), 'utf-8')
    os.replace(temp, path)


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def page_diagnostic(folder, html, url, error, requests=()):
    """Local structure only: strip scripts, forms and controls before inspection."""
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(html,'html.parser')
    for item in soup.select('script,style,input,form,textarea,noscript,iframe,header,nav'):
        item.decompose()
    (folder/'last-page.html').write_text(str(soup)[:2000000],'utf-8')
    write_status(folder,state='waiting_for_body',error=error,page_url=url,
                 title=soup.title.get_text()[:180] if soup.title else '',requests=list(requests)[-60:])


async def run(payload, authorize=False):
    from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout, Error as BrowserError
    url = str(payload['url'])
    site = site_for(url)
    if urlsplit(url).scheme != 'https' or not article_key(url) or urlsplit(url).username or urlsplit(url).port not in {None,443}:
        raise ValueError('专用浏览器仅接受 HTTPS 的贴吧帖子或知乎正文')
    folder = Path(payload['profile_root']).resolve()/site
    with profile_lock(folder):
        proxy = SafeTunnel(site)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            async with async_playwright() as p:
                kwargs = dict(headless=not authorize, locale='zh-CN', accept_downloads=False,
                              service_workers='block', chromium_sandbox=True,
                              proxy={'server':'http://127.0.0.1:'+str(proxy.server_address[1])},
                              args=['--proxy-bypass-list=<-loopback>', '--disable-quic', '--disable-background-networking', '--disable-sync'])
                if os.name == 'nt':
                    kwargs['channel'] = 'msedge'
                browser = await p.chromium.launch_persistent_context(str(folder/'profile'), **kwargs)
                try:
                    page = browser.pages[0] if browser.pages else await browser.new_page()
                    main_requests = 0
                    body_requests = 0
                    requests=[]
                    async def route_request(route):
                        nonlocal main_requests,body_requests
                        req = route.request
                        parsed = urlsplit(req.url)
                        if req.resource_type in {'document','xhr','fetch'}:
                            requests.append({'method':req.method,'host':parsed.hostname,'path':parsed.path[:180],'type':req.resource_type})
                        if parsed.scheme != 'https' or not allowed_host(parsed.hostname or '', site):
                            return await route.abort()
                        if not authorize and ((req.method not in {'GET','HEAD','OPTIONS'} and not (req.method=='POST' and readonly_post(req.url,site))) or req.resource_type in {'media','font','image'}):
                            return await route.abort()
                        if req.is_navigation_request() and req.frame == page.main_frame:
                            main_requests += 1
                            if not authorize:
                                if main_requests > 4 or article_key(req.url) != article_key(url):
                                    return await route.abort()
                                emit({'reserve': True})
                                answer = await asyncio.to_thread(sys.stdin.readline)
                                if answer.strip() != 'ok':
                                    return await route.abort()
                        elif not authorize and req.method=='POST' and readonly_post(req.url,site):
                            body_requests+=1
                            if body_requests>4:return await route.abort()
                            emit({'reserve':True})
                            if (await asyncio.to_thread(sys.stdin.readline)).strip()!='ok':return await route.abort()
                        await route.continue_()
                    await browser.route('**/*', route_request)
                    await browser.route_web_socket('**/*', lambda ws: ws.close())
                    page.on('dialog', lambda dialog: asyncio.create_task(dialog.dismiss()))
                    write_status(folder, state='authorizing' if authorize else 'reading', error='')
                    initial_url='https://www.zhihu.com/signin' if authorize and site=='zhihu' else url
                    try:
                        await page.goto(initial_url, wait_until='domcontentloaded', timeout=20000)
                    except BrowserTimeout:
                        pass
                    deadline = time.monotonic()+(900 if authorize else 12)
                    error = '页面未加载正文'
                    last_diagnostic=0
                    while time.monotonic() < deadline and not page.is_closed():
                        try:
                            if article_key(page.url) != article_key(url):
                                if authorize and site=='zhihu' and urlsplit(page.url).hostname=='www.zhihu.com' and await page.locator('.AppHeader-profileEntry, .AppHeader-userInfo .Avatar').count():
                                    await page.goto(url,wait_until='domcontentloaded',timeout=20000)
                                    continue
                                raise ValueError('页面正在登录或已离开请求的文章')
                            html = await page.content()
                            if len(html.encode('utf-8')) > 2*1024*1024:
                                raise ValueError('网页超过2MB限制')
                            result = extract_article(html, url)
                            result['reader'] = 'dedicated_browser'
                            write_status(folder, state='ready', error='', last_success=time.time(),title=result['title'],page_url=url)
                            return result
                        except BrowserError:
                            error = '网页仍在跳转，尚未取得正文'
                        except ValueError as exc:
                            error = str(exc)
                            if article_key(page.url)==article_key(url) and time.monotonic()-last_diagnostic>5:
                                page_diagnostic(folder,html,page.url,error,requests)
                                last_diagnostic=time.monotonic()
                        await asyncio.sleep(.5)
                    needs_auth=any(x in error for x in ('要求登录','要求验证','正在登录'))
                    write_status(folder, state='needs_authorization' if needs_auth else 'body_unavailable', error=error)
                    raise ValueError(error+('；请使用“网页阅读登录”入口完成一次站点验证，阅读器随后自动复用此状态' if needs_auth else '；未将该页记为已读'))
                finally:
                    await browser.close()
        finally:
            proxy.stopping.set()
            await asyncio.to_thread(proxy.shutdown)
            proxy.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--authorize', action='store_true')
    args = parser.parse_args()
    try:
        payload = json.loads(sys.stdin.readline())
        emit({'result': asyncio.run(run(payload, args.authorize))})
    except Exception as exc:
        emit({'error': str(exc)[:600], 'error_type': type(exc).__name__})
        sys.exit(1)
