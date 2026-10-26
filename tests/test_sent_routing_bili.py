import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_maintenance import Main as ProactiveMain, ROOT
from aiocqhttp import CQHttp
from data.plugins.astrbot_plugin_proactive_agent.sent_memory import SentMemory, remember_sent
from data.plugins.astrbot_plugin_proactive_agent.route_guard import bind_route, explicit_route, install_route_guard
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer
from data.plugins.astrbot_plugin_proactive_agent.visual_intent import generation_kind
from data.plugins.astrbot_plugin_bili_scroll.client import BiliClient, BiliUnavailable, video, score
from data.plugins.astrbot_plugin_bili_scroll.main import Main, FIELDS, validate


class Memory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'sent.sqlite3'
        self.memory = SentMemory(self.path)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_persisted_same_conversation_only_and_reset(self):
        self.memory.record('qq:FriendMessage:1', 'c1', '实际审阅后发送的文字', 'fixed')
        loaded = SentMemory(self.path)
        self.assertIn('实际审阅后', loaded.note('qq:FriendMessage:1', 'c1'))
        self.assertEqual(loaded.note('qq:FriendMessage:2', 'c1'), '')
        self.assertEqual(loaded.note('qq:FriendMessage:1', 'c2'), '')

    async def test_first_proactive_send_adopted_once_and_no_qzone(self):
        self.memory.record('qq:FriendMessage:1', None, '第一条', 'fixed')
        self.memory.record('qzone', None, '动态', 'qzone')
        self.assertIn('第一条', self.memory.note('qq:FriendMessage:1', 'first'))
        self.assertEqual(self.memory.note('qq:FriendMessage:1', 'reset'), '')
        self.assertEqual(self.memory.note('qzone', None), '')

    async def test_history_write_failure_cannot_trigger_duplicate_delivery(self):
        p = SimpleNamespace(_sent_memory=SimpleNamespace(record=lambda *a: (_ for _ in ()).throw(OSError())),
                            logger=SimpleNamespace(error=lambda *a: None))
        await remember_sent(p, 'qq:FriendMessage:1', 'c', '已发', 'fixed')

    async def test_context_injected_even_when_meme_function_disabled(self):
        from test_maintenance import ConversationContext
        p = object.__new__(ProactiveMain)
        p.config = {'meme_context_enabled': False}
        p._sent_memory = self.memory
        event = ConversationContext().event('你刚才为什么这样说')
        origin = event.unified_msg_origin
        self.memory.record(origin, 'conv', '本龙想聊游戏存档', 'fixed')
        req = SimpleNamespace(prompt='为什么', system_prompt='', conversation=SimpleNamespace(cid='conv'))
        await p.enrich_conversation(event, req)
        self.assertIn('本龙想聊游戏存档', req.system_prompt)
        self.assertIn('真实发送者QQ', req.system_prompt)


