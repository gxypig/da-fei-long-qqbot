import asyncio,copy,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer,ReviewUnavailable,bind_request
from data.plugins.astrbot_plugin_proactive_agent.review_contract import goals
from data.plugins.astrbot_plugin_proactive_agent.identity_expression import detect,guard
from data.plugins.astrbot_plugin_proactive_agent.review_history import ReviewHistory
from data.plugins.astrbot_plugin_proactive_agent.ai_profiles import AIProfiles,identity
PASS={'pass':True,'natural':True,'chat_fit':True}
FAIL={**PASS,'pass':False,'natural':False,'reason':'多余的客服尾句','replacement':'不能采用的旧改稿','replacement_check':{**PASS,'facts_preserved':True}}

class IndependentReview(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):bind_request('case',[],False,{}, {'enabled':True,'mode':'all'})
    def reviewer(self,values):
        return ReplyReviewer(AsyncMock(side_effect=[json.dumps(v,ensure_ascii=False) for v in values]),lambda *_:None)
    async def test_first_draft_pass_calls_once(self):
        r=self.reviewer([PASS]);self.assertEqual(await r.review('瞧你得意的',origin='case'),'瞧你得意的');self.assertEqual(r.generate.await_count,1)
    async def test_identity_defect_reaches_judge_not_canned_local_substitute(self):
        state={'persona_status':'selected','persona_prompt':'白毛龙娘','identity_scene':True}
        r=self.reviewer([FAIL,{'reply':'龙骨架，外面裹龙鳞'},PASS])
        self.assertEqual(await r.review('用的是AstrBot框架',origin='case',character=state),'龙骨架，外面裹龙鳞')
        self.assertEqual(r.generate.await_args_list[0].args[1]['待发文字'],'用的是AstrBot框架')
        self.assertEqual(r.generate.await_count,3)
    async def test_self_certificate_cannot_approve_and_writer_does_not_see_old_answer(self):
        r=self.reviewer([FAIL,{'reply':'你说哪个？'},PASS]);state={'is_father':True,'persona_version':'one','current_life':{'current_activity':'整理书'}}
        result=await r.review('独有旧答案',origin='case',character=state)
        self.assertEqual(result,'你说哪个？');self.assertEqual(r.generate.await_count,3)
        writer=r.generate.await_args_list[1].args[1];judge=r.generate.await_args_list[2].args[1]
        self.assertNotIn('独有旧答案',json.dumps(writer,ensure_ascii=False));self.assertNotIn('不能采用的旧改稿',json.dumps(writer,ensure_ascii=False))
        self.assertNotIn('replacement_check',json.dumps(judge));self.assertEqual(judge['可信状态'],writer['可信状态'])
        self.assertEqual(judge['必须修正的问题'],[]);self.assertTrue(writer['可信状态']['is_father'])
    async def test_five_calls_max_and_no_original_fallback(self):
        r=self.reviewer([FAIL,{'reply':'第二稿'},FAIL,{'reply':'第三稿'},FAIL])
        with self.assertRaises(ReviewUnavailable):await r.review('原稿',origin='case')
        self.assertEqual(r.generate.await_count,5)
    async def test_generic_objections_do_not_produce_defensive_rewrite(self):
        r=self.reviewer([{**FAIL,'reason':'更谨慎','problems':[]}])
        with self.assertRaises(ReviewUnavailable):await r.review('自然话',origin='case')
        r.generate.assert_awaited_once()
    async def test_off_zero_calls(self):
        r=self.reviewer([]);r.enabled=lambda:False
        self.assertEqual(await r.review('回复',origin='case'),'回复');r.generate.assert_not_called()
    async def test_plain_writer_requires_next_judge(self):
        r=ReplyReviewer(AsyncMock(side_effect=[json.dumps(FAIL),'现在好了，接着聊',json.dumps(PASS)]),lambda *_:None)
        self.assertEqual(await r.review('客服说明',origin='case'),'现在好了，接着聊')
        self.assertEqual(r.generate.await_count,3)
    async def test_timeout_bounds_queue_and_rewrites(self):
        async def slow(*a,**k):await asyncio.sleep(.1);return json.dumps(PASS)
        r=ReplyReviewer(slow,lambda *_:None,timeout=.015)
        with self.assertRaises(ReviewUnavailable):await r.review('草稿',origin='case')
    async def test_verification_records_are_separate_and_feedback_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            h=ReviewHistory(Path(folder)/'reviews.db');await h.start()
            try:
                r=self.reviewer([FAIL,{'reply':'好呀'},PASS]);r.history=h
                await r.review('旧稿',origin='case',character={'persona_version':'v','persona_prompt':'龙娘'})
                row=next(row for row in h.store.rows.values() if row.get('origin')=='case')
                self.assertEqual(len(row['rounds']),2);self.assertEqual(len(row['rewrites']),1);self.assertTrue(row['independent_review'])
                body={'review_id':row['id'],'action':'worse','request_id':'request-one','feedback_version':0}
                saved=await h.action(body,None);again=await h.action(body,None)
                self.assertEqual(len(again['feedback']),1);self.assertEqual(saved['feedback_version'],1)
                self.assertEqual(again['approved'],'好呀');self.assertEqual(again['sends'],{})
            finally:await h.close()
    def test_goal_filter_locations_and_unrelated_claims(self):
        self.assertEqual(goals({'problems':[{'category':'wrong_response','quote':'不存在','reason':'错','goal':'改正'}]},'自然话'),[])
    def test_raw_provider_failure_cannot_escape_but_diagnostics_and_quotes_can(self):
        from data.plugins.astrbot_plugin_proactive_agent.send_checks import issues
        raw="LLM 响应错误: All chat models failed: BadRequestError: Error code: 400 - {'error': {'type':'invalid_request_error'}}"
        self.assertIn('internal_error_or_control_payload',issues(raw))
        self.assertNotIn('internal_error_or_control_payload',issues(raw,mode='management'))
        self.assertNotIn('internal_error_or_control_payload',issues('“'+raw+'”'))
        self.assertNotIn('internal_error_or_control_payload',issues('图片没生成出来，请求参数不兼容'))
    def test_framework_followup_and_technical_question_separation(self):
        s={'persona_status':'selected','persona_prompt':'白毛龙娘'}
        for text in ('你是什么bot','你用的什么bot框架','你的架构是什么','你是什么框架'):
            self.assertTrue(detect(text,s),text)
        for text in ('AstrBot怎么装','那个机器人是什么框架','你知道这个框架怎么用吗'):
            self.assertFalse(detect(text,s),text)
        rows=[{'sender':'a','message_id':'quoted','time':100,'text':'你用什么bot框架'}]
        self.assertTrue(detect('别绕，具体用什么',s,rows,'a',150))
        self.assertFalse(detect('底层呢',s,rows,'b',150))
        self.assertTrue(detect('底层呢',s,rows,'b',150,quoted={'message_id':'quoted'}))
    def test_framework_unknown_tail_is_guarded_only_in_own_identity_scene(self):
        from data.plugins.astrbot_plugin_proactive_agent.identity_expression import violation
        state={'persona_status':'selected','persona_prompt':'白毛龙娘','identity_scene':True}
        self.assertTrue(violation('具体框架没法从当前对话确认，得看部署配置',state))
        self.assertFalse(violation('龙骨架，外面裹龙鳞',state))
        self.assertFalse(violation('具体框架没法从当前对话确认',{**state,'identity_scene':False}))

