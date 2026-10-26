import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_maintenance import Main
from test_dragon_character import event as basic_event
from test_sent_routing_bili import ITEM
from data.plugins.astrbot_plugin_bili_scroll.main import Main as BiliMain, FIELDS
from data.plugins.astrbot_plugin_bili_scroll.client import video
from data.plugins.astrbot_plugin_proactive_agent.group_flow import GroupFlow
from data.plugins.astrbot_plugin_proactive_agent.dragon_character import CharacterState, limit_father_address
from data.plugins.astrbot_plugin_proactive_agent.proactive_variety import ProactiveVariety, TooSimilar
from data.plugins.astrbot_plugin_proactive_agent.fixed_schedule import FixedScheduler
from data.plugins.astrbot_plugin_proactive_agent.sent_memory import SentMemory
from data.plugins.astrbot_plugin_proactive_agent.route_guard import bind_route, install_route_guard, explicit_route
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.retrieval import RetrievalMixin
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.common import PENDING_PROACTIVE_EMOJI_EXTRA_KEY
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image
from aiocqhttp import CQHttp


def event(text, sender='12345', group='100', reason='keyword'):
    e = basic_event(text, sender, group)
    e.get_messages = lambda: [Plain(text)]
    e.get_platform_id = lambda: 'aiocqhttp'
    e.get_self_id = lambda: '99999'
    e.set_extra('qqbot_group_wake', reason)
    e.stop_event = Mock()
    async def deliver(*args,**kwargs):
        from data.plugins.astrbot_plugin_bili_scroll.receipts import capture
        scope=capture.get()
        if scope:scope[1].append({'state':'confirmed','message_id':'test-qq-42'})
    e.send = AsyncMock(side_effect=deliver)
    return e


class Freshness(unittest.IsolatedAsyncioTestCase):
    async def test_burst_coalesces_without_cross_sender_or_dropping_explicit_tasks(self):
        flow = GroupFlow({}.get)
        entered, release = asyncio.Event(), asyncio.Event()
        processed = []
        async def stage(_, e, prefix):
            if not processed:
                entered.set()
                await release.wait()
            processed.append((e.get_sender_id(), e.get_message_str(), e.get_extra('qqbot_chat_batch')))
            yield None
        async def run(e):
            flow.observe(e)
            async for _ in flow.process(stage, None, e, ''):
                pass
        first = asyncio.create_task(run(event('正在回复的消息')))
        await entered.wait()
        tasks=[]
        for e in [event('前半句'), event('另一位的消息', '22222'), event('补充后半句'), event('帮我找个视频')]:
            tasks.append(asyncio.create_task(run(e)))
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, *tasks)
        self.assertEqual(len(processed), 4)
        self.assertEqual(processed[1][0], '22222')
        self.assertEqual(processed[2][2], ['前半句', '补充后半句'])
        self.assertEqual(processed[3][1], '帮我找个视频')
        self.assertEqual(flow.stats['merged'], 1)

    async def test_only_passive_expires_and_latest_context_is_group_scoped(self):
        clock = [100]
        flow = GroupFlow({}.get, lambda: clock[0])
        passive, explicit = event('嗯', reason='probability'), event('肥龙来聊')
        flow.observe(passive); flow.observe(explicit)
        for i in range(4):
            clock[0] += 1
            flow.observe(event('话题变了' + str(i), sender='22222'))
        flow.observe(event('另一个群秘密', group='200'))
        flow.pending_passive['aiocqhttp:GroupMessage:100'] = [passive, explicit]
        self.assertTrue(flow.stale(passive))
        self.assertFalse(flow.stale(explicit))
        self.assertIn('话题变了', flow.note(explicit))
        self.assertNotIn('另一个群秘密', flow.note(explicit))
        fresh=event('新的',reason='probability'); flow.observe(fresh)
        clock[0]+=26
        flow.pending_passive['aiocqhttp:GroupMessage:100'] = [fresh]
        self.assertFalse(flow.stale(fresh))

    async def test_cancelled_waiter_releases_queue_and_does_not_cancel_active(self):
        flow=GroupFlow({}.get); gate=asyncio.Event(); entered=asyncio.Event()
        async def stage(_,e,p):
            entered.set(); await gate.wait(); yield None
        async def run(e):
            flow.observe(e)
            async for _ in flow.process(stage,None,e,''): pass
        active=asyncio.create_task(run(event('工作中'))); await entered.wait()
        queued=asyncio.create_task(run(event('等待'))); await asyncio.sleep(0)
        queued.cancel(); await asyncio.gather(queued,return_exceptions=True)
        self.assertFalse(active.done()); gate.set(); await active
        self.assertEqual(flow.waiting,{})

    async def test_stale_after_review_clears_reply_and_meme_preparation(self):
        p=object.__new__(Main);p.config={};p._group_flow=GroupFlow({}.get,lambda:100)
        e=event('旧话题',reason='probability');e.set_extra('qqbot_arrival',50)
        p._group_flow.pending_passive['aiocqhttp:GroupMessage:100'] = [e,event('新话题',reason='probability')]
        cleanup=AsyncMock();e.set_extra('qqbot_cancel_reply_preparation',cleanup)
        response=SimpleNamespace(completion_text='旧回复',result_chain=MessageChain([Plain('旧回复')]))
        self.assertTrue(await p.drop_stale_reply(e,response))
        self.assertEqual(response.completion_text,'');self.assertEqual(response.result_chain.chain,[])
        cleanup.assert_awaited_once()


