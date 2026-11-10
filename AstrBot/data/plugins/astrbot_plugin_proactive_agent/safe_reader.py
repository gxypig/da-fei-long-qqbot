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

async def read_web(url,allowed,reserve,*,session_factory=aiohttp.ClientSession):
    """reserve is persisted for EVERY HTTP request, including redirects."""
    url=validate_url(url,allowed)
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
                    parser=TextReader();parser.feed(text)
                    content=('\n'.join(parser.parts) if ctype!='text/plain' else text)[:16000]
                    if not content.strip():raise ValueError('网页没有可读正文')
                    title=' '.join(parser.title)[:180]
                    # A successful HTTP response may still be an anti-bot/login shell.
                    gate=('安全验证','访问验证','人机验证','请完成验证','访问异常','验证码','登录后查看','登录后继续','Just a moment','Access Denied','Sign in to continue')
                    if any(v.casefold() in title.casefold() for v in gate) or (len(content)<800 and any(v.casefold() in content.casefold() for v in gate)):
                        raise ValueError('网站要求登录或验证，尚未读到正文')
                    if len(content.strip())<60:raise ValueError('网页只有空壳或内容不足，未标记已读')
                    return {'url':url,'title':title,'content':content,'scope':'网页正文（截取）' if parser.size>16000 else '网页正文','bytes':len(data),'truncated':parser.size>16000}
    raise ValueError('网页读取未完成')
