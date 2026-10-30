import asyncio,json,time,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from test_reply_pipeline import plugin
from test_chat_flow import event
from data.plugins.astrbot_plugin_proactive_agent import review_modes as modes
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer,OutboundGuard,bind_request,ReviewUnavailable
from data.plugins.astrbot_plugin_proactive_agent.send_checks import issues,normalize,verbatim_request,snapshot

def verdict(ok,replacement=''):
    return json.dumps(dict(pass_=ok)).replace('pass_','pass') if False else json.dumps({'pass':ok,'natural':ok,'chat_fit':ok,'reason':'具体问题','replacement':replacement})

class Modes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):self.token=modes._settings.set(None)
    async def asyncTearDown(self):modes._settings.reset(self.token)

    async def test_prompt_dedup_preserves_current_speaker_and_original_message(self):
        from test_maintenance import Main
        from test_async_emoji import Event
        from data.plugins.astrbot_plugin_proactive_agent.speaker_context import MARKER
        p=object.__new__(Main);p.config={'meme_context_enabled':False,'reply_review_enabled':False}
        p.context=SimpleNamespace(get_config=lambda **_: {})
        e=Event('我是另外一个人，不要把刚才那人的经历算给我',sender='2001')
        req=SimpleNamespace(prompt=e.get_message_str(),system_prompt='',conversation=None)
        await p.enrich_conversation(e,req)
        self.assertTrue(req.prompt.endswith(e.get_message_str()))
        self.assertEqual(json.loads(req.prompt.splitlines()[0][len(MARKER):])['QQ'],'2001')
        self.assertIn('真实QQ',req.system_prompt)
        self.assertNotIn(e.get_message_str(),req.system_prompt)
        self.assertIn('2001',req.system_prompt)
        self.assertIsNone(e.get_extra('qqbot_reply_prediction'))

    def test_dedicated_voice_and_speaker_facts_are_not_duplicated(self):
        from data.plugins.astrbot_plugin_proactive_agent.dragon_character import character_note
        from data.plugins.astrbot_plugin_proactive_agent.speaker_context import speaker_note
        from data.plugins.astrbot_plugin_proactive_agent.voice_memory import voice_note
        state={'group_speaker':{'QQ':'2001'},'voice_capability':{'available':True},
               'voice_receipts':'确认音频原文：确实听见啦','is_father':False,'emoji_allowed':True}
        prompt=character_note(state)+speaker_note(state)+voice_note(state)
        self.assertEqual(prompt.count('确认音频原文'),1)
        self.assertEqual(prompt.count('2001'),1)
        self.assertIn('已启用语音输出',prompt)
        self.assertIn('emoji_allowed',prompt)

    async def test_current_persona_bans_reach_first_generation_without_review(self):
        from test_maintenance import Main
        from test_async_emoji import Event
        from unittest.mock import patch
        p=object.__new__(Main);p.config={'meme_context_enabled':False,'reply_review_enabled':False}
        p.context=SimpleNamespace(get_config=lambda **_: {})
        async def bound(plugin,event,req,state):state['persona_prompt']='不使用“喵”作为自己的口癖'
        e=Event('你俩什么关系喵',sender='2001')
        req=SimpleNamespace(prompt=e.get_message_str(),system_prompt='',conversation=None)
        with patch('data.plugins.astrbot_plugin_proactive_agent.persona_authority.bind_persona',side_effect=bound):
            await p.enrich_conversation(e,req)
        self.assertIn('当前人格已禁止自用的口癖：["喵"]',req.system_prompt)
        self.assertIn('逐字引用仍保留原文',req.system_prompt)
        self.assertIsNone(e.get_extra('qqbot_reply_prediction'))

    def test_defaults_legacy_and_modes(self):
        self.assertEqual(modes.configured({})['mode'],'off')
        self.assertEqual(modes.configured({})['policy'],'speed_first')
        for cfg,expected in [({'reply_review_enabled':True},'speed_first'),
            ({'reply_review_enabled':True,'reply_prediction_enabled':False},'all'),
            ({'reply_review_enabled':False,'review_policy':'all'},'off')]:
            self.assertEqual(modes.configured(cfg)['mode'],expected)

    async def test_off_and_full_with_candidates_do_not_predict(self):
        for enabled,policy in [(False,'speed_first'),(False,'all'),(True,'all')]:
            p=plugin();p.config.update(reply_review_enabled=enabled,review_policy=policy)
            p._generate_json=AsyncMock();e=event('今天不错')
            self.assertIsNone(p.prepare_reply_prediction(e,[{'id':'a'}]))
            p._generate_json.assert_not_awaited()
            self.assertEqual(p._prediction_stats['started'],0)

    async def test_selection_is_independent_and_local_candidate_costs_zero(self):
        p=plugin();p.config['reply_review_enabled']=False;p._generate_json=AsyncMock()
        e=event('哈哈');answer=await p.prepare_emoji_selection(e,[{'id':'happy','tags':['开心'],'proactive_fast_fallback':True}])
        self.assertEqual(answer['image_ids'],['happy']);p._generate_json.assert_not_awaited()
        self.assertIsNone(e.get_extra('qqbot_reply_prediction'))

    async def test_mode_pinned_and_not_gated_by_latency_toggle(self):
        p=plugin();p.config.update(reply_review_enabled=True,latency_optimization_enabled=False)
        p._generate_json=AsyncMock(return_value='{"risk":"low"}')
        e=event('今天不错');e.set_extra('qqbot_character',{})
        await p.prepare_reply_prediction(e);p.config['reply_review_enabled']=False
        self.assertEqual(p.review_settings(e)['mode'],'speed_first')
        self.assertEqual(p.reply_prediction(e)['risk'],'low')
        self.assertEqual(p.review_settings(event('新的一条'))['mode'],'off')
        await p._close_reply_pipeline()

    async def test_no_full_review_during_grace_and_only_one_after_unknown(self):
        for risk in ['low','unknown','high']:
            p=plugin();p.config['reply_review_enabled']=True;e=event('下班喝奶茶')
            e.set_extra('qqbot_character',{});release=asyncio.Event()
            async def predict(*_):await release.wait();return json.dumps({'risk':risk})
            p._generate_json=predict;p.prepare_reply_prediction(e)
            t=asyncio.create_task(p.review_with_pending_prediction(e,'好喝',reasons=['prediction_unavailable'],recent=[],mode='chat',media=0,prior_replies=[],state={},used_tools=False))
            await asyncio.sleep(.02);p._reviewer.review.assert_not_awaited();release.set()
            checked,fast=await t;self.assertEqual(fast,risk=='low')
            self.assertEqual(p._reviewer.review.await_count,0 if risk=='low' else 1)
            await p._close_reply_pipeline()

    async def test_late_low_cannot_rescue_full_failure(self):
        p=plugin();p.config['reply_review_enabled']=True;e=event('今天好困');e.set_extra('qqbot_character',{})
        async def slow(*_):await asyncio.sleep(2);return '{"risk":"low"}'
        p._generate_json=slow;p.prepare_reply_prediction(e);p._reviewer.review.side_effect=ReviewUnavailable('failed')
        started=time.monotonic()
        with self.assertRaises(ReviewUnavailable):
            await p.review_with_pending_prediction(e,'去睡吧',reasons=[],recent=[],mode='chat',media=0,prior_replies=[],state={},used_tools=False)
        self.assertLess(time.monotonic()-started,.6);p._reviewer.review.assert_awaited_once()
        await p._close_reply_pipeline()

    async def test_two_rewrites_keep_original_and_accumulated_feedback(self):
        generate=AsyncMock(side_effect=[verdict(False,'第二稿'),verdict(False,'第三稿'),verdict(True)])
        audit=Mock();review=ReplyReviewer(generate,audit)
        self.assertEqual(await review.review('原稿',origin='a'), '第三稿')
        self.assertEqual(generate.await_count,3)
        self.assertEqual(generate.call_args.args[1]['原稿'],'原稿')
        self.assertTrue(generate.call_args.args[1]['必须修正的问题'])
        self.assertTrue(audit.call_args.args[1]['extended'])

    async def test_same_task_draft_coalesces_but_context_changes_do_not(self):
        bind_request('a',['问题'],False,{},modes.configured({'reply_review_enabled':True}))
        async def generate(*_):await asyncio.sleep(.03);return verdict(True)
        fn=AsyncMock(side_effect=generate);r=ReplyReviewer(fn,Mock())
        self.assertEqual(await asyncio.gather(r.review('好',origin='a'),r.review('好',origin='a')),['好','好'])
        self.assertEqual(fn.await_count,1)
        await asyncio.gather(r.review('好',origin='a',recent=['b']),r.review('好',origin='a',recent=['c']))
        self.assertEqual(fn.await_count,3)

    async def test_off_guard_formats_without_review_and_records_path(self):
        p=plugin();p.config['reply_review_enabled']=False
        bind_request('aiocqhttp:FriendMessage:1',[],False,{},modes.configured(p.config))
        guard=OutboundGuard(p,p._reviewer)
        result=await guard.prepare('send_private_msg',{'user_id':1,'message':'好呀。'})
        p._reviewer.review.assert_not_awaited();self.assertEqual(result['message'][0]['data']['text'],'好呀')
        check=snapshot('aiocqhttp:FriendMessage:1','好呀')
        self.assertEqual(check['path'],'local_repair');self.assertFalse(check['final_body_reviewed'])

    async def test_foreign_forward_quote_keeps_original(self):
        p=plugin();p.config['reply_review_enabled']=True
        p._reviewer.review=AsyncMock(return_value='自己这句改写')
        bind_request('aiocqhttp:GroupMessage:1',[],False,{},modes.configured(p.config))
        message=[{'type':'node','data':{'uin':'other','content':[{'type':'text','data':{'text':'第三方原话。'}}]}},
                 {'type':'node','data':{'uin':'bot','content':[{'type':'text','data':{'text':'我写的句子'}}]}}]
        result=await OutboundGuard(p,p._reviewer).prepare('send_group_forward_msg',{'group_id':1,'messages':message},bot=SimpleNamespace(self_id='bot'))
        self.assertEqual(result['messages'][0],message[0]);p._reviewer.review.assert_awaited_once()

    def test_objective_checks_preserve_quotes_help_and_identity(self):
        for text in ['我是AI驱动的虚拟角色','抱歉，刚才数字算错了','先按住伤口止血并尽快求助','这段Python的错误在第二行']:
            self.assertEqual(issues(text),[])
        self.assertEqual(issues('他说“喵”，我没这么说',state={'self_banned_phrases':['喵']}),[])
        self.assertTrue(issues('喵',state={'self_banned_phrases':['喵']}))
        self.assertTrue(issues('Traceback (most recent call last):'))
        self.assertTrue(issues('{reply}'))
        exact=verbatim_request('原样复述：“A。🙂”');self.assertEqual(exact,'A。🙂')
        self.assertEqual(normalize('错稿',{'verbatim_text':exact}),exact)
        self.assertEqual(normalize('他说“好。”，我说好。'),'他说“好。”，我说好')
