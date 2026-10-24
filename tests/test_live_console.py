import asyncio
import copy
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from test_chat_flow import event
from data.plugins.astrbot_plugin_proactive_agent.group_flow import GroupFlow
from data.plugins.astrbot_plugin_proactive_agent.live_journal import LiveJournal, install_live_journal, text_parts
from data.plugins.astrbot_plugin_proactive_agent.chat_lane import install_chat_lane, MARKER, simple_chat
from data.plugins.astrbot_plugin_proactive_agent import dashboard_live_patch as dashboard
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.dashboard.services.conversation_service import ConversationService
from aiocqhttp import CQHttp


class QueueSafety(unittest.IsolatedAsyncioTestCase):
    async def test_busy_group_keeps_running_and_latest_passive_and_explicit(self):
        clock=[100];flow=GroupFlow({}.get,lambda:clock[0]);entered=asyncio.Event();release=asyncio.Event();seen=[]
        async def stage(_,e,p):
            seen.append(e.get_message_str())
            if len(seen)==1:
                entered.set();await release.wait()
                self.assertFalse(flow.stale(e))
            yield None
        async def run(e):
            flow.observe(e)
            async for _ in flow.process(stage,None,e,''):pass
        active=asyncio.create_task(run(event('最初概率',reason='probability')));await entered.wait()
        jobs=[]
        for i in range(6):
            clock[0]+=30
            jobs.append(asyncio.create_task(run(event('后续'+str(i),str(20000+i),reason='probability'))));await asyncio.sleep(0)
        jobs.append(asyncio.create_task(run(event('帮我找视频','50000'))));await asyncio.sleep(0)
        clock[0]+=60;release.set();await asyncio.gather(active,*jobs)
        self.assertEqual(seen,['最初概率','后续5','帮我找视频'])
        self.assertEqual(flow.pending_passive,{})


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_visibility_restart_isolation_and_actual_success_only(self):
        with tempfile.TemporaryDirectory() as d:
            journal=LiveJournal(Path(d)/'journal.sqlite3');await journal.start()
            journal.add('qq:GroupMessage:2_10','消息还没生成回复','user','测试者')
            self.assertIn('消息还没生成',journal.history('qq:GroupMessage:3_10')[0]['content'])
            self.assertEqual(journal.history('qq:FriendMessage:10'),[])
            bot=object();plugin=SimpleNamespace(_live_journal=journal,_platform=lambda:SimpleNamespace(bot=bot),_cfg=lambda k,d:'qq' if k=='platform_id' else d)
            with patch.object(CQHttp,'call_action',AsyncMock(return_value={'message_id':1})):
                restore=install_live_journal(plugin)
                try:
                    await CQHttp.call_action(bot,'send_group_msg',group_id=10,message=[{'type':'text','data':{'text':'实际发出'}},{'type':'image','data':{'file':'private-file-path'}}])
                finally:restore()
            with patch.object(CQHttp,'call_action',AsyncMock(side_effect=RuntimeError('offline'))):
                restore=install_live_journal(plugin)
                try:
                    with self.assertRaises(RuntimeError):await CQHttp.call_action(bot,'send_group_msg',group_id=10,message='不应写入')
                finally:restore()
            self.assertEqual(len(journal.history('qq:GroupMessage:10')),2)
            self.assertNotIn('private-file-path',str(journal.history('qq:GroupMessage:10')))
            await journal.close();again=LiveJournal(journal.path);await again.start()
            self.assertEqual(len(again.history('qq:GroupMessage:10')),2);await again.close()

    async def test_native_detail_retains_context_and_permissions(self):
        j=SimpleNamespace(history=lambda o:[{'role':'user','content':'新消息'}],error=None)
        plugin=SimpleNamespace(_live_journal=j)
        original={'user_id':'qq:FriendMessage:1','history':'[]'}
        with patch.object(ConversationService,'get_conversation_detail',AsyncMock(return_value=copy.deepcopy(original))):
            restore=install_live_journal(plugin)
            try:
                result=await ConversationService.get_conversation_detail(None,{})
                self.assertEqual(result['history'],'[]');self.assertEqual(len(result['qqbot_live_history']),1)
            finally:restore()
        with patch.object(ConversationService,'get_conversation_detail',AsyncMock(side_effect=ValueError('denied'))):
            restore=install_live_journal(plugin)
            try:
                with self.assertRaises(ValueError):await ConversationService.get_conversation_detail(None,{})
            finally:restore()


class ChatLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_simple_scope_tool_preservation_overrides_and_fallback(self):
        p=object.__new__(ProviderOpenAIOfficial);p.provider_config={'model':'gpt-5.4','custom_extra_body':{'temperature':.7}}
        calls=[];tool=object();payload={'model':'gpt-5.4','messages':[{'role':'system','content':MARKER},{'role':'user','content':'十连跪'}]}
        async def query(prov,data,tools,**kw):calls.append((prov,data,tools,kw));return 'ok'
        with patch.object(ProviderOpenAIOfficial,'_query',query):
            restore=install_chat_lane({'chat_low_latency':True}.get)
            try:
                await p._query(payload,tool,request_max_retries=1)
                self.assertEqual(calls[-1][0].provider_config['custom_extra_body']['reasoning_effort'],'low')
                self.assertIs(calls[-1][2],tool);self.assertNotIn('reasoning_effort',p.provider_config['custom_extra_body'])
                p.provider_config['custom_extra_body']['reasoning_effort']='high';await p._query(payload,tool)
                self.assertIs(calls[-1][0],p)
            finally:restore()
        p.provider_config['custom_extra_body'].pop('reasoning_effort')
        class Unsupported(Exception):status_code=400
        async def reject(prov,*a,**kw):
            calls.append(prov)
            if prov is not p:raise Unsupported('reasoning_effort unsupported')
            return 'fallback'
        with patch.object(ProviderOpenAIOfficial,'_query',reject):
            restore=install_chat_lane({'chat_low_latency':True}.get)
            try:
                self.assertEqual(await p._query(payload,tool),'fallback')
                n=len(calls);await p._query(payload,tool);self.assertEqual(len(calls),n+1)
            finally:restore()

    def test_complex_tasks_not_selected(self):
        state={'mood':'cheeky','visual_intent':'none'}
        self.assertTrue(simple_chat('我又拖到最后一天才写作业',state))
        for text in ['帮我找个视频','画一张猫','分析这道数学题','设置主动时间']:
            self.assertFalse(simple_chat(text,state))
        self.assertFalse(simple_chat('救命',{'mood':'serious'}))


class DashboardTests(unittest.TestCase):
    def test_patch_idempotence_and_unknown_version(self):
        original=dashboard.ASSET.with_suffix('.js.qqbot-original')
        source=(original if original.exists() else dashboard.ASSET).read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'bundle.js';path.write_text(source,encoding='utf-8')
            self.assertTrue(dashboard.repair(path));self.assertFalse(dashboard.repair(path))
            updated=path.read_text(encoding='utf-8')
            self.assertIn('qqClosed=true',updated);self.assertIn('.reverse()',updated)
            path.write_text('unknown version',encoding='utf-8')
            with self.assertRaises(ValueError):dashboard.repair(path)
            self.assertEqual(path.read_text(encoding='utf-8'),'unknown version')
