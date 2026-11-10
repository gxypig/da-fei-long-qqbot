"""Only links located in selected article paragraphs become followable evidence."""
import re
from urllib.parse import urljoin,urlsplit
from .long_memory_store import digest

def extract_links(block,url):
    from .safe_reader import validate_url
    result=[];seen=set()
    for anchor in block.select('a[href]'):
        if any(p.name in {'nav','aside','header','footer','form'} or re.search(r'(^|[-_ ])(recommend|related|advert|share|menu|navigation)([-_ ]|$)',' '.join(p.get('class',[])),re.I) for p in [anchor,*anchor.parents] if getattr(p,'attrs',None)):
            continue
        href=urljoin(url,str(anchor.get('href','')))
        try:href=validate_url(href,[urlsplit(href).hostname or ''])
        except (ValueError,TypeError):continue
        label=anchor.get_text(' ',strip=True)
        paragraph=anchor.find_parent(['p','li','blockquote','section','div']) or anchor.parent
        text=paragraph.get_text(' ',strip=True)
        if href==url or href in seen or not label or len(text)<12:continue
        if re.search(r'/((log|sign)in|register|search)(/|\?|$)',href,re.I):continue
        seen.add(href);result.append({'id':'link:'+digest([url,href])[:20],'kind':'body','url':href,'title':label[:120],
            'paragraph':text[:500],'source_url':url})
        if len(result)>=30:break
    return result

def generic_article(text,url,ctype):
    from bs4 import BeautifulSoup
    from .safe_reader import TextReader
    if ctype=='text/plain':
        return dict(title='',content=text[:16000],scope='纯文本正文'+('（截取）' if len(text)>16000 else ''),
            body_verified=True,reading_level='body_fragment' if len(text)>16000 else 'full_body',truncated=len(text)>16000,links=[],body_images=[])
    soup=BeautifulSoup(text,'html.parser');title=soup.title.get_text(' ',strip=True)[:180] if soup.title else ''
    # Many actual articles use divs rather than HTML5 article/main tags.
    # Require a specific body marker plus substantial non-link prose; a generic
    # .content wrapper or a recommendation list is not evidence of an article.
    blocks=[]
    for specific,selector in ((True,'[itemprop="articleBody"],.article-body,.entry-content,.post-article,.main-article-content'),
                     (True,'.post-content,.article-content'),(False,'article,main,[role="main"]')):
        for candidate in soup.select(selector):
            size=len(candidate.get_text(' ',strip=True))
            linked=sum(len(a.get_text(' ',strip=True)) for a in candidate.select('a'))
            if not specific or size>=200 and linked/max(1,size)<.45:blocks.append(candidate)
        if blocks:break
    block=max(blocks,key=lambda b:len(b.get_text(' ',strip=True))) if blocks else None
    if block is not None:
        for tag in block.select('script,style,noscript,nav,aside,header,footer,form,button,svg,.related,.recommend,.advertisement'):tag.decompose()
        content=block.get_text('\n',strip=True);links=extract_links(block,url);images=[]
        for img in block.select('img')[:10]:
            src=urljoin(url,str(img.get('data-src') or img.get('src') or ''))
            if urlsplit(src).scheme in {'http','https'} and src not in images:images.append(src)
    else:
        parser=TextReader();parser.feed(text);content='\n'.join(parser.parts);links=[];images=[]
    verified=block is not None;truncated=len(content)>16000
    level=('body_fragment' if truncated else 'full_body') if verified else 'page_fragment'
    return dict(title=title,content=content[:16000],body_verified=verified,reading_level=level,truncated=truncated,
        scope=('网页正文' if verified else '网页可见文字片段（未确认正文容器）')+('（截取）' if truncated else ''),
        links=links,body_images=images[:3],image_count=len(images),stages=['fetched','parsed'])