class Routing(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = object.__new__(CQHttp)
        self.transport = AsyncMock(return_value={'message_id': 9})
        self.bot._api = SimpleNamespace(call_action=self.transport)
        self.plugin = SimpleNamespace(_platform=lambda: SimpleNamespace(bot=self.bot), _cfg=lambda k,d:d)
        self.restore = install_route_guard(self.plugin)

    async def asyncTearDown(self):
        self.restore()

    async def test_wrong_private_recipient_blocked_without_optional_review(self):
        bind_route('aiocqhttp:FriendMessage:1', '1')
        with self.assertRaises(RuntimeError):
            await self.bot.send_private_msg(user_id=2, message='不可误投')
        self.transport.assert_not_awaited()
        await self.bot.send_private_msg(user_id=1, message='正确')
        self.assertEqual(self.transport.call_args.kwargs['user_id'], 1)

    async def test_group_concurrency_keeps_body_without_automatic_quotes(self):
        async def send(sender, message_id):
            bind_route('aiocqhttp:GroupMessage:10', sender, message_id, True)
            await asyncio.sleep(0)
            await self.bot.send_group_msg(group_id=10, message=sender)
        await asyncio.gather(send('Alice', '100'), send('Bob', '200'))
        payloads = [c.kwargs['message'] for c in self.transport.call_args_list]
        self.assertEqual(payloads, ['Alice', 'Bob'])

    async def test_admin_explicit_send_restores_current_origin(self):
        bind_route('aiocqhttp:FriendMessage:1', '1')
        with explicit_route('aiocqhttp:FriendMessage:2'):
            await self.bot.send_private_msg(user_id=2, message='授权主动发送')
        with self.assertRaises(RuntimeError):
            await self.bot.send_private_msg(user_id=2, message='普通回复不许越界')

    async def test_unique_group_session_and_media_preserved(self):
        bind_route('aiocqhttp:GroupMessage:22_10', '22', '300', True)
        message = [{'type':'image','data':{'file':'base64://test'}}, {'type':'at','data':{'qq':'22'}}]
        await self.bot.send_group_msg(group_id=10, message=message)
        payload = self.transport.call_args.kwargs['message']
        self.assertEqual(payload, message)
        self.assertEqual(len(message), 2)


ITEM = {'bvid':'BV1xx411c7mD','aid':1,'title':'<em>抽象</em>动画','desc':'一句简介',
        'owner':{'name':'UP'}, 'duration':70, 'pubdate':int(time.time()), 'stat':{'reply':100}}


class Bili(unittest.IsolatedAsyncioTestCase):
    async def test_public_search_cached_and_dedup_per_target(self):
        client = BiliClient()
        async def get(path, params):
            return {'result':[ITEM]} if path.endswith('/type') else {'replies':[{'content':{'message':'@朋友 绷不住了'}}]}
        client.get = AsyncMock(side_effect=get)
        a = await client.discover('动画')
        self.assertEqual(a['items'][0]['title'], '抽象动画')
        self.assertEqual(a['items'][0]['url'], 'https://www.bilibili.com/video/BV1xx411c7mD')
        b = await client.discover('动画', ['BV1xx411c7mD'])
        self.assertEqual(b['items'], [])
        self.assertEqual(client.get.await_count, 2)
        self.assertEqual(len((await client.discover('动画'))['items']), 1)

    async def test_anonymous_search_blocked_does_not_share_unrelated_hot_video(self):
        client = BiliClient()
        async def get(path, params):
            if path.endswith('/type'):
                raise BiliUnavailable('HTTP 412')
            return {'list':[ITEM]}
        client.get = AsyncMock(side_effect=get)
        result = await client.discover('不存在的主题')
        self.assertEqual(result['items'], [])
        self.assertIn('412', result['warnings'][0])

    async def test_comment_failures_degrade_honestly_and_urls_validated(self):
        client = BiliClient()
        async def get(path, params):
            if path.endswith('/reply'):
                raise BiliUnavailable('受限')
            return {'list':[ITEM, {**ITEM,'bvid':'https://evil.example'}]}
        client.get = get
        result = await client.discover()
        self.assertEqual(len(result['items']), 1)
        self.assertIn('评论暂不可读', result['items'][0]['read_scope'])
        with self.assertRaises(ValueError):
            await BiliClient().get('/x/write/anything', {})

    async def test_review_rewrite_preserves_actual_video_link(self):
        url = 'https://www.bilibili.com/video/BV1xx411c7mD'
        model = AsyncMock(side_effect=[json.dumps({'pass':False,'natural':False,'chat_fit':True,'replacement':'这标题把本龙看愣了'}),
                                      '{"pass":true,"natural":true,"chat_fit":true}'])
        reviewer = ReplyReviewer(model, lambda *a: None)
        answer = await reviewer.review('首先总结一下\n'+url, origin='aiocqhttp:FriendMessage:1')
        self.assertIn(url, answer)
        self.assertEqual(model.await_count, 2)

    async def test_timers_each_target_failure_and_no_immediate_retry(self):
        p = object.__new__(Main);p.config={**FIELDS,'auto_enabled':True,'quiet_hours':'','group_ids':'10,11','private_ids':'20'}
        db = {}
        async def get(k,d=None):return copy.deepcopy(db.get(k,d))
        async def put(k,v):db[k]=copy.deepcopy(v)
        p.get_kv_data,p.put_kv_data=get,put
        p.share=AsyncMock(side_effect=[RuntimeError(), '已向当前会话分享', '已向当前会话分享'])
        with patch('data.plugins.astrbot_plugin_bili_scroll.main.time.time',return_value=10000):
            await p.tick()
        with patch('data.plugins.astrbot_plugin_bili_scroll.main.time.time',return_value=10000+120*60):
            await p.tick();await p.tick()
        self.assertEqual(p.share.await_count, 3)
        self.assertEqual(sorted(r['status'] for r in db['timers'].values()), ['failed','sent','sent'])

    async def test_only_confirmed_send_enters_history(self):
        with tempfile.TemporaryDirectory() as d:
            p=object.__new__(Main);p.config=dict(FIELDS);p._locks={}
            p._sent_memory=SentMemory(Path(d)/'sent.sqlite3')
            p.context=SimpleNamespace(send_message=AsyncMock(return_value=False))
            p.client=SimpleNamespace(discover=AsyncMock(return_value={'items':[video(ITEM)],'warnings':[]}))
            p.make_copy=AsyncMock(return_value='最终文案\n'+video(ITEM)['url'])
            p.get_kv_data=AsyncMock(return_value=[]);p.put_kv_data=AsyncMock()
            with self.assertRaises(RuntimeError):await p.share('aiocqhttp:FriendMessage:20')
            self.assertEqual(p._sent_memory.recent('aiocqhttp:FriendMessage:20',None), [])
            p.context.send_message.return_value=True
            async def confirmed(*args):
                from data.plugins.astrbot_plugin_bili_scroll.receipts import capture
                capture.get()[1].append({'state':'confirmed','message_id':'test-qq-42'})
                return True
            p.context.send_message.side_effect=confirmed
            await p.share('aiocqhttp:FriendMessage:20')
            self.assertIn('最终文案',p._sent_memory.note('aiocqhttp:FriendMessage:20','next'))

    def test_defaults_no_spam_and_validation(self):
        self.assertFalse(FIELDS['auto_enabled'])
        self.assertEqual(FIELDS['group_ids'], '')
        for field,value in [('interval_minutes',1),('private_ids','abc'),('quiet_hours','99:00-07:00')]:
            with self.assertRaises(ValueError):validate({**FIELDS,field:value})

    def test_video_browsing_is_not_image_generation(self):
        for text in ['看看B站上的搞笑视频', '想看动画视频', '给我看看bilibili']:
            self.assertEqual(generation_kind(text), 'none')
        self.assertEqual(generation_kind('画一张B站视频封面'), 'scene')
        self.assertEqual(generation_kind('看看你'), 'selfie')


if __name__=='__main__':unittest.main()
