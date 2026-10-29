import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger,delivery_scope
from data.plugins.astrbot_plugin_proactive_agent.life_state import ProactiveLife
from data.plugins.astrbot_plugin_proactive_agent.target_health import TargetHealth


class LifeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name)
        self.now=1793088000.;self.config={'enable':True,'allow_private':True,'allow_group':True,'allow_qzone':True,
            'reply_review_enabled':False,'fixed_private_ids':'1,2'};self.kv={};self.busy=False;self.sent=[]
        self.ledger=DeliveryLedger(root/'ledger.db');await self.ledger.start();self.addAsyncCleanup(self.ledger.close)
        async def get(key,default=None):return copy.deepcopy(self.kv.get(key,default))
        async def put(key,value):self.kv[key]=copy.deepcopy(value)
        async def send(origin,chain):
            self.sent.append((origin,chain));id_=self.ledger.begin(origin,'cid',chain,[])
            self.ledger.result(id_,'confirmed',str(len(self.sent)));return True
        self.plugin=SimpleNamespace(_cfg=self.config.get,_quiet_now=lambda:False,_id_set=lambda v:set(str(v).split(',')) if v else set(),
            get_kv_data=get,put_kv_data=put,_delivery=self.ledger,_recent_messages={},_build_chain=lambda s,images:s,
            _clean=lambda s:s,_work=SimpleNamespace(busy=lambda:self.busy),_qzone_allowed=AsyncMock(return_value=True),
            review_context=lambda origin:[],_generate_json=AsyncMock(return_value=json.dumps({'decision':'send','reason':'日常','content':'昨天那堆小东西总算收好了'})),
            context=SimpleNamespace(send_message=send,conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value='cid'))))
        self.plugin._targets=SimpleNamespace(check=AsyncMock(return_value={'available':True}),status=lambda origin:{'available':True,'reason':''})
        self.life=ProactiveLife(self.plugin,root/'life.db',clock=lambda:self.now);self.plugin._life=self.life
        await self.life.start();self.addAsyncCleanup(self.life.close)
        self.origin='aiocqhttp:FriendMessage:1'

    async def asyncTearDown(self):
        # Complete all disk-producing coroutines before Windows temp cleanup,
        # and assert the lifecycle rather than hiding a directory race.
        await self.life.close();await self.ledger.close()
        for store in (self.life.store,self.ledger.store):
            self.assertTrue(store.closed)
            self.assertFalse(store.pending)
            self.assertTrue(store.task is None or store.task.done())
            self.assertTrue(store._io is None or store._io.done())

    async def test_preview_is_read_only_and_private_event_does_not_cross_scope(self):
        self.life.event('chat_confirmed','只有这个会话知道的测试事项',visibility=self.origin,source_ids=['msg1'])
        before=copy.deepcopy(self.life.store.rows);pending=copy.deepcopy(self.life.store.pending)
        result=await self.life.opportunity(self.origin,dry_run=True)
        self.assertTrue(result['preview']);self.assertEqual(before,self.life.store.rows);self.assertEqual(pending,self.life.store.pending)
        self.assertIn('只有这个会话',self.life.context(self.origin))
        self.assertNotIn('只有这个会话',self.life.context('aiocqhttp:FriendMessage:2'))
        self.assertEqual(self.plugin._generate_json.await_count,0);self.assertEqual(self.sent,[])

    async def test_persisted_activity_changes_only_at_phase_boundary(self):
        self.life.advance();before=copy.deepcopy(self.life.activity());revision=self.life.store.rows['life']['revision']
        self.assertFalse(self.life.advance());self.assertEqual(self.life.activity()['revision'],revision)
        await self.life.close();self.life=ProactiveLife(self.plugin,Path(self.temp.name)/'life.db',clock=lambda:self.now)
        self.plugin._life=self.life;await self.life.start();self.addAsyncCleanup(self.life.close)
        self.assertEqual(self.life.activity()['current_activity'],before['current_activity'])
        self.now+=86400;self.assertTrue(self.life.advance());self.assertEqual(self.life.activity()['previous_activity'],before['current_activity'])

    async def test_unrelated_chat_keeps_persona_focus_but_life_questions_and_private_items_remain(self):
        self.life.advance()
        self.assertEqual(self.life.context(self.origin,'下班了，奖励自己一杯奶茶'),'')
        self.assertIn('current_activity',self.life.context(self.origin,'你今天在干嘛'))
        self.life.event('chat_confirmed','聊过的画稿还没整理好',visibility=self.origin,source_ids=['pending'],extra={'kind':'open_item'})
        self.assertIn('画稿',self.life.context(self.origin,'后来整理好了吗'))
        self.assertNotIn('画稿',self.life.context('aiocqhttp:FriendMessage:2','你今天在干嘛'))

    async def test_foreground_preempts_ordinary_generation_and_required_is_preserved(self):
        self.life.event('chat_confirmed','聊过游戏',visibility=self.origin,source_ids=['1'])
        self.busy=True
        result=await self.life.opportunity(self.origin,mode='fixed')
        self.assertEqual(result['decision'],'defer');self.assertEqual(self.plugin._generate_json.await_count,0)
        result=await self.life.opportunity(self.origin,mode='fixed',required=True)
        self.assertEqual(result['decision'],'confirmed');self.assertEqual(len(self.sent),1)

    async def test_unknown_receipt_never_becomes_said_or_replayed(self):
        self.life.event('chat_confirmed','聊过游戏',visibility=self.origin,source_ids=['1'])
        async def ambiguous(*args):delivery_scope.get()['state']='unknown'
        self.plugin.context.send_message=ambiguous
        result=await self.life.opportunity(self.origin)
        self.assertEqual(result['decision'],'unknown')
        before=self.plugin._generate_json.await_count
        result=await self.life.opportunity(self.origin)
        self.assertEqual(result['decision'],'defer');self.assertEqual(self.plugin._generate_json.await_count,before)
        self.assertFalse(self.life.store.rows.get('contact:'+self.origin,{}).get('last_confirmed_at'))

    async def test_unanswered_pauses_and_new_inbound_resets_pause(self):
        self.life.store.put('contact:'+self.origin,{'unanswered':2})
        event=self.life.event('chat_confirmed','可跟进事项',visibility=self.origin,source_ids=['1'])
        result=await self.life.opportunity(self.origin)
        self.assertIn('未得到回应',result['reason'])
        self.life.observe(self.origin,'2','接着说','1');self.now+=61
        self.assertEqual(self.life.decide(self.life.candidate(self.origin))['decision'],'send')

    async def test_dropped_reason_is_not_regenerated_on_next_check(self):
        self.life.event('chat_confirmed','无须联系的普通话题',visibility=self.origin,source_ids=['drop'])
        self.plugin._generate_json.return_value=json.dumps({'decision':'drop','reason':'没有联系动机'})
        self.assertEqual((await self.life.opportunity(self.origin))['decision'],'drop')
        self.assertEqual((await self.life.opportunity(self.origin))['decision'],'defer')
        self.assertEqual(self.plugin._generate_json.await_count,1)

    async def test_confirmed_send_memory_failure_cannot_reopen_delivery(self):
        from unittest.mock import patch
        self.life.event('chat_confirmed','事项',visibility=self.origin,source_ids=['send'])
        with patch('data.plugins.astrbot_plugin_proactive_agent.life_state.remember_sent',side_effect=OSError('full')):
            self.assertEqual((await self.life.opportunity(self.origin))['decision'],'confirmed')
        self.assertEqual(len(self.sent),1)
        self.assertTrue(any(r.get('status')=='confirmed' for r in self.life.store.rows.values()))

    async def test_resolution_requires_same_scope_original_human_statement(self):
        e=self.life.event('plan','明天整理画稿',visibility=self.origin,source_ids=['p'],extra={'kind':'promise'})
        message={'source_id':'done','text':'已经整理好了','sender':'1'}
        item={'event_id':e['id'],'source_id':'done','quote':'已经整理好了','status':'completed'}
        self.life.resolve_items('aiocqhttp:FriendMessage:2',[item],{'done':message})
        self.assertNotIn('resolved_at',self.life.store.rows['event:'+e['id']])
        self.life.resolve_items(self.origin,[item],{'done':{**message,'role':'assistant'}})
        self.assertNotIn('resolved_at',self.life.store.rows['event:'+e['id']])
        self.life.resolve_items(self.origin,[item],{'done':message})
        self.assertEqual(self.life.store.rows['event:'+e['id']]['resolution']['source'],'chat_confirmed')
        self.assertIsNone(self.life.candidate(self.origin))

    async def test_proactive_resolves_same_session_persona_as_normal_reply(self):
        from data.plugins.astrbot_plugin_proactive_agent.life_state import role_context
        self.plugin.context.get_config=lambda **kwargs:{}
        self.plugin.context.conversation_manager.get_conversation=AsyncMock(return_value=SimpleNamespace(persona_id='session-persona'))
        resolver=AsyncMock(return_value=('session-persona',{'prompt':'本会话专用人格'},None,False))
        self.plugin.context.persona_manager=SimpleNamespace(resolve_selected_persona=resolver,
            get_default_persona_v3=AsyncMock(return_value={'prompt':'动态默认人格'}))
        self.assertIn('本会话专用人格',await role_context(self.plugin,self.origin,cid='cid'))
        self.assertEqual(resolver.call_args.kwargs['conversation_persona_id'],'session-persona')
        self.assertIn('动态默认人格',await role_context(self.plugin,'qzone'))

    async def test_diagnostic_capacity_cannot_reopen_confirmed_candidate(self):
        self.life.advance();candidate=self.life.candidate(self.origin)
        self.life.store.put('candidate:'+candidate['id'],{**candidate,'status':'submitting'})
        original=self.life.store.put
        def full(key,value):
            if key.startswith('decision'):raise RuntimeError('diagnostic queue full')
            return original(key,value)
        from unittest.mock import patch
        with patch.object(self.life.store,'put',full):self.life.record_decision(candidate,{'decision':'confirmed','reason':'receipt'})
        self.assertEqual(self.life.store.rows['candidate:'+candidate['id']]['status'],'confirmed')


class TargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_self_missing_and_three_explicit_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=DeliveryLedger(Path(directory)/'ledger.db');await ledger.start()
            self.addAsyncCleanup(ledger.close)
            async def call(action):
                return {'get_friend_list':[{'user_id':1,'nickname':'普通好友'},{'user_id':2,'nickname':'我的电脑'},{'user_id':3,'nickname':'bot'}],
                    'get_group_list':[],'get_login_info':{'user_id':3}}[action]
            p=SimpleNamespace(_delivery=ledger,_call_onebot=call,_cfg={}.get,_id_set=lambda text:set())
            service=TargetHealth(p);await service.refresh()
            self.assertTrue(service.status('aiocqhttp:FriendMessage:1')['available'])
            for uid in ('2','3','4'):self.assertFalse(service.status('aiocqhttp:FriendMessage:'+uid)['available'])
            for _ in range(3):
                i=ledger.begin('aiocqhttp:FriendMessage:1','cid','x',[]);ledger.result(i,'failed',reason='ActionFailed')
            self.assertIn('暂停30分钟',service.status('aiocqhttp:FriendMessage:1')['reason'])
            self.assertEqual(ledger.health()['counts']['failed'],3)
            await ledger.close()
