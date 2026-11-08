import asyncio,json,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from test_round8_visual import Event
import test_round13_images as base
from data.plugins.astrbot_plugin_proactive_agent import image_spec
from data.plugins.astrbot_plugin_proactive_agent.image_followup import continuation,reset_reference
from data.plugins.astrbot_plugin_proactive_agent.image_generation import install,generate

def user(text,sender='one'):
    return {'role':'user','content':'[群聊发言身份] '+json.dumps({'QQ':sender})+'\n'+text}

def pending():
    return [user('给我拍张穿卫衣拿红苹果的自拍'),{'role':'assistant','content':'可以换成浅蓝色卫衣，保留红苹果和自拍视角，按这个方案拍吗？'}]

def request(history=None,images=None):
    return SimpleNamespace(contexts=history if history is not None else pending(),image_urls=images or [],extra_user_content_parts=[])

class Continuation(unittest.TestCase):
    def test_refusal_diagnostics_are_finite_and_do_not_leak_provider_prose(self):
        from data.plugins.astrbot_plugin_proactive_agent.image_transport import rejection_details
        result=rejection_details(SimpleNamespace(body={'error':{'code':'moderation_blocked','message':'private secret',
            'moderation_details':{'moderation_stage':'input','categories':['sexual','private secret',{},'sexual']}}}))
        self.assertEqual(result,{'provider_code':'moderation_blocked','moderation_stage':'input','moderation_categories':['sexual']})
    def test_short_confirmations_are_admitted_only_for_unfinished_visual_topic(self):
        for text in ('好就这么着','好，就这么办','按你说的来','行','可以','那换成这个','换成浅紫色','第二个','就这样吧','开始吧'):
            self.assertTrue(continuation(Event(text),request()),text)
            self.assertFalse(continuation(Event(text),request([user('晚饭吃面？'),{'role':'assistant','content':'可以，吃面吧'}])),text)
    def test_cancellation_refusal_completed_work_and_other_speaker_not_resumed(self):
        for text in ('不用了','先不拍','算了','等会再说','为什么这么做','明天吧','那换个话题'):
            self.assertFalse(continuation(Event(text),request()),text)
        for reply in ('本龙刚拍的自拍，收好','图片服务的内容审核拒绝了这次请求，未返回图片','这次画面请求被图片服务拒绝了，请换一种合适的画面要求'):
            self.assertFalse(continuation(Event('好就这么着'),request([user('拍张自拍'),{'role':'assistant','content':reply}])),reply)
        self.assertFalse(continuation(Event('行',sender='other'),request()))
        self.assertFalse(continuation(Event('行'),request(pending()+[user('我的方案呢','other'),{'role':'assistant','content':'可以拍一张照片'}])))
        self.assertFalse(continuation(Event('行'),request(pending()+[user('换个话题'),{'role':'assistant','content':'可以啊'}])))
    def test_incorrect_tool_denial_is_still_a_pending_request(self):
        req=request([user('拍一张穿卫衣的自拍'),{'role':'assistant','content':'这张自拍暂时没有可用的生图工具'}])
        self.assertTrue(continuation(Event('那就按这个来'),req))
    def test_quoted_approval_cannot_resume_others_request(self):
        from astrbot.api.message_components import Reply
        self.assertFalse(continuation(Event('好',extra=[Reply(id='another')]),request()))
    def test_bounded_context_keeps_proposal_but_not_another_actors_proposal(self):
        history=[user('拍一张猫','other'),{'role':'assistant','content':'其他人的秘密方案'},*pending()]
        event=Event('好就这么着');event.set_extra('qqbot_image_projected_context',history)
        material=image_spec.context_material(event,request(history))
        data=json.dumps(material,ensure_ascii=False)
        self.assertIn('浅蓝色卫衣',data);self.assertNotIn('其他人的秘密方案',data)
        material=image_spec.context_material(Event('好就这么着'),request())
        self.assertIn('浅蓝色卫衣',json.dumps(material,ensure_ascii=False))
    def test_fresh_and_explicitly_reset_requests_do_not_inherit_old_visual_content(self):
        old=[user('画猫'),{'role':'assistant','content':'OLD_VISUAL_DETAIL'},user('那张图')]
        for text in ('画一只狗','不用参考上一张图，拍张你穿蓝色卫衣的自拍','不要用那张照片，画一只猫','只按文字描述画一张猫'):
            event=Event(text);event.set_extra('qqbot_image_projected_context',old)
            material=image_spec.context_material(event,request(old))
            self.assertEqual(material['recent'],[],text)
            if reset_reference(text):self.assertEqual(image_spec.reference_paths(event,request(old,['old.png'])),[],text)
        self.assertFalse(reset_reference('按照这张图拍同款自拍'))
    def test_reference_content_is_not_silently_removed_to_retry_a_refusal(self):
        event=Event('看看你这样的照片')
        self.assertEqual(image_spec.reference_paths(event,request(images=['reference.png'])),['reference.png'])

