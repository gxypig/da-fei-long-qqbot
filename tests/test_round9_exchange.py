import unittest
from types import SimpleNamespace
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.exchange_state import Exchanges,protected
from data.plugins.astrbot_plugin_proactive_agent.persona_projection import project,binding_projection,dragon_identity
from data.plugins.astrbot_plugin_proactive_agent.persona_authority import role_text

class Event:
    def __init__(self,text='这逻辑没毛病',origin='p:GroupMessage:10',sender='1',mid='1'):
        self.text=text;self.unified_msg_origin=origin;self.sender=sender;self.extra={};self.message_obj=SimpleNamespace(message_id=mid)
    def get_extra(self,key):return self.extra.get(key)
    def set_extra(self,key,value):self.extra[key]=value
    def get_sender_id(self):return self.sender
    def get_message_str(self):return self.text
    def get_messages(self):return []
    def stop_event(self):self.stopped=True

class ExchangeTests(unittest.TestCase):
    def test_optional_failure_never_swallows_explicit_help_or_manual_task(self):
        from data.plugins.astrbot_plugin_proactive_agent.failure_policy import quiet_failure
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyJob
        job=ReplyJob('p:GroupMessage:10','1','1','哈哈这也行',passive=True)
        self.assertTrue(quiet_failure(job));job.text='电脑蓝屏了怎么办'
        self.assertFalse(quiet_failure(job));job.text='哈哈这也行';job.manually_adjusted=True
        self.assertFalse(quiet_failure(job));job.manually_adjusted=False;job.passive=False
        self.assertFalse(quiet_failure(job));job.passive=True;job.origin='p:FriendMessage:1'
        self.assertFalse(quiet_failure(job));self.assertFalse(quiet_failure(None,{'proactive':True,'required':True}))

    def test_receipts_segments_unknown_and_private_isolation(self):
        service=Exchanges(clock=lambda:100)
        base=dict(origin='p:GroupMessage:10',sender='1',purpose='answer',job_id='job',exchange={'progress':'same','topic':'玩笑'})
        service.confirmed({**base,'id':'a','state':'unknown','text':'还'})
        self.assertEqual(service.view(base['origin'],'1')['rounds_5m'],0)
        service.confirmed({**base,'id':'a','state':'confirmed','text':'还'})
        service.confirmed({**base,'id':'b','state':'confirmed','text':'行'})
        service.confirmed({**base,'id':'b','state':'confirmed','text':'行'})
        view=service.view(base['origin'],'1');self.assertEqual(view['rounds_5m'],1);self.assertEqual(view['confirmed_replies'][0]['text'],'还行')
        self.assertEqual(service.view(base['origin'],'2')['rounds_5m'],0)
        self.assertEqual(service.view('p:GroupMessage:11','1')['rounds_5m'],0)
    def test_six_rounds_repeated_at_cool_new_task_preserved(self):
        service=Exchanges(clock=lambda:100);event=Event()
        for i in range(6):
            e=Event(mid=str(i));service.observe(e)
            service.confirmed(dict(origin=e.unified_msg_origin,sender='1',purpose='answer',id=str(i),job_id=str(i),state='confirmed',text='确实这逻辑没毛病',exchange={'progress':'same','topic':'玩笑'}))
        job=lambda e:SimpleNamespace(event=e,origin=e.unified_msg_origin,sender=e.sender,text=e.text)
        self.assertIsNotNone(service.before(job(Event('@123 这逻辑没毛病'))))
        for text in ['帮我查一下天气','继续说吧','为什么会乳化','我电脑蓝屏了','你是什么模型','在吗','看看你','来张表情包','告诉我你上次说了啥']:
            self.assertIsNone(service.before(job(Event(text))))
        self.assertIsNone(service.before(job(Event('今天突然收到录取通知了'))))
        self.assertFalse(service.optional_image_allowed(event)) if event.get_extra('qqbot_exchange') else None
    def test_control_line_never_reaches_text_and_protected_not_skipped(self):
        service=Exchanges();e=Event('帮我解释一下');state={};prompt=service.prepare(e,state);nonce=e.get_extra('qqbot_exchange_nonce')
        text,decision=service.parse(e,f'[[EXCHANGE:{nonce}:skip:same:旧话题]]\n先说哪个点')
        self.assertEqual(text,'先说哪个点');self.assertEqual(decision['action'],'normal')
        text,_=service.parse(e,'[[EXCHANGE:broken:normal:new:话题]]\n正文');self.assertEqual(text,'正文')
        text,decision=service.parse(e,'[[S]]在呢');self.assertEqual(text,'在呢');self.assertEqual(decision['progress'],'same')
        text,decision=service.parse(e,'在呢[[S]]');self.assertEqual(text,'在呢');self.assertEqual(decision['progress'],'same')
        text,decision=service.parse(e,'[[S]]在呢[[S]]');self.assertEqual(text,'在呢');self.assertEqual(decision['progress'],'same')
        text,decision=service.parse(e,'[[K]]');self.assertEqual(text,'');self.assertTrue(decision['invalid_skip'])
        text,decision=service.parse(e,'新的回答');self.assertEqual(decision['progress'],'new')
        self.assertNotIn('recent_messages',state['exchange']);self.assertLess(len(prompt),1400)
    def test_cooldown_outlives_rolling_count_but_not_new_short_statements(self):
        now=[100];service=Exchanges(clock=lambda:now[0])
        for i in range(6):
            now[0]=100+50*i;e=Event('@123456789 这逻辑没毛病',mid=str(i));service.observe(e)
            service.confirmed(dict(origin=e.unified_msg_origin,sender='1',purpose='answer',id=str(i),job_id=str(i),state='confirmed',text='确实这逻辑没毛病',exchange={'progress':'same'}))
        now[0]=401
        def job(text):
            e=Event(text,mid=text);return SimpleNamespace(event=e,origin=e.unified_msg_origin,sender=e.sender,text=e.text)
        self.assertEqual(service.view(e.unified_msg_origin,'1')['rounds_5m'],5)
        repeated=job('[CQ:at,qq=123456789]这逻辑没毛病')
        self.assertIsNotNone(service.before(repeated))
        state={};prompt=service.prepare(repeated.event,state)
        self.assertTrue(state['exchange']['cooling_down']);self.assertIn('冷却尚未结束',prompt)
        for text in ['@123456789 我失恋了','@123456789 今晚吃火锅','@123456789 该有个表情包了吧','帮我看看这题']:
            self.assertIsNone(service.before(job(text)),text)
        state={};prompt=service.prepare(Event('帮我看看这题'),state)
        self.assertIn('这次必须给出回答正文',prompt)
        service.observe(Event('@123456789 [附件]我真的好想吃火锅[附件]',mid='positive'))
        self.assertIsNone(service.before(job('@123456789 [附件]我真的不想吃火锅[附件]')))
        self.assertIsNotNone(service.before(job('@123456789 我真的不想吃火锅')))
        now[0]=471
        self.assertIsNone(service.before(job('@123456789 这逻辑没毛病')))
        state={};prompt=service.prepare(Event(),state)
        self.assertNotIn('cooling_down',state['exchange']);self.assertNotIn('冷却尚未结束',prompt)
    def test_limits_topic_reset_and_time(self):
        now=[100];service=Exchanges(clock=lambda:now[0])
        for i in range(300):service.observe(Event(sender=str(i)))
        self.assertEqual(len(service.rows),256)
        for i in range(40):service.observe(Event(mid=str(i)))
        self.assertEqual(len(service.view(Event().unified_msg_origin,'1')['recent_messages']),20)
        service.confirmed(dict(origin=Event().unified_msg_origin,sender='1',id='x',state='confirmed',text='新的问题',exchange={'progress':'new'}))
        self.assertEqual(service.view(Event().unified_msg_origin,'1')['rounds_5m'],0)
    def test_projection_has_complete_mapping_unknown_sections_preserved(self):
        master='前言\n【日常性格与口吻】\n自然\n【QQ视频分享文案场景】\n视频具体钩子\n【未知约定】\n必须保留'
        views,mapping=project(master)
        self.assertEqual(''.join(master[m['start']:m['end']] for m in mapping),master)
        self.assertNotIn('视频具体钩子',views['chat']);self.assertIn('视频具体钩子',views['bili'])
        for v in views.values():self.assertIn('必须保留',v)
        binding=binding_projection(master,{'chat':'补充'})
        self.assertTrue(role_text('chat',binding).endswith('补充'))
    def test_identity_scoped_to_current_role_and_actual_question(self):
        for text in ['你是什么模型','你是AI吗','你到底是哪个模型','你是不是GPT','你是deepseek吗']:
            state={'persona_prompt':'白毛龙娘'};self.assertTrue(dragon_identity(text,state));self.assertTrue(state['identity_scene'])
        for text in ['他是什么模型','“你是什么模型”这句话','哪个模型好用','你觉得GPT好用吗']:
            self.assertFalse(dragon_identity(text,{'persona_prompt':'白毛龙娘'}))
        self.assertFalse(dragon_identity('你是什么模型',{'persona_prompt':'严谨助手'}))
        self.assertFalse(dragon_identity('你是什么模型',{}))

class HookTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_fault_optional_interjection_quiet_but_help_not_silent(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import AsyncMock,Mock,patch
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyJob,ReplyQueue,current_job
        from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import DeliveryLedger
        from data.plugins.astrbot_plugin_proactive_agent.fullchain import install_fullchain
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
        with tempfile.TemporaryDirectory() as folder:
            ledger=DeliveryLedger(Path(folder)/'ledger.db');await ledger.start();queue=ReplyQueue(lambda k,d=None:d)
            p=SimpleNamespace(_cfg=lambda k,d=None:d,_reply_queue=queue,_delivery=ledger,_audit=Mock(),context=SimpleNamespace(conversation_manager=None))
            original=AsyncMock()
            with patch.object(InternalAgentSubStage,'_send_llm_error_message',original):
                restore=install_fullchain(p);e=Event('哈哈这也行');job=ReplyJob(e.unified_msg_origin,e.sender,'1',e.text,event=e,passive=True);token=current_job.set(job)
                try:
                    await InternalAgentSubStage._send_llm_error_message(None,e,'synthetic model failure')
                    original.assert_not_awaited();self.assertEqual(job.execution_result,'failed')
                    job.text='电脑蓝屏了怎么办'
                    await InternalAgentSubStage._send_llm_error_message(None,e,'synthetic model failure')
                    self.assertEqual(original.await_count,1)
                    e.set_extra('qqbot_gallery_result','found')
                    ident=ledger.begin(job.origin,None,'',[{'kind':'image'}],job);ledger.result(ident,'unknown')
                    self.assertEqual(e.get_extra('qqbot_gallery_result'),'found')
                    other=ledger.begin(job.origin,None,'',[{'kind':'image'}],job);ledger.result(other,'failed',reason='explicit_failure')
                    self.assertEqual(e.get_extra('qqbot_gallery_result'),'send_failed');self.assertEqual(ledger.store.rows[other]['gallery_result'],'send_failed')
                finally:current_job.reset(token);restore();await ledger.close()

    async def test_protocol_removed_before_voice_review_and_native_history(self):
        from unittest.mock import AsyncMock,Mock
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Plain
        from astrbot.core.agent.message import Message,TextPart
        from data.plugins.astrbot_plugin_proactive_agent.conversation_review import bind_request
        p=Main.__new__(Main);p._cfg=lambda k,d=None:d;p._audit=Mock();p.drop_stale_reply=AsyncMock(return_value=False)
        p._exchanges=Exchanges();p.character_context=lambda e:{};p.review_mode=lambda o:'chat';p.review_settings=lambda *a:{'enabled':False,'mode':'off'}
        p.repair_local_output=AsyncMock(return_value='这里是需要处理的新问题')
        for user,action,progress,body in [('你好','normal','new','在呢。'),('这逻辑没毛病','skip','same',''),('帮我解释一下','skip','same','')]:
            e=Event(user);state={};p._exchanges.prepare(e,state);nonce=e.get_extra('qqbot_exchange_nonce')
            raw=f'[[EXCHANGE:{nonce}:{action}:{progress}:话题]]\n'+body
            response=SimpleNamespace(completion_text=raw,tools_call_name=[],result_chain=MessageChain([Plain(raw)]),role='assistant')
            context=SimpleNamespace(messages=[Message(role='assistant',content=[TextPart(text=raw)])])
            bind_request(e.unified_msg_origin,[],False,{},p.review_settings())
            await p.prepare_chat_punctuation(e,context,response)
            self.assertNotIn('EXCHANGE:',response.completion_text)
            if user=='这逻辑没毛病':self.assertTrue(e.stopped);self.assertEqual(response.result_chain.chain,[])
            else:
                self.assertTrue(response.completion_text);self.assertNotIn('EXCHANGE:',context.messages[-1].content[-1].text)
        self.assertEqual(p.repair_local_output.await_count,1)
