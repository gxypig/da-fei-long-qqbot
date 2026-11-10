"""Extract publisher article containers, never search snippets or page chrome."""
import json
import re
from urllib.parse import urlsplit


def site_for(url):
    host = (urlsplit(url).hostname or '').lower()
    return 'tieba' if host == 'tieba.baidu.com' else 'zhihu' if host in {'www.zhihu.com', 'zhuanlan.zhihu.com'} else None


def article_key(url):
    p = urlsplit(url)
    patterns = {'tieba': r'^/p/(\d+)/?$', 'zhihu': r'^/(?:question/\d+/answer|p)/(\d+)/?$'}
    site = site_for(url)
    match = re.match(patterns.get(site, r'(?!)'), p.path)
    if match:
        return site, match[1]
    if site == 'zhihu':
        match = re.fullmatch(r'/question/(\d+)/?', p.path)
        if match:
            return site, 'question:' + match[1]
    return None


def extract_article(html, url):
    from bs4 import BeautifulSoup
    key = article_key(url)
    if not key:
        raise ValueError('不是可识别的贴吧帖子或知乎正文网址')
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    for tag in soup.select('script,style,noscript,template,svg'):
        tag.decompose()
    content = soup.get_text(' ', strip=True)
    if any(v in title for v in ('安全验证', '访问异常', 'Access Denied')):
        raise ValueError('网站要求验证，请在专用阅读窗口完成一次验证')
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical and article_key(canonical.get('href', '')) not in {None, key}:
        raise ValueError('网页跳到了其他文章，未读取请求的正文')
    blocks = []
    body_images=[]
    comments_partial=False
    if key[0] == 'tieba':
        modern=soup.select_one('.pc-pb-box .image-text')
        main=modern.select_one(':scope > .pb-content-wrap') if modern else None
        if main is not None:
            ids={t.get('tid') for t in main.select('[tid]')}
            if ids and ids!={key[1]}:
                raise ValueError('正文的帖子编号与请求不一致')
            blocks.append(('楼主正文',main))
            for item in modern.select('.pb-comment-item,.pb-lzl-item')[:30]:
                body=item.select_one(':scope > .comment-content > .pb-rich-text')
                author=item.select_one(':scope > .head-line .head-name')
                if body:
                    blocks.append(('页面评论（'+(author.get_text(' ',strip=True) if author else '匿名')+'的观点）',body))
            comments_partial=True
        for item in soup.select('.l_post')[:30]:
            body = item.select_one('.d_post_content')
            if not body:
                continue
            try:
                data = json.loads(item.get('data-field', '{}'))
            except (ValueError, TypeError):
                data = {}
            blocks.append((str(data.get('content', {}).get('post_no', len(blocks)+1))+'楼', body))
        if not blocks:
            blocks = [(str(i+1)+'楼', b) for i, b in enumerate(soup.select('.d_post_content')[:30])]
    elif urlsplit(url).hostname == 'zhuanlan.zhihu.com':
        body = soup.select_one('.Post-RichText')
        if body:
            blocks.append(('文章正文', body))
    else:
        for item in soup.select('.AnswerItem')[:20]:
            try:
                data = json.loads(item.get('data-zop', '{}'))
            except (ValueError, TypeError):
                data = {}
            ident = str(item.get('data-aid') or data.get('itemId') or '')
            if not key[1].startswith('question:') and ident != key[1]:
                continue
            body = item.select_one('.RichContent-inner .RichText, .RichContent-inner')
            if body:
                blocks.append(('回答 '+ident, body))
            if len(blocks) >= (3 if key[1].startswith('question:') else 1):
                break
        # Some article layouts omit AnswerItem metadata; only one answer is safe.
        if not blocks and not key[1].startswith('question:') and not soup.select('.AnswerItem'):
            bodies = soup.select('.RichContent-inner')
            if len(bodies) == 1:
                blocks.append(('当前回答', bodies[0]))
    if not blocks:
        if any(v in content for v in ('请求存在异常', '请完成下方验证', '请向右滑动', '登录后查看', '登录后继续', '验证失败')):
            raise ValueError('网站要求登录或验证，尚未取得原帖正文')
        if soup.select('.network-error,.error-status'):
            raise ValueError('正文接口未返回数据，页面只有网络错误和推荐区域')
        raise ValueError('页面没有目标正文容器，未将导航、推荐或摘要当作正文')
    from .reading_links import extract_links
    links=[]
    texts, images = [], 0
    for label, block in blocks:
        links.extend(extract_links(block,url))
        for image in block.select('img'):
            images += 1
            alt = str(image.get('alt') or '').strip()
            src=str(image.get('data-src') or image.get('data-original') or image.get('src') or '')
            host=(urlsplit(src).hostname or '').lower()
            if len(body_images)<3 and (not label.startswith('页面评论')) and urlsplit(src).scheme=='https' and (host in {'tiebapic.baidu.com','imgsa.baidu.com'} or host.endswith('.zhimg.com')):
                body_images.append(src)
            image.replace_with('[正文图片'+('，原图说明：'+alt[:160] if alt else '，内容未识别')+']')
        text = block.get_text('\n', strip=True)
        if text:
            texts.append(label+'：\n'+text)
    body = '\n\n'.join(texts).strip()
    if not body:
        raise ValueError('原帖正文为空或仅含未加载的附件')
    truncated = len(body) > 16000
    scope = '贴吧当前页楼层正文' if key[0] == 'tieba' else '知乎当前回答正文' if not key[1].startswith('question:') else '知乎当前页回答正文（最多3条）'
    if urlsplit(url).hostname == 'zhuanlan.zhihu.com':
        scope = '知乎文章正文'
    if truncated:
        scope += '（截取）'
    if images:
        scope += '；内嵌图片未识别'
    if comments_partial:
        scope += '；仅当前加载的公开评论，非全部评论'
    # Folding in a recommended answer says nothing about the requested answer.
    # CSS folding also does not prove that the DOM itself is an excerpt.
    folded = any(block.select_one('.ContentItem-expandButton') or
                 block.find_parent(class_='is-collapsed') for _, block in blocks)
    if folded:
        scope += '；目标正文处于折叠容器，仅依据已加载文字'
    return dict(url=url, title=title[:180], content=body[:16000], scope=scope,
                truncated=truncated, image_count=images, body_verified=True,
                links=list({r['id']:r for r in links}.values())[:30], reading_level='body_fragment' if truncated or comments_partial or folded else 'full_body',
                body_images=body_images, comments_partial=comments_partial, folded=bool(folded),
                stages=['discovered', 'article_body_read'])
