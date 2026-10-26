"""Read-only Bilibili discovery, anonymous or explicitly selected login.

Inspired by dsh-bili-scroll's candidate pool/comment scoring, reimplemented for
AstrBot. See NOTICE.md for upstream revisions and MIT attributions.
"""
import asyncio
import copy
import html
import json
import math
import re
import sys
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import aiohttp
from .work_limits import remaining

BVID = re.compile(r'BV[0-9A-Za-z]{10}\Z')
API = 'https://api.bilibili.com'
PATHS = {'/x/web-interface/popular', '/x/web-interface/search/type',
         '/x/web-interface/view', '/x/v2/reply', '/x/space/arc/search',
         '/x/web-interface/archive/related'}


def clean(value, limit=200):
    text = html.unescape(re.sub(r'<[^>]*>', '', str(value or '')))
    return re.sub(r'[\x00-\x1f\x7f]', ' ', text).strip()[:limit]


def video(row):
    bvid = str(row.get('bvid', ''))
    if not BVID.fullmatch(bvid):
        return None
    duration = row.get('duration') or row.get('length') or 0
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
            'duration': duration, 'published': int(row.get('pubdate') or row.get('created') or 0),
            'play': int((row.get('stat') or {}).get('view') or row.get('play') or 0),
            'replies': int((row.get('stat') or {}).get('reply') or row.get('review') or row.get('comment') or 0)}


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


_read_scope = ContextVar('qqbot_bili_read_scope', default=None)


def _caller_priority():
    # The existing timer loops mark their task with model_work('scheduled').
    # Read the loaded marker only: importing the runtime here would add a hard
    # dependency to this otherwise standalone, read-only client.
    runtime = sys.modules.get('data.plugins.astrbot_plugin_proactive_agent.work_scheduler')
    marker = getattr(runtime, '_kind', None)
    kind = marker.get() if marker is not None else None
    return int(kind in {'scheduled', 'background', 'auxiliary'})


@dataclass(eq=False)
class _SharedRead:
    owner: object
    identity: tuple
    waiters: dict = field(default_factory=dict)
    task: object = None

    def priority(self):
        # Sources can themselves be shared reads (anonymous visitor setup).
        # Re-evaluate at admission so joining/leaving a foreground waiter takes
        # effect without duplicating or interrupting an already running GET.
        return min((source() for source in self.waiters.values()), default=1)


class _ReadSlots:
    """Two HTTP requests, FIFO within priority, with cancellation-safe leases."""
    def __init__(self, limit=2):
        self.limit, self.active = limit, 0
        self.waiters = []
        self.sequence = 0

    def _dispatch(self):
        while self.active < self.limit and self.waiters:
            item = min(self.waiters, key=lambda row: (row['priority'](), row['sequence']))
            self.waiters.remove(item)
            if item['future'].done():
                continue
            item['granted'] = True
            self.active += 1
            item['future'].set_result(None)

    @asynccontextmanager
    async def hold(self, priority):
        future = asyncio.get_running_loop().create_future()
        self.sequence += 1
        item = {'future': future, 'priority': priority, 'sequence': self.sequence, 'granted': False}
        self.waiters.append(item)
        self._dispatch()
        try:
            await future
            yield
        finally:
            # Cancellation may arrive after grant but before await resumes.
            if item['granted']:
                self.active -= 1
            elif item in self.waiters:
                self.waiters.remove(item)
            if not future.done():
                future.cancel()
            self._dispatch()


