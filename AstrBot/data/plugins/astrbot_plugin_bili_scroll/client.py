"""Anonymous, read-only Bilibili discovery; no account cookies or write API.

Inspired by dsh-bili-scroll's candidate pool/comment scoring, reimplemented for
AstrBot. See NOTICE.md for upstream revisions and MIT attributions.
"""
import asyncio
import copy
import html
import json
import math
import re
import time

import aiohttp

BVID = re.compile(r'BV[0-9A-Za-z]{10}\Z')
API = 'https://api.bilibili.com'
PATHS = {'/x/web-interface/popular', '/x/web-interface/search/type',
         '/x/web-interface/view', '/x/v2/reply'}


def clean(value, limit=200):
    text = html.unescape(re.sub(r'<[^>]*>', '', str(value or '')))
    return re.sub(r'[\x00-\x1f\x7f]', ' ', text).strip()[:limit]


def video(row):
    bvid = str(row.get('bvid', ''))
    if not BVID.fullmatch(bvid):
        return None
    duration = row.get('duration', 0)
    if isinstance(duration, str) and ':' in duration:
        parts = duration.split(':')
        duration = sum(int(x) * 60 ** i for i, x in enumerate(reversed(parts)) if x.isdigit())
    try:
        duration = int(duration)
    except (ValueError, TypeError):
        duration = 0
    return {'bvid': bvid, 'aid': int(row.get('aid') or row.get('id') or 0),
            'url': f'https://www.bilibili.com/video/{bvid}', 'title': clean(row.get('title')),
            'author': clean((row.get('owner') or {}).get('name') or row.get('author'), 80),
            'description': clean(row.get('desc') or row.get('description'), 500),
            'duration': duration, 'published': int(row.get('pubdate') or 0),
            'replies': int((row.get('stat') or {}).get('reply') or row.get('review') or 0)}


def score(item, comments, now=None):
    """Public comment reactions and short/fresh videos, never invented views."""
    now = time.time() if now is None else now
    count = max(1, len(comments))
    ats = sum('@' in x for x in comments) / count
    abstract = sum(any(word in x for word in ('绷不住', '抽象', '离谱', '什么东西', '赤石')) for x in comments) / count
    duration_bonus = 12 if 0 < item['duration'] <= 180 else 3
    fresh = 5 if 0 < now - item['published'] < 3 * 86400 else 0
    return round(35 * ats + 10 * abstract + duration_bonus + fresh + min(5, math.log10(item['replies'] + 1)), 2)


class BiliUnavailable(RuntimeError):
    pass


class BiliClient:
    def __init__(self):
        self.session = None
        self.cache = {}
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(2)
        self.visitor_ready = False

    async def close(self):
        if self.session:
            await self.session.close()

    async def get(self, path, params):
        if path not in PATHS:
            raise ValueError('Only approved public read endpoints are supported')
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'},
                cookie_jar=aiohttp.CookieJar())
        # Bilibili's public search expects its anonymous visitor session. Obtain
        # only the cookies returned by its normal public search page; no account,
        # browser profile, copied cookies, generated tokens or persisted login.
        if path.endswith('/search/type') and not self.visitor_ready:
            try:
                async with self.session.get('https://search.bilibili.com/all',
                        params={'keyword': params.get('keyword', '')}, allow_redirects=False) as page:
                    if page.status != 200:
                        raise BiliUnavailable(f'B站匿名搜索页面 HTTP {page.status}')
                    await page.content.read(65536)
                    self.visitor_ready = True
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise BiliUnavailable('B站匿名搜索初始化失败，稍后再试') from exc
        async with self.slots:
            try:
                async with self.session.get(API + path, params=params, allow_redirects=False) as response:
                    if response.status != 200:
                        raise BiliUnavailable(f'B站匿名访问返回 HTTP {response.status}，稍后再试')
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > 2_000_000:
                            raise BiliUnavailable('B站返回内容过大')
                    data = json.loads(raw)
                if data.get('code') != 0:
                    raise BiliUnavailable(f'B站匿名接口暂不可用（code={data.get("code")}），不尝试登录或绕过限制')
                return data.get('data') or {}
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                raise BiliUnavailable('B站网络或响应异常，请稍后重试') from exc

    async def discover(self, keyword='', excluded=()):
        keyword = clean(keyword, 40)
        async with self.lock:
            cached = self.cache.get(keyword)
            if not cached or time.time() - cached[0] > 300:
                rows, warnings = [], []
                if keyword:
                    try:
                        data = await self.get('/x/web-interface/search/type',
                            {'search_type': 'video', 'keyword': keyword, 'page': 1, 'order': 'totalrank'})
                        rows = data.get('result') or []
                    except BiliUnavailable as exc:
                        warnings.append(str(exc) + '；仅在当前热门标题中匹配关键词')
                if not rows:
                    data = await self.get('/x/web-interface/popular', {'pn': 1, 'ps': 30})
                    rows = data.get('list') or []
                    if keyword:
                        rows = [r for r in rows if keyword.casefold() in clean(r.get('title')).casefold()]
                items = [v for r in rows if isinstance(r, dict) and (v := video(r))]
                items = list({x['bvid']: x for x in items}.values())[:6]

                async def inspect(item):
                    comments = []
                    if item['aid']:
                        try:
                            data = await asyncio.wait_for(self.get('/x/v2/reply', {'type': 1, 'oid': item['aid'], 'pn': 1, 'ps': 10, 'sort': 2}), 4)
                            comments = [clean((r.get('content') or {}).get('message'), 120) for r in (data.get('replies') or [])[:8]]
                        except (BiliUnavailable, TimeoutError):
                            pass
                    item['comments'] = comments
                    item['read_scope'] = '标题、简介' + ('、公开评论' if comments else '（评论暂不可读）')
                    item['score'] = score(item, comments)
                    return item

                items = await asyncio.gather(*(inspect(x) for x in items))
                self.cache[keyword] = (time.time(), sorted(items, key=lambda x: -x['score']), warnings)
                # Do not let arbitrary user searches grow memory without a bound.
                while len(self.cache) > 32:
                    self.cache.pop(next(iter(self.cache)))
                cached = self.cache[keyword]
            return {'items': copy.deepcopy([v for v in cached[1] if v['bvid'] not in excluded]),
                    'warnings': list(cached[2]), 'anonymous': True}

    async def detail(self, bvid):
        if not BVID.fullmatch(bvid):
            raise ValueError('无效的视频BV号')
        return video(await self.get('/x/web-interface/view', {'bvid': bvid}))
