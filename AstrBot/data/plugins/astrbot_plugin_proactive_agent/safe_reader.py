"""Bounded allowlist reader with DNS pinning and redirect revalidation."""
import asyncio,ipaddress,socket
from html.parser import HTMLParser
from urllib.parse import urlsplit,urljoin
import aiohttp

class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self,host,port=0,family=socket.AF_INET):
        rows=await asyncio.get_running_loop().getaddrinfo(host,port,type=socket.SOCK_STREAM)
        output=[]
        for fam,_,proto,_,address in rows:
            ip=ipaddress.ip_address(address[0]);ip=ip.ipv4_mapped if getattr(ip,'ipv4_mapped',None) else ip
            if not ip.is_global:raise ValueError('禁止访问本机、内网或元数据地址')
            output.append(dict(hostname=host,host=address[0],port=port,family=fam,proto=proto,flags=socket.AI_NUMERICHOST))
        if not output:raise ValueError('域名没有可用的公网地址')
        return output
    async def close(self):pass

def validate_url(url,allowed):
    parsed=urlsplit(str(url))
    host=(parsed.hostname or '').lower().rstrip('.')
    allowed={x.strip().lower().rstrip('.') for x in allowed if x.strip()}
    if parsed.scheme not in {'https','http'} or parsed.username or parsed.password or not host or host not in allowed or parsed.port not in {None,80,443}:
        raise ValueError('网址不在明确允许的域名范围内')
    try:
        ip=ipaddress.ip_address(host)
    except ValueError:pass
    else:
        if not ip.is_global:raise ValueError('禁止访问非公网地址')
    return parsed._replace(fragment='').geturl()

class TextReader(HTMLParser):
    def __init__(self):super().__init__();self.skip=0;self.parts=[];self.size=0;self.title=[];self.in_title=False
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style','noscript','svg','template'}:self.skip+=1
        if tag=='title':self.in_title=True
    def handle_endtag(self,tag):
        if tag in {'script','style','noscript','svg','template'}:self.skip=max(0,self.skip-1)
        if tag=='title':self.in_title=False
    def handle_data(self,data):
        if self.skip or not data.strip():return
        if self.in_title:self.title.append(data.strip())
        if self.size<16000:self.parts.append(data.strip());self.size+=len(data)


def main_article(text):
    """Prefer a publisher's article/main container over navigation-heavy page text."""
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(text,'html.parser')
    blocks=soup.select('article,main,[role="main"]')
    if not blocks:return None
    block=max(blocks,key=lambda node:len(node.get_text(' ',strip=True)))
    for tag in block.select('script,style,noscript,nav,aside,header,footer,form,button,svg'):
        tag.decompose()
    body=block.get_text('\n',strip=True)
    return body if len(body)>=60 else None

async def read_web(url,allowed,reserve,*,session_factory=aiohttp.ClientSession,profile_root=None,checkpoint=None):
    url=validate_url(url,allowed)
    from .article_body import site_for
    site=site_for(url)
    if site=='zhihu' and profile_root is not None:
        from .browser_bridge import configured,read_owner_browser
        if configured(profile_root):return await read_owner_browser(url,profile_root,reserve)
    try:
        return await _read_http(url,allowed,reserve,session_factory=session_factory,checkpoint=checkpoint)
    except (asyncio.CancelledError,):raise
    except (ValueError,aiohttp.ClientError,TimeoutError) as exc:
        if not site or profile_root is None:raise
        if isinstance(exc,ValueError) and not any(word in str(exc) for word in ('正文','空壳','登录','验证')):raise
        from .browser_reader import read_browser
        return await read_browser(url,profile_root,reserve)


async def _read_http(url,allowed,reserve,*,session_factory=aiohttp.ClientSession,checkpoint=None):
    """reserve is persisted for EVERY HTTP request, including redirects."""
    url=validate_url(url,allowed)
    raw=await checkpoint.load() if checkpoint else None
    if raw:return await checkpoint.parse(raw,parse_raw)
    async with asyncio.timeout(10):
        connector=aiohttp.TCPConnector(resolver=PublicResolver(),use_dns_cache=False)
        async with session_factory(connector=connector,trust_env=False,timeout=aiohttp.ClientTimeout(total=10)) as session:
            for attempt in range(4):
                await reserve()
                async with session.get(url,allow_redirects=False,headers={'Accept':'text/html,text/plain','User-Agent':'DaFeiLong-Reader/1.0'}) as response:
                    if response.status in {301,302,303,307,308}:
                        if attempt==3:raise ValueError('重定向次数超过限制')
                        url=validate_url(urljoin(url,response.headers.get('Location','')),allowed);continue
                    response.raise_for_status()
                    if response.content_length and response.content_length>2*1024*1024:raise ValueError('网页超过2MB限制')
                    ctype=response.headers.get('Content-Type','').split(';',1)[0].lower()
                    if ctype not in {'text/html','text/plain','application/xhtml+xml'}:raise ValueError('只读取网页或纯文本正文')
                    data=bytearray()
                    async for chunk in response.content.iter_chunked(32768):
                        data.extend(chunk)
                        if len(data)>2*1024*1024:raise ValueError('网页超过2MB限制')
                    text=bytes(data).decode(response.charset or 'utf-8',errors='replace')
                    raw={'url':url,'text':text,'ctype':ctype,'bytes':len(data)}
                    if checkpoint:
                        await checkpoint.save(raw)
                        return await checkpoint.parse(raw,parse_raw)
                    return await asyncio.to_thread(parse_raw,raw)
    raise ValueError('网页读取未完成')


def parse_raw(raw):
    from .article_body import site_for,extract_article
    from .reading_links import generic_article
    url=raw['url'];text=raw['text']
    result=extract_article(text,url) if site_for(url) else generic_article(text,url,raw['ctype'])
    title=result['title'];content=result['content']
    gate=('安全验证','访问验证','人机验证','请完成验证','访问异常','验证码','登录后查看','登录后继续','Just a moment','Access Denied','Sign in to continue')
    if any(v.casefold() in title.casefold() for v in gate) or len(content)<800 and any(v.casefold() in content.casefold() for v in gate):raise ValueError('网站要求登录或验证，尚未读到正文')
    if len(content.strip())<60:raise ValueError('网页只有空壳或内容不足，未标记已读')
    return {**result,'url':url,'bytes':raw['bytes'],'reader':'direct_http','parser_version':2}
