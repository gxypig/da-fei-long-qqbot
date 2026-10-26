"""AstrBot adaptation of holoiallo/dsh-bili-scroll 0.2.0's chishi scout.

Scoring weights and discovery lanes derive from the MIT upstream; see NOTICE.md.
Only metadata and public comments are evidence, never an assertion of video viewing.
"""
import asyncio
import copy
import math
import re
import time

from .client import BiliUnavailable, clean, video

CHISHI_WORDS = ('赤石视频', '赤石', '大份视频', '散装大粪', '互联网散装大粪', '抽象视频')
MYGO_WORDS = ('MyGO 整活', 'MyGO 二创', '高松灯 梗', '春日影 梗', '买狗 TV', 'mygo 抽象')
DEFAULT_UPS = '互联网粪王,你爱咋想咋想吧,地球online神人集合,坤坤是条闲鱼,抽象搬运工258,番茄青蛙-,funnybite,沈井彬-擒史皇'
FEED_WORDS = ('吃', '尝', '呕', '吐', '赤石', '屎', '粪', '史', '投喂', '请你', '留着', '下饭', '喂')
ABSTRACT_WORDS = ('抽象', '整活', '绷', '逆天', '离谱', '发癫', '笑死', '地狱', '神人', '癫', '人类', '电子')
FOOD_WORDS = ('吃播', '大胃王', '探店', '教程', '食谱', '餐厅', '自助', '炸鸡', '牛排', '汉堡', '便当', '试吃')
SERIES = re.compile(r'(第\s*[0-9一二三四五六七八九十百]+\s*[期集话]|合集|TV|[①-⑳])', re.I)
MYGO = re.compile(r'(MyGO|迷子|高松灯|春日影|祥子|爱音|爽世|立希|乐奈|买狗|邦邦|BanG)', re.I)
PURE_AT = re.compile(r'^(@[^\s@]+\s*)+$')
MAX_SCAN = 48  # Within upstream's 6..60 range; sparse anonymous comments need a wider pool.


def split_words(text):
    return list(dict.fromkeys(x.strip() for x in re.split(r'[,，、\n]+', text) if x.strip()))


def duration_bonus(seconds):
    if 120 <= seconds <= 900:
        return 1.0
    if 45 <= seconds < 120:
        return .65
    if 900 < seconds <= 1800:
        return .8
    return .15 if seconds < 45 else .3


def judge(item, replies, now=None):
    """Original weights + the upstream prompt's mandatory selection thresholds."""
    now = time.time() if now is None else now
    bodies, ats, pure, feeds, abstract, evidence = [], 0, 0, 0, 0, []
    # Only top-level hot comments. Do not count pinned/hot duplicates or nested replies twice.
    seen = set()
    for row in replies:
        content = row.get('content') or {}
        body = clean(content.get('message'), 1000)
        key = str(row.get('rpid') or body)
        if not body or key in seen:
            continue
        seen.add(key)
        bodies.append(body)
        at = '@' in body or bool(content.get('members'))
        feed = any(w in body for w in FEED_WORDS)
        ats += int(at)
        pure += bool(PURE_AT.fullmatch(body))
        feeds += int(feed)
        abstract += any(w in body for w in ABSTRACT_WORDS)
        if (at or feed) and len(evidence) < 3:
            evidence.append(body[:120])
    n = len(bodies)
    denominator = max(1, n)
    title, seconds = item['title'], item['duration']
    series, mygo = bool(SERIES.search(title)), bool(MYGO.search(title))
    fromup = any(s.startswith('up:') for s in item.get('sources', []))
    fresh = 0 <= now - item['published'] < 3 * 86400 if item['published'] else False
    food = sum(w in title for w in FOOD_WORDS)
    score = (35 * ats / denominator + 15 * pure / denominator + 30 * duration_bonus(seconds)
             + 10 * feeds / denominator + 5 * abstract / denominator
             + 5 * min(1, math.log10(max(0, item['replies']) + 1) / 3.2) - 15 * min(food, 2)
             + 12 * fromup + 8 * series + 6 * mygo + 5 * fresh)
    reasons = []
    if not n:
        reasons.append('热评不可读或为空，不能判断')
    elif ats / n < .3:
        reasons.append('热评@率不足30%')
    if seconds < 45:
        reasons.append('时长不足45秒')
    if not feeds:
        reasons.append('未发现投喂话术')
    return {'score': round(score, 1), 'at_ratio': round(ats / denominator, 3),
            'pure_at_ratio': round(pure / denominator, 3), 'feed_ratio': round(feeds / denominator, 3),
            'abs_ratio': round(abstract / denominator, 3), 'sample_count': n,
            'evidence': evidence, 'comments': bodies[:12], 'eligible': not reasons,
            'rejected_reasons': reasons, 'series': series, 'mygo': mygo, 'fromup': fromup,
            'food_penalty': 15 * min(food, 2), 'selection_mode': 'chishi',
            'read_scope': '标题、简介、公开热评' if n else '标题、简介（评论暂不可读）'}