class AccountSwitch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.cfg={'ai_profiles_enabled':True,'ai_probe_enabled':True}
        self.s=AIProfiles(Path(self.temp.name)/'ai.db',self.cfg.get,busy=lambda:True);await self.s.start()
        self.key=identity('qq','test');self.s.observe('qq','test','tool_calls: []','qq:GroupMessage:g','a')
    async def asyncTearDown(self):await self.s.close();self.temp.cleanup()
    async def test_disable_invalidates_running_analysis_even_if_enabled_again(self):
        entered=asyncio.Event();gate=asyncio.Event()
        async def generate(*_):entered.set();await gate.wait();return json.dumps({'judgement_type':'ai','evidence':[],'counterevidence':[]})
        self.s.generate=generate;task=asyncio.create_task(self.s.analyze(self.key,0));await entered.wait()
        self.cfg['ai_profiles_enabled']=False;self.s.config_changed();self.cfg['ai_profiles_enabled']=True;self.s.config_changed();gate.set();await task
        self.assertIsNone(self.s.store.rows[self.key].get('analyzed_at'))
    async def test_disabled_no_observation_probe_or_label_and_history_retained(self):
        self.cfg['ai_profiles_enabled']=False;self.s.config_changed();before=copy.deepcopy(self.s.store.rows[self.key])
        self.s.observe('qq','test','hello','qq:GroupMessage:g','b')
        self.assertEqual(self.s.store.rows[self.key],before);self.assertEqual(self.s.reply_note('qq','test'),'')
        self.assertIsNone(await self.s.prepare_probe('qq','test','b','qq:GroupMessage:g'))
        self.assertEqual(self.s.snapshot()['pending'],0);self.assertTrue(self.s.snapshot()['probe_enabled'])
        self.assertFalse(self.s.snapshot()['enabled'])
    async def test_disabled_manual_reanalyze_rejected(self):
        self.cfg['ai_profiles_enabled']=False;self.s.config_changed()
        with self.assertRaises(ValueError):self.s.action({'action':'reanalyze','platform':'qq','account':'test','instance':self.s.instance,'version':self.s.version,'request_id':'request-test'})
