"""Recipient-specific topics and durable duplicate checks, without cross-chat text."""
import asyncio
import hashlib
import json
import re
import time
import uuid
from difflib import SequenceMatcher

from .conversation_review import approve_once, request_scope
from .chat_punctuation import casual_punctuation
from .dragon_character import limit_father_address, limit_emoji

KEY = 'proactive_variety_v1'
TOPICS = (
    '游戏里囤到通关也不舍得用的道具', '动画角色换一件衣服就认不出的瞬间',
    '输入法自作聪明把一句话变成笑话', '起床闹钟的名字比闹钟本人还努力',
    '快捷键记住了，手却总按错另一个', '零食袋子的体积和里面食物的差距',
    '地图上明明很近却绕了半天的路', '歌单随机播放总撞上同一首歌',
    '游戏角色待机动作比玩家还忙', '加载进度到了99%突然开始思考人生',
    '收藏的教程与真正动手之间隔着什么', '聊天撤回之后反而更令人好奇',
    '耳机线或充电线为什么总能自己打结', '昵称取太长在游戏结算时被截断',
    '按跳过新手教程之后立刻迷路', '打开冰箱又关上这种毫无变化的探索',
    '截图里最抢眼的总是无关背景', '游戏装备为了外观牺牲一点属性',
    '一首歌的前奏只响两秒就认出来', '表情包存了一堆临用时找不到那张',
    '队友标了地点却朝相反方向走', '给虚拟宠物取的名字越来越离谱',
    '看标题以为一分钟解决，进度条却很长', '等待下载时盯着数字并不能让它变快',
    '突然忘记刚才打开这个软件要干什么', '想给房间收拾一角却发现童年小物件',
    '密码记不住却记得小时候的广告词', '冷门配角的一句台词意外很耐用',
    '按电梯按钮时明知亮了还想再按', '商店试用装的名字长到念不完',
    '游戏里习惯先打碎路边每一个箱子', '睡前说最后一局却匹配进漫长加时',
    '文件命名从认真描述走向随手乱敲', '看攻略时觉得懂了上手时还是迷路',
    '天气图标画得像食物引起的联想', '小龙尾巴在狭小房间转身的虚构烦恼',
    '二次元龙角与帽子形状的虚构矛盾', '便利贴上的待办字迹比任务更难懂',
    '看别人操作很轻松轮到自己手忙脚乱', '游戏里最想带回现实的一件普通小道具',
)
ANGLES = ('一个具体小槽点', '一个反常识但不需要查资料的小问题', '一句嘴硬的虚构龙娘吐槽',
          '一个轻松的二选一话题', '换个角度的一句观察', '一句直接说中实际情况的短吐槽，不作牵强比喻')


def normalized(text):
    return re.sub(r'[^\w\u4e00-\u9fff]', '', re.sub(r'本龙|老爸|爸爸', '', text)).lower()


def fingerprint(text):
    text = normalized(text)
    return sorted({hashlib.sha256(text[i:i+3].encode()).hexdigest()[:16] for i in range(max(0, len(text)-2))})


class TooSimilar(RuntimeError):
    pass


class ProactiveVariety:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()

    async def state(self):
        state = await self.plugin.get_kv_data(KEY, {})
        return state if isinstance(state, dict) else {}

    async def topic(self, origin, reserve=True):
        async with self.lock:
            state = await self.state()
            cursor = int(state.get('cursor', 0))
            state['cursor'] = cursor + 1
            if reserve:
                await self.plugin.put_kv_data(KEY, state)
        return TOPICS[cursor % len(TOPICS)] + '；表达角度：' + ANGLES[(cursor // len(TOPICS)) % len(ANGLES)]

    def history(self, origin):
        memory = getattr(self.plugin, '_sent_memory', None)
        return memory.proactive_recent(origin) if memory else []

    async def checked(self, content, origin):
        p = self.plugin
        mode = 'qzone' if origin == 'qzone' else 'chat'
        state = (request_scope(origin) or {}).get('character', {})
        content = casual_punctuation(limit_father_address(limit_emoji(content, state, mode), state, mode), mode)
        reviewer = getattr(p, '_reviewer', None)
        from .review_modes import current
        if current(p._cfg)['enabled'] and reviewer:
            content = await reviewer.review(content, origin=origin, mode=mode,
                recent=p.review_context(origin), prior_replies=self.history(origin), character=state)
            approve_once(origin,content,policy_version=p.review_policy_version() if hasattr(p,'review_policy_version') else None)
        return content

    async def duplicate(self, origin, content):
        candidate = normalized(content)
        own = self.history(origin)
        for old in own:
            previous = normalized(old)
            if candidate == previous or SequenceMatcher(None, candidate, previous).ratio() >= .65:
                return True
        hashes = set(fingerprint(content))
        state = await self.state()
        # Other conversations contribute only opaque word fingerprints; their
        # actual messages/identities are never supplied to this recipient's model.
        for row in state.get('fingerprints', []):
            if time.time() - row['at'] > 7 * 86400:
                continue
            old = set(row['hashes'])
            if hashes and old and len(hashes & old) / min(len(hashes), len(old)) >= .62:
                return True
        return False

    async def deliver(self, origin, content, send,attempt=None):
        content = await self.checked(content, origin)
        async with self.lock:
            if await self.duplicate(origin, content):
                raise TooSimilar('正文与近期主动消息过于相似，需要另换话题')
            # Reserve before transport: an ambiguous network failure must not
            # cause another recipient to receive the same generated template.
            state = await self.state()
            rows = [r for r in state.get('fingerprints', []) if time.time() - r['at'] < 7*86400]
            reservation=uuid.uuid4().hex
            state['fingerprints'] = (rows + [{'id':reservation,'at': time.time(), 'hashes': fingerprint(content)}])[-1000:]
            await self.plugin.put_kv_data(KEY, state)
        from .delivery_ledger import delivery_scope
        attempt=attempt if attempt is not None else {'state':'preparing'};token=delivery_scope.set(attempt)
        approve_once(origin, content)
        try:
            await send(content)
        except BaseException:
            # Explicit rejection or pre-transport cancellation releases a reservation.
            # Once submitted with no answer the content stays reserved, never replayed.
            if attempt['state'] in {'preparing','failed'}:
                async with self.lock:
                    state=await self.state()
                    state['fingerprints']=[r for r in state.get('fingerprints',[]) if r.get('id')!=reservation]
                    await self.plugin.put_kv_data(KEY,state)
            raise
        finally:delivery_scope.reset(token)
        return content


def variety(plugin):
    current = getattr(plugin, '_variety', None)
    if current is None:
        current = plugin._variety = ProactiveVariety(plugin)
    return current