async def bounded_batch(coroutines, seconds):
    from .work_limits import remaining
    tasks = [asyncio.create_task(c) for c in coroutines]
    if not tasks:
        return False
    try:
        _, pending = await asyncio.wait(tasks, timeout=remaining(seconds,reserve=18))
        return bool(pending)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def scout(client, keyword='', excluded=(), up_list='', mygo=True):
    keyword = clean(keyword, 40)
    ups = split_words(up_list)[:12]
    key = ('chishi', client._read_identity(), keyword, tuple(ups), bool(mygo))
    async def build():
        pool, inspected, warnings = {}, [], []
        failures = 0
        timed_out = False
        started = time.monotonic()

        def add(row, source):
            if not isinstance(row, dict):
                return
            try:
                item = video(row)
            except (ValueError, TypeError):
                return
            if not item:
                return
            old = pool.get(item['bvid'])
            if old:
                if source not in old['sources']:
                    old['sources'].append(source)
                return
            if len(pool) < 800:
                item['sources'] = [source]
                pool[item['bvid']] = item

        async def search(word, order, page, source, recent=False):
            nonlocal failures
            try:
                data = await client.get('/x/web-interface/search/type',
                    {'search_type': 'video', 'keyword': word, 'order': order, 'page': page})
                for row in (data.get('result') or [])[:20]:
                    if recent and not time.time() - 7 * 86400 <= int(row.get('pubdate') or 0) <= time.time():
                        continue
                    add(row, source)
            except (BiliUnavailable, ValueError, TypeError):
                failures += 1

        async def up_feed(name):
            nonlocal failures
            try:
                uid = name
                if not name.isdigit():
                    data = await client.get('/x/web-interface/search/type',
                        {'search_type': 'bili_user', 'keyword': name, 'page': 1})
                    match = next((r for r in data.get('result', []) if r.get('uname') == name), None)
                    if not match:
                        raise BiliUnavailable('未找到精确昵称')
                    uid = str(match['mid'])
                for page in (1, 2):
                    data = await client.get('/x/space/arc/search',
                        {'mid': uid, 'pn': page, 'ps': 30, 'order': 'pubdate'})
                    rows = (data.get('list') or {}).get('vlist')
                    if not isinstance(rows, list):
                        raise BiliUnavailable('匿名接口未返回投稿列表')
                    for row in rows:
                        if time.time() - 7 * 86400 <= int(row.get('created') or 0) <= time.time():
                            add(row, 'up:' + name)
            except (BiliUnavailable, ValueError, TypeError, KeyError) as exc:
                failures+=1
                warnings.append(f'UP「{clean(name, 40)}」读取受限或无精确匹配；已跳过该来源')

        words = [keyword] if keyword else list(CHISHI_WORDS)
        tasks = [up_feed(name) for name in ups]
        # Interleave lanes so a limited anonymous request budget covers all sources.
        for page in (1, 2):
            for order in ('totalrank', 'pubdate'):
                tasks.extend(search(w, order, page, 'kw:' + w) for w in words)
                if page == 1 and order == 'totalrank':
                    tasks.extend(search(w, 'pubdate', 1, 'new7d:' + w, True)
                                 for w in ('赤石', '大份视频', '抽象视频'))
                    if mygo:
                        tasks.extend(search(w, 'totalrank', 1, 'mygo:' + w) for w in MYGO_WORDS)
        if await bounded_batch(tasks, 22):
            timed_out=True
            warnings.append('召回达到时间预算，仅使用已取得的候选')

        async def related(item):
            nonlocal failures
            try:
                rows = await client.get('/x/web-interface/archive/related', {'bvid': item['bvid']})
                for row in rows[:18]:
                    add(row, 'related:' + item['bvid'])
            except (BiliUnavailable, TypeError):
                failures += 1

        seeds = sorted(pool.values(), key=lambda x: -x.get('play', 0))[:3]
        if await bounded_batch([related(x) for x in seeds], 6):
            timed_out=True
            warnings.append('相关推荐读取超时，保留其他来源')

        alive = [x for x in pool.values() if x['replies'] >= 8]
        def prescore(item):
            title_hit = 2 * any(w in item['title'] for w in ('赤石', '大份', '大粪', '散装', '抽象', '猎奇'))
            up_hit = 1.5 * any(s.startswith('up:') for s in item['sources'])
            return title_hit + up_hit + min(1, math.log10(item['replies'] + 1) / 3.2)
        chosen = {x['bvid']: x for x in sorted(alive, key=prescore, reverse=True)[:12]}
        # Keep each original keyword lane represented; a broad high-volume
        # keyword otherwise consumes every slot before "大份/散装" is inspected.
        for word in words:
            lane = sorted((x for x in alive if 'kw:' + word in x['sources']), key=prescore, reverse=True)
            added = 0
            for item in lane:
                if item['bvid'] not in chosen:
                    chosen[item['bvid']] = item
                    added += 1
                if added >= 4 or len(chosen) >= MAX_SCAN:
                    break
        for item in sorted(alive, key=lambda x: -x['replies']):
            if len(chosen) >= MAX_SCAN:
                break
            chosen.setdefault(item['bvid'], item)

        async def inspect(item):
            nonlocal failures
            try:
                if not item['aid'] or not item['duration']:
                    detail = await client.detail(item['bvid'])
                    if detail:
                        item.update(detail)
                data = await client.get('/x/v2/reply',
                    {'type': 1, 'oid': item['aid'], 'pn': 1, 'ps': 20, 'sort': 2})
                item.update(judge(item, data.get('replies') or []))
                inspected.append(item)
            except (BiliUnavailable, ValueError, TypeError):
                failures += 1

        if await bounded_batch([inspect(x) for x in chosen.values()], 22):
            timed_out=True
            warnings.append('热评检查达到时间预算，不发送尚未核验的视频')
        if failures:
            warnings.append(f'{failures} 个公开接口请求未成功，未绕过限制')
        sparse = sum(0 < x['sample_count'] <= 3 for x in inspected)
        if sparse:
            warnings.append(f'{sparse} 条候选仅返回不超过3条热评；比例按可见样本计算，可在账号区选择扫码登录')
        items = sorted((x for x in inspected if x['eligible']), key=lambda x: -x['score'])
        if not items:
            warnings.append('没有通过搬屎门槛的视频，本轮不拿普通热门凑数')
        stats = {'candidates': len(pool), 'scanned': len(inspected), 'qualified': len(items),
                 'read_state':('partial' if inspected else 'failed') if failures or timed_out else ('ok' if items else 'empty'),
                 'failed_reads':failures,'budget_exhausted':timed_out,
                 'elapsed_seconds': round(time.monotonic() - started, 1)}
        return (time.time(), items, warnings, stats)

    cached = await client._cached_scan(key, build)
    return {'items': copy.deepcopy([x for x in cached[1] if x['bvid'] not in excluded]),
            'warnings': list(cached[2]), 'anonymous': client.anonymous, 'mode': 'chishi', 'stats': dict(cached[3])}