class BiliClient:
    def __init__(self, account=None, use_account=lambda: False):
        self.account, self.use_account = account, use_account
        self.session = None
        self.cache = {}
        self.slots = _ReadSlots(2)
        self._flights = {}
        self._read_tasks = set()
        self._http_tasks = set()
        self._closed = False
        self.visitor_ready = False

    @property
    def anonymous(self):
        return not (self.account and self.use_account() and self.account.ready)

    def identity(self):
        return ('anonymous',) if self.anonymous else self.account.identity()

    def _read_identity(self):
        account = self.account
        using = bool(account and self.use_account())
        raw = account.identity() if account else None
        effective = raw if using and account.ready else ('anonymous',)
        # Keep the public identity API intact. Internal keys also distinguish a
        # logout/relogin while browsing anonymously and replacement sessions.
        return effective, raw, using, id(account)

    def _check_read(self, identity):
        if self._closed:
            raise BiliUnavailable('B站读取客户端已关闭')
        if identity != self._read_identity():
            raise BiliUnavailable('浏览账号或登录状态已变化，请重新查询')

    async def _shared_read(self, key, identity, operation, priority=None):
        self._check_read(identity)
        if priority is None:
            value = _caller_priority()
            priority = lambda: value
        flight = self._flights.get(key)
        if flight is None or flight.task.done():
            flight = _SharedRead(self, identity)
            self._flights[key] = flight

            async def run():
                token = _read_scope.set(flight)
                try:
                    self._check_read(identity)
                    return await operation()
                finally:
                    _read_scope.reset(token)

            flight.task = asyncio.create_task(run(), name='bili-shared-read')
            self._read_tasks.add(flight.task)

            def finished(task):
                self._read_tasks.discard(task)
                if self._flights.get(key) is flight:
                    self._flights.pop(key, None)
                if not task.cancelled():
                    task.exception()  # Retrieve failures even if all waiters left.

            flight.task.add_done_callback(finished)
        waiter = object()
        flight.waiters[waiter] = priority
        try:
            result = await asyncio.shield(flight.task)
            self._check_read(identity)
            return result
        finally:
            flight.waiters.pop(waiter, None)
            if not flight.waiters and not flight.task.done():
                # Detach before cancellation: a new caller must never join a
                # dying task, and its replacement must survive the old callback.
                if self._flights.get(key) is flight:
                    self._flights.pop(key, None)
                flight.task.cancel()
                await asyncio.gather(flight.task, return_exceptions=True)

    async def _cached_scan(self, key, operation):
        identity = key[1]
        self._check_read(identity)
        cached = self.cache.get(key)
        metadata=cached[3] if cached and len(cached)>3 else {}
        ttl=8 if metadata.get('read_state')=='failed' else 25 if metadata.get('read_state')=='partial' else 300
        if cached and time.time() - cached[0] <= ttl:
            return cached

        async def fill():
            result = await operation()
            self._check_read(identity)
            self.cache[key] = result
            while len(self.cache) > 32:
                self.cache.pop(next(iter(self.cache)))
            return result

        return await self._shared_read(('scan', key), identity, fill)

    async def close(self):
        self._closed = True
        tasks = (self._read_tasks | self._http_tasks) - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._flights.clear()
        if self.session:
            await self.session.close()

    async def get(self, path, params):
        if path not in PATHS:
            raise ValueError('Only approved public read endpoints are supported')
        scope = _read_scope.get()
        if scope is None or scope.owner is not self:
            value = _caller_priority()
            scope = _SharedRead(self, self._read_identity(), {None: lambda: value})
        self._check_read(scope.identity)
        task = asyncio.current_task()
        self._http_tasks.add(task)
        try:
            for attempt in range(2):
                try:
                    async with asyncio.timeout(remaining(12)):
                        return await self._get(path, params, scope)
                except BiliUnavailable as exc:
                    # Only transport failures on a read are retryable. Auth,
                    # rate limits and invalid responses retain their meaning.
                    transient=isinstance(exc.__cause__,(aiohttp.ClientConnectionError,TimeoutError))
                    if attempt or not transient or remaining(2)<1:raise
                    await asyncio.sleep(.15)
        finally:
            self._http_tasks.discard(task)

    async def _get(self, path, params, scope):
        if scope.identity[0] != ('anonymous',):
            async with self.slots.hold(scope.priority):
                self._check_read(scope.identity)
                result = await self.account.get(path, params)
                self._check_read(scope.identity)
                return result
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10),
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'},
                cookie_jar=aiohttp.CookieJar())
        # Bilibili's public search expects its anonymous visitor session. Obtain
        # only the cookies returned by its normal public search page; no account,
        # browser profile, copied cookies, generated tokens or persisted login.
        if path.endswith('/search/type') and not self.visitor_ready:
            async def visitor():
                if not self.visitor_ready:
                    current = _read_scope.get()
                    async with self.slots.hold(current.priority):
                        self._check_read(scope.identity)
                        try:
                            async with self.session.get('https://search.bilibili.com/all',
                                    params={'keyword': params.get('keyword', '')}, allow_redirects=False) as page:
                                if page.status != 200:
                                    raise BiliUnavailable(f'B站匿名搜索页面 HTTP {page.status}')
                                await page.content.read(65536)
                                self._check_read(scope.identity)
                                self.visitor_ready = True
                        except (aiohttp.ClientError, TimeoutError) as exc:
                            raise BiliUnavailable('B站匿名搜索初始化失败，稍后再试') from exc

            await self._shared_read(('visitor', scope.identity), scope.identity, visitor, scope.priority)
        async with self.slots.hold(scope.priority):
            self._check_read(scope.identity)
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
                self._check_read(scope.identity)
                if data.get('code') != 0:
                    raise BiliUnavailable(f'B站匿名接口暂不可用（code={data.get("code")}），不尝试登录或绕过限制')
                return data.get('data') or {}
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                raise BiliUnavailable('B站网络或响应异常，请稍后重试') from exc

    async def discover(self, keyword='', excluded=(), *, mode='normal', up_list='', mygo=True):
        if mode == 'chishi':
            from .scout import scout
            return await scout(self, keyword, excluded, up_list, mygo)
        if mode != 'normal':
            raise ValueError('未知选片模式')
        return await self.normal_discover(keyword, excluded)

    async def normal_discover(self, keyword='', excluded=()):
        keyword = clean(keyword, 40)
        key = ('normal', self._read_identity(), keyword)
        async def build():
            rows, warnings = [], []
            failures=0
            if keyword:
                try:
                    data = await self.get('/x/web-interface/search/type',
                        {'search_type': 'video', 'keyword': keyword, 'page': 1, 'order': 'totalrank'})
                    rows = data.get('result') or []
                except BiliUnavailable as exc:
                    failures+=1
                    warnings.append(str(exc) + '；仅在当前热门标题中匹配关键词')
            if not rows:
                data = await self.get('/x/web-interface/popular', {'pn': 1, 'ps': 30})
                rows = data.get('list') or []
                if keyword:
                    rows = [r for r in rows if keyword.casefold() in clean(r.get('title')).casefold()]
            items = [v for r in rows if isinstance(r, dict) and (v := video(r))]
            items = list({x['bvid']: x for x in items}.values())[:6]

            async def inspect(item):
                nonlocal failures
                comments = []
                if item['aid']:
                    try:
                        data = await asyncio.wait_for(self.get('/x/v2/reply', {'type': 1, 'oid': item['aid'], 'pn': 1, 'ps': 10, 'sort': 2}), 4)
                        comments = [clean((r.get('content') or {}).get('message'), 120) for r in (data.get('replies') or [])[:8]]
                    except (BiliUnavailable, TimeoutError):
                        failures+=1
                item['comments'] = comments
                item['read_scope'] = '标题、简介' + ('、公开评论' if comments else '（评论暂不可读）')
                item['score'] = score(item, comments)
                return item

            tasks = [asyncio.create_task(inspect(x)) for x in items]
            try:
                items = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            return (time.time(), sorted(items, key=lambda x: -x['score']), warnings,
                    {'read_state':('partial' if items else 'failed') if failures else ('ok' if items else 'empty'),
                     'failed_reads':failures})

        cached = await self._cached_scan(key, build)
        return {'items': copy.deepcopy([v for v in cached[1] if v['bvid'] not in excluded]),
                'warnings': list(cached[2]), 'anonymous': self.anonymous, 'mode': 'normal', 'stats':dict(cached[3])}

    async def detail(self, bvid):
        if not BVID.fullmatch(bvid):
            raise ValueError('无效的视频BV号')
        return video(await self.get('/x/web-interface/view', {'bvid': bvid}))