class Routing(unittest.IsolatedAsyncioTestCase):
    setup_flow=base.Flow.setup_flow
    async def test_confirmation_runs_existing_planner_once_and_preserves_attachment(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        self.setup_flow([base.answer(base.picture())],[base.PASS],{'image_generation_api_enabled':True})
        event=Event('好就这么着');event.set_extra('qqbot_generation_request',request())
        with patch.object(Provider,'_query',AsyncMock()) as ordinary:
            restore=install(self.plugin);token=current_job.set(SimpleNamespace(event=event))
            try:
                a,b=await asyncio.gather(Provider._query(self.provider,{'messages':pending()}),Provider._query(self.provider,{'messages':pending()}))
                self.assertIs(a,b);self.assertTrue(a.qqbot_generated_image);ordinary.assert_not_awaited()
                self.assertEqual(self.create.await_count,1);self.assertEqual(self.provider.text_chat.await_count,2)
                payload=json.loads(self.provider.text_chat.call_args_list[0].kwargs['prompt'])
                self.assertEqual(payload['request'],'好就这么着');self.assertIn('浅蓝色',json.dumps(payload,ensure_ascii=False))
            finally:current_job.reset(token);restore()
    async def test_non_creation_semantic_verdict_returns_to_chat_without_images(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        self.setup_flow([],[],{'image_generation_api_enabled':True});self.provider.text_chat.side_effect=[base.llm({'intent':'none'})]
        event=Event('可以');event.set_extra('qqbot_generation_request',request())
        with patch.object(Provider,'_query',AsyncMock(return_value='ordinary')) as ordinary:
            restore=install(self.plugin);token=current_job.set(SimpleNamespace(event=event))
            try:
                self.assertEqual(await Provider._query(self.provider,{'messages':pending()}),'ordinary')
                self.create.assert_not_awaited();ordinary.assert_awaited_once()
            finally:current_job.reset(token);restore()
    async def test_ordinary_acknowledgement_has_no_planner_or_generation(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        self.setup_flow([],[],{'image_generation_api_enabled':True})
        event=Event('好');event.set_extra('qqbot_generation_request',request([user('吃面吧'),{'role':'assistant','content':'行啊'}]))
        with patch.object(Provider,'_query',AsyncMock(return_value='ordinary')) as ordinary:
            restore=install(self.plugin);token=current_job.set(SimpleNamespace(event=event))
            try:
                self.assertEqual(await Provider._query(self.provider,{}),'ordinary')
                self.provider.text_chat.assert_not_awaited();self.create.assert_not_awaited()
            finally:current_job.reset(token);restore()
    async def test_explicit_reset_ignores_previous_reference_without_waiting(self):
        self.setup_flow([base.answer(base.picture())],[base.PASS])
        event=Event('不用参考上一张图，拍张你穿蓝色卫衣的自拍')
        event.set_extra('qqbot_generation_request',request(images=['unreadable-old.png']))
        self.plugin._incoming_visual=SimpleNamespace(wait_reference=AsyncMock(side_effect=AssertionError('no wait expected')),cache=None)
        await generate(self.plugin,self.provider,event)
        self.edit.assert_not_awaited();self.create.assert_awaited_once()
    async def test_content_refusal_never_falls_back_or_drops_references(self):
        import httpx
        from openai import BadRequestError
        response=httpx.Response(400,request=httpx.Request('POST','http://example.invalid/images'))
        refusal=BadRequestError('blocked',response=response,body={'code':'content_policy_violation'})
        self.setup_flow([refusal],[])
        with self.assertRaises(BadRequestError):await generate(self.plugin,self.provider,Event('拍张自拍'))
        self.create.assert_awaited_once()