class Speech(unittest.TestCase):
    def test_address_probability_and_limit_leave_relationship_and_family_facts(self):
        for probability, roll, allowed in [(0,0,False),(20,.19,True),(20,.2,False),(100,.99,True)]:
            state=CharacterState(roll=lambda:roll).for_message('o','12345','你好',{'father_qq':'12345','father_address_probability':probability}.get)
            self.assertTrue(state['is_father']);self.assertEqual(state['father_address_allowed'],allowed)
        state={'is_father':True,'father_address_allowed':False}
        self.assertEqual(limit_father_address('爸，本龙知道啦',state),'本龙知道啦')
        self.assertEqual(limit_father_address('对不起嘛爸，本龙嘴快了',state),'对不起嘛，本龙嘴快了')
        self.assertEqual(limit_father_address('他爸爸的游戏账号',state),'他爸爸的游戏账号')
        self.assertEqual(limit_father_address('爸，状态原文',state,'management'),'爸，状态原文')


class VideoOutcome(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        p=object.__new__(BiliMain);p.config=dict(FIELDS);p._locks={}
        p.context=SimpleNamespace();p.client=SimpleNamespace(discover=AsyncMock(return_value={'items':[video(ITEM)],'warnings':[]}))
        p.get_kv_data=AsyncMock(return_value=[]);p.put_kv_data=AsyncMock()
        p.make_copy=AsyncMock(return_value='本龙找的，看看\n'+video(ITEM)['url'])
        return p

    async def test_success_stops_only_single_purpose_and_duplicate_tool_does_not_send_twice(self):
        p=self.plugin();e=event('肥龙去B站给我找个小猫视频')
        result=await p.share_video(e,'小猫','normal')
        self.assertTrue(result.startswith('已向'));e.stop_event.assert_called_once()
        await p.share_video(e,'小猫','normal')
        e.send.assert_awaited_once();p.client.discover.assert_awaited_once()
        p=self.plugin();e=event('找个视频然后帮我画张猫')
        await p.share_video(e,'小猫','normal')
        e.stop_event.assert_not_called()

    async def test_failure_and_no_match_never_mark_success(self):
        p=self.plugin();e=event('找个视频');e.send.side_effect=RuntimeError('断网')
        self.assertIn('未能确认',await p.share_video(e,'猫','normal'))
        self.assertIsNone(e.get_extra('qqbot_bili_sent'));e.stop_event.assert_not_called()
        p=self.plugin();p.client.discover.return_value={'items':[],'warnings':[]};e=event('找个视频')
        self.assertIn('没有找到',await p.share_video(e,'猫','normal'))
        e.send.assert_not_awaited();e.stop_event.assert_not_called()

    async def test_actual_framework_tool_runner_does_not_query_again_after_delivery(self):
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        from astrbot.core.agent.hooks import BaseAgentRunHooks
        from astrbot.core.provider.entities import ProviderRequest, LLMResponse
        from astrbot.core.astr_agent_run_util import run_agent
        p=self.plugin();e=event('给我找个小猫视频')
        stopped=[False]
        e.stop_event=lambda:stopped.__setitem__(0,True)
        e.is_stopped=lambda:stopped[0]
        e.get_platform_name=lambda:'aiocqhttp';e.trace=SimpleNamespace(record=lambda *a,**k:None)
        e.clear_result=Mock();e.set_result=Mock()
        first=LLMResponse(role='assistant',completion_text='',tools_call_name=['share_bilibili_video'],
                          tools_call_args=[{'keyword':'小猫','mode':'normal'}],tools_call_ids=['call1'])
        provider=SimpleNamespace(provider_config={'id':'test','model':'test'},get_model=lambda:'test',
            text_chat=AsyncMock(side_effect=[first,AssertionError('不应发生第二轮模型请求')]))
        class Executor:
            @classmethod
            async def execute(cls,tool,run_context,**args):
                from mcp.types import CallToolResult, TextContent
                yield CallToolResult(content=[TextContent(type='text',text=await p.share_video(e,**args))])
        runner=ToolLoopAgentRunner()
        await runner.reset(provider,ProviderRequest(prompt='给我找个小猫视频',func_tool=ToolSet([
            FunctionTool(name='share_bilibili_video',description='分享视频',parameters={'type':'object'})])),
            ContextWrapper(context=SimpleNamespace(event=e)),Executor(),BaseAgentRunHooks(),request_max_retries=1)
        async for _ in run_agent(runner,show_tool_use=False,show_tool_call_result=False):pass
        provider.text_chat.assert_awaited_once();e.send.assert_awaited_once()
        self.assertIsNotNone(e.get_extra('qqbot_bili_sent'))


class Quotes(unittest.IsolatedAsyncioTestCase):
    async def test_switch_targets_current_message_once_and_no_proactive_quote(self):
        bot=object.__new__(CQHttp);transport=AsyncMock(return_value={'message_id':42})
        bot._api=SimpleNamespace(call_action=transport)
        cfg={'auto_quote_enabled':True}
        p=SimpleNamespace(_cfg=cfg.get,_platform=lambda:SimpleNamespace(bot=bot))
        restore=install_route_guard(p)
        try:
            async def send(sender,mid):
                bind_route('aiocqhttp:GroupMessage:10',sender,mid,True)
                await asyncio.sleep(0)
                await bot.send_group_msg(group_id=10,message=sender)
                await bot.send_group_msg(group_id=10,message=[{'type':'image','data':{'file':'test.png'}}])
            await asyncio.gather(send('Alice','101'),send('Bob','102'))
            quoted=[c.kwargs['message'][0]['data']['id'] for c in transport.call_args_list if c.kwargs['message'][0]['type']=='reply']
            self.assertEqual(quoted,['101','102'])
            cfg['auto_quote_enabled']=False;bind_route('aiocqhttp:GroupMessage:10','Alice','103',True)
            await bot.send_group_msg(group_id=10,message='关闭后原文')
            self.assertEqual(transport.call_args.kwargs['message'],'关闭后原文')
            cfg['auto_quote_enabled']=True
            with explicit_route('aiocqhttp:GroupMessage:10'):
                await bot.send_group_msg(group_id=10,message='主动话题')
            self.assertEqual(transport.call_args.kwargs['message'],'主动话题')
        finally:restore()


class Variety(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        p=object.__new__(Main);p.config={'reply_review_enabled':False};db={}
        async def get(k,d=None):return copy.deepcopy(db.get(k,d))
        async def put(k,v):db[k]=copy.deepcopy(v)
        p.get_kv_data,p.put_kv_data=get,put
        return p,db

    async def test_topics_unique_parallel_and_survive_restart(self):
        p,db=self.plugin();v=ProactiveVariety(p)
        topics=await asyncio.gather(*(v.topic(str(i)) for i in range(10)))
        self.assertEqual(len(set(topics)),10)
        self.assertNotIn(await ProactiveVariety(p).topic('0'),topics)
        before=copy.deepcopy(db);await v.topic('preview',reserve=False);self.assertEqual(db,before)

    async def test_cross_target_duplicate_checked_after_review_no_private_text_shared(self):
        p,db=self.plugin();p.config['reply_review_enabled']=True;p._recent_messages={}
        p._reviewer=SimpleNamespace(review=AsyncMock(return_value='游戏里的药囤到通关都没舍得用'))
        v=ProactiveVariety(p);send=AsyncMock()
        await v.deliver('aiocqhttp:FriendMessage:1','第一份不同草稿',send)
        with self.assertRaises(TooSimilar):await v.deliver('aiocqhttp:FriendMessage:2','第二份不同草稿',send)
        send.assert_awaited_once()
        self.assertNotIn('游戏里的药',json.dumps(db,ensure_ascii=False))
        self.assertNotIn('FriendMessage:1',json.dumps(db))
        self.assertTrue(await ProactiveVariety(p).duplicate('aiocqhttp:FriendMessage:2','游戏里的药囤到通关都没舍得用'))

    async def test_same_recipient_history_spans_conversation_reset_but_not_other_chat(self):
        p,_=self.plugin()
        with tempfile.TemporaryDirectory() as d:
            p._sent_memory=SentMemory(Path(d)/'sent.sqlite3')
            p._sent_memory.record('a','old','第一位自己聊过的话','fixed')
            p._sent_memory.record('b','other','另一位的私密话','fixed')
            v=ProactiveVariety(p)
            self.assertEqual(v.history('a'),['第一位自己聊过的话'])
            self.assertTrue(await v.duplicate('a','第一位自己聊过的话'))

    async def test_fixed_generation_retries_duplicate_but_keeps_each_recipient_slot(self):
        p,_=self.plugin();p.config.update(enable=True,allow_private=True,fixed_messages_enabled=True,fixed_private_ids='1,2',fixed_message_interval_minutes=30)
        p._quiet_now=lambda:False;p._recent_messages={}
        responses=iter(['游戏道具攒到通关也舍不得用','游戏道具攒到通关也舍不得用','输入法是不是偷偷给本龙改了名'])
        model=SimpleNamespace(text_chat=AsyncMock(side_effect=lambda **kw:SimpleNamespace(completion_text=next(responses))))
        p.context=SimpleNamespace(get_using_provider_async=AsyncMock(return_value=model),send_message=AsyncMock(return_value=True))
        s=FixedScheduler(p)
        await s.generate_and_send('private:1',1800);await s.generate_and_send('private:2',1800)
        self.assertEqual(p.context.send_message.await_count,2)
        self.assertEqual(model.text_chat.await_count,3)
        texts=[c.args[1].chain[0].text for c in p.context.send_message.call_args_list]
        self.assertNotEqual(*texts)


class EmojiDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_combined_and_separate_probabilities_preserve_image_and_send_once(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'meme.png';path.write_bytes(b'test')
            for percent,roll,separate in [(0,0,False),(50,.49,True),(50,.5,False),(100,.99,True)]:
                p=RetrievalMixin();p.context=SimpleNamespace(get_registered_star=lambda _:SimpleNamespace(activated=True,star_cls=SimpleNamespace(_cfg={'meme_separate_probability':percent}.get)))
                p._abs_plugin_data_path=lambda _:path;p._tags_from_item=lambda _:[];p._tags_for_library_path=lambda _:[]
                p._prepare_send_image_path=AsyncMock(return_value=(path,[]));p._defer_send_image_style_cleanup=lambda e,x:x
                p._after_plugin_sent_image_for_meme_combat=AsyncMock();p._log_proactive_emoji_debug=lambda *a:None
                p._cleanup_temp_paths=lambda _:None;p._proactive_emoji_config=lambda:{}
                e=event('闲聊');result=MessageChain([Plain('回复文字')])
                with patch('data.plugins.astrbot_plugin_smart_imagechat_hub.backend.retrieval.random.random',return_value=roll):
                    await p._apply_proactive_emoji_image(e,result,{'rel_path':'meme.png'},{'embed_in_conversation':True})
                self.assertEqual(any(isinstance(x,Image) for x in result.chain),not separate)
                await p._send_pending_proactive_emoji(e);await p._send_pending_proactive_emoji(e)
                self.assertEqual(e.send.await_count,int(separate))
                self.assertIsNone(e.get_extra(PENDING_PROACTIVE_EMOJI_EXTRA_KEY))


if __name__=='__main__':unittest.main()
