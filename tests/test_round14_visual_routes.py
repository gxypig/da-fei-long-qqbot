import asyncio,base64,copy,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from test_round8_visual import Event
import test_round13_images as base
from test_round13_images import picture,answer,llm,PLAN,PASS
from data.plugins.astrbot_plugin_proactive_agent.image_generation import generate,install
from data.plugins.astrbot_plugin_proactive_agent.visual_intent import generation_kind,media_candidate
from data.plugins.astrbot_plugin_proactive_agent import image_spec

ANIMATION_REQUESTS=['给我拍个视频','录一段你挥手的小视频','给本宫拍一个你眨眼的短片','让我看看会动的自拍','把这张自拍变成动态的','录个你拿苹果的','可以给我拍几秒你眨眼吗','给我整一个你挥手的gif','想看你动起来，拍一段呗','来段你的小视频','拍个视频给我，内容是你挥手','帮我生成你眨眼的视频','不要照片，给我录一段视频','帮我拍一小段你挥手的视频','record a short video of you waving']
STATIC_REQUESTS=['来张你的自拍','拍一张照片','看看你','生成你做出这个图片相同的动作','不要动图，画一张你拿苹果的图片','给我来张猫的照片']
NON_CREATION=['给我找个B站视频','把刚才的视频转发给我','如何生成动图','gif是什么','不要拍视频','生成一段代码','录一段语音','帮我写画图脚本','拍一拍我','拍桌子','帮我剪辑这段视频','你会拍视频吗','给我下载这个视频','打开摄像头看看','分析这张图片','生成提示词']

class Candidates(unittest.TestCase):
    def test_media_requests_reach_model_but_ordinary_actions_do_not(self):
        for text in ANIMATION_REQUESTS+STATIC_REQUESTS:
            self.assertNotEqual(generation_kind(text),'none',text)
        for text in NON_CREATION:
            self.assertEqual(generation_kind(text),'none',text)
        for text in ('让这张照片动起来','把这张图动一动','给我发个你挥手的小视频'):
            self.assertNotEqual(generation_kind(text),'none',text)
        self.assertEqual(generation_kind('来张开心的图片'),'none')
    def test_reference_pose_can_request_full_body_without_forcing_selfie(self):
        plan=image_spec.normalize_plan({**PLAN,'camera_mode':'third_person'}, {'request':'生成你做出这个图片相同的动作'},None)
        self.assertEqual(plan['camera_mode'],'third_person')
        plan=image_spec.normalize_plan({**PLAN,'camera_mode':'third_person'}, {'request':'自拍做这个动作'},None)
        self.assertEqual(plan['camera_mode'],'selfie')
    def test_missing_intent_cannot_silently_become_static(self):
        data={k:v for k,v in PLAN.items() if k!='intent'}
        with self.assertRaisesRegex(ValueError,'image_intent_invalid'):image_spec.normalize_plan(data,{'request':'给我拍个视频'},None)

class Routes(unittest.IsolatedAsyncioTestCase):
    setup_flow=base.Flow.setup_flow
    async def test_image_only_resumes_only_the_same_speakers_missing_reference_request(self):
        request='生成你做出这个图片相同的动作'
        req=SimpleNamespace(image_urls=['current.png'],contexts=[
            {'role':'user','content':[{'type':'text','text':'[群聊发言身份] {"QQ":"one"}\n'+request}]},
            {'role':'assistant','content':'这张动作参考图现在读不到了，再发一下原图，本龙照着拍'}])
        event=Event('')
        self.assertEqual(image_spec.followup_request(event,req),request)
        self.assertIsNone(image_spec.followup_request(Event('',sender='other'),req))
        self.assertIsNone(image_spec.followup_request(Event('不用画了'),req))
        req.contexts.append({'role':'user','content':'换个话题'})
        self.assertIsNone(image_spec.followup_request(event,req))
    async def test_factual_error_survives_both_review_hooks_without_model_or_tts(self):
        from test_reply_pipeline import plugin,event
        from astrbot.core.agent.message import Message,TextPart
        from astrbot.core.provider.entities import LLMResponse
        p=plugin();e=event('生成你做出这个图片相同的动作')
        text='这张动作参考图现在读不到了，再发一下原图，本龙照着拍'
        r=LLMResponse(role='assistant',completion_text=text);r.qqbot_status_receipt=text
        history=SimpleNamespace(messages=[Message(role='assistant',content=[TextPart(text=text)])])
        p.repair_local_output=AsyncMock(side_effect=AssertionError('receipt rewritten'))
        await p.prepare_chat_punctuation(e,history,r);await p.review_agent_reply(e,history,r)
        self.assertEqual(r.completion_text,text);self.assertEqual(history.messages[-1].content[-1].text,text)
        self.assertEqual(e.get_extra('qqbot_send_path'),'template_receipt');self.assertTrue(e.get_extra('qqbot_agent_final_done'))
        p._reviewer.review.assert_not_awaited();p.repair_local_output.assert_not_awaited()
    async def test_nine_second_pose_followup_does_not_depend_on_history_commit(self):
        from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import IncomingVisual
        from astrbot.api.message_components import Image
        from astrbot.core.provider.entities import ProviderRequest
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'pose.png';p.write_bytes(picture());now=[100.]
            self.setup_flow([answer(picture())],[PASS]);self.cfg['incoming_visual_burst_ms']=0
            service=IncomingVisual(self.plugin,lambda:now[0]);self.plugin._incoming_visual=service
            component=Image.fromFileSystem(str(p));first=Event(images=[component],mid='first');service.observe(first);service.captured(first,component,str(p))
            now[0]+=9;event=Event('生成你做出这个图片相同的动作',mid='second');req=ProviderRequest(prompt=event.text,image_urls=[],contexts=[])
            await service.enrich(event,req);self.assertEqual(req.image_urls,[])
            event.set_extra('qqbot_generation_request',req)
            await generate(self.plugin,self.provider,event)
            self.assertEqual(len(self.provider.text_chat.call_args_list[0].kwargs['image_urls']),1);self.edit.assert_awaited_once()
            self.assertEqual(image_spec.reference_paths(Event(event.text,sender='other'),req,self.plugin),[])
            service.observe(Event('换个话题',mid='break'));now[0]+=1;next_=Event(event.text,mid='third');service.observe(next_)
            req.contexts=[{'role':'user','content':[{'type':'text','text':'[群聊发言身份] {"QQ":"one"}'},{'type':'image_url','image_url':{'url':str(p)}}]}]
            self.assertEqual(image_spec.reference_paths(next_,req,self.plugin),[])
    async def test_inactive_image_hook_is_fully_transparent(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        self.setup_flow([],[],{'image_generation_api_enabled':True});event=Event('刚才拍个视频');event.set_extra('qqbot_visual_route','none')
        token=current_job.set(SimpleNamespace(event=event))
        with patch.object(Provider,'_query',AsyncMock(return_value='normal')) as old:
            restore=install(self.plugin);retained=Provider._query;restore()
            try:
                payload={'messages':[{'role':'user','content':'hello'}]};await retained(self.provider,payload)
                self.assertIs(old.call_args.args[1],payload);self.provider.text_chat.assert_not_awaited()
            finally:current_job.reset(token)
    async def test_identity_and_pose_have_distinct_roles_at_each_boundary(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'visual-identity';folder.mkdir();p=folder/'canon.png';p.write_bytes(picture())
            (folder/'profile.json').write_text(json.dumps({'reference':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'description':'成年白发龙娘'}),'utf-8')
            self.setup_flow([answer(picture())],[PASS],{'image_identity_enabled':True});self.plugin._data_dir=root
            self.provider.text_chat.side_effect=[llm({**PLAN,'intent':'image','camera_mode':'third_person'}),llm(PASS)]
            event=Event('生成你做出这个图片相同的动作');event.set_extra('qqbot_generation_request',SimpleNamespace(image_urls=[str(p)],contexts=[]))
            await generate(self.plugin,self.provider,event)
            first,last=self.provider.text_chat.call_args_list
            self.assertEqual(json.loads(first.kwargs['prompt'])['reference_layout'],[{'index':1,'role':'identity'},{'index':2,'role':'user_reference'}])
            self.assertEqual(json.loads(last.kwargs['prompt'])['reference_layout'],[{'index':2,'role':'identity'},{'index':3,'role':'user_reference'}])
            self.assertEqual(len(self.edit.call_args.kwargs['image']),2)
    async def test_stream_declined_route_normalizes_fallback_without_rendering(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        seen=[]
        async def old(provider,payloads,**kwargs):seen.append(payloads);yield 'normal'
        self.setup_flow([],[],{'image_generation_api_enabled':True});self.provider.text_chat.side_effect=[llm({'intent':'none'})]
        event=Event('你昨天拍个视频给别人了吗');token=current_job.set(SimpleNamespace(event=event))
        with patch.object(Provider,'_query_stream',old):
            restore=install(self.plugin)
            try:
                payload={'messages':[{'role':'user','content':event.text}]}
                for _ in range(2):self.assertEqual([p async for p in Provider._query_stream(self.provider,payload)],['normal'])
                self.assertTrue(all('没有要求现在创作' in s['messages'][0]['content'] for s in seen));self.assertEqual(len(payload['messages']),1)
                self.provider.text_chat.assert_awaited_once();self.create.assert_not_awaited()
            finally:restore();current_job.reset(token)
    async def test_disabling_review_does_not_disable_semantic_intent(self):
        self.setup_flow([answer(picture(True))],[],{'image_quality_check_enabled':False})
        self.provider.text_chat.side_effect=[llm({**PLAN,'intent':'animation'})]
        self.plugin._image_gif_unavailable={(id(self.provider),'gpt-image-2.6-sunburst'):float('inf')}
        event=Event('给我拍个视频');await generate(self.plugin,self.provider,event)
        self.assertEqual(event.get_extra('qqbot_generated_format'),'gif');self.provider.text_chat.assert_awaited_once()
    async def test_semantic_type_controls_model_and_success_caption(self):
        self.setup_flow([answer(picture(True))],[PASS],{'image_generation_model':'static','image_animation_model':'animation'})
        self.provider.text_chat.side_effect=[llm({**PLAN,'intent':'animation','caption':'本龙刚录的，接着'}),llm(PASS)]
        self.plugin._image_gif_unavailable={(id(self.provider),'animation'):float('inf')}
        event=Event('给我拍个你眨眼的视频');result=await generate(self.plugin,self.provider,event)
        self.assertEqual(self.create.call_args.kwargs['model'],'animation')
        self.assertEqual(event.get_extra('qqbot_generated_format'),'gif')
        self.assertEqual(result.completion_text,'本龙刚录的，接着')
    async def test_model_can_choose_static_despite_video_in_old_context(self):
        self.setup_flow([answer(picture())],[PASS])
        self.provider.text_chat.side_effect=[llm({**PLAN,'intent':'image'}),llm(PASS)]
        event=Event('不要动图，拍一张静态自拍');await generate(self.plugin,self.provider,event)
        self.assertEqual(event.get_extra('qqbot_generated_format'),'png')
    async def test_static_caption_uses_persona_planner_not_technical_fallback(self):
        self.setup_flow([answer(picture())],[PASS])
        self.provider.text_chat.side_effect=[llm({**PLAN,'intent':'image','caption':'本龙拍的，接着'}),llm(PASS)]
        event=Event('画一张你张开双手的图片')
        event.set_extra('qqbot_character',{'persona_status':'selected','persona_fallbacks':{'image_scene':'这张图生成好了'}})
        result=await generate(self.plugin,self.provider,event)
        self.assertEqual(result.completion_text,'本龙拍的，接着')
    async def test_non_creation_passthrough_is_cached_even_when_calls_overlap(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        self.setup_flow([],[],{'image_generation_api_enabled':True})
        self.provider.text_chat.side_effect=[llm({'intent':'none'})]
        event=Event('听说你昨天拍个视频给别人了');token=current_job.set(SimpleNamespace(event=event))
        with patch.object(Provider,'_query',AsyncMock(return_value='normal')) as old:
            restore=install(self.plugin)
            try:
                self.assertEqual(await asyncio.gather(Provider._query(self.provider),Provider._query(self.provider)),['normal','normal'])
                self.provider.text_chat.assert_awaited_once();self.create.assert_not_awaited();self.assertEqual(old.await_count,2)
            finally:restore();current_job.reset(token)
    async def test_pose_reference_reaches_planner_renderer_and_verifier_for_both_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'pose.png';p.write_bytes(picture())
            for sender in ('owner','other_user'):
                self.setup_flow([answer(picture())],[PASS])
                self.provider.text_chat.side_effect=[llm({**PLAN,'intent':'image','camera_mode':'third_person'}),llm(PASS)]
                event=Event('生成你做出这个图片相同的动作',sender=sender,origin='b:FriendMessage:'+sender)
                event.set_extra('qqbot_generation_request',SimpleNamespace(image_urls=[str(p)],contexts=[]))
                await generate(self.plugin,self.provider,event)
                first,last=self.provider.text_chat.call_args_list
                self.assertEqual(len(first.kwargs['image_urls']),1)
                self.assertEqual(len(last.kwargs['image_urls']),2)
                self.assertEqual(first.kwargs['image_urls'][0],last.kwargs['image_urls'][1])
                self.assertEqual(json.loads(last.kwargs['prompt'])['reference_layout'],[{'index':2,'role':'user_reference'}])
                self.edit.assert_awaited_once()
    async def test_missing_reference_does_not_pay_for_futile_redraws(self):
        self.setup_flow([],[]);self.provider.text_chat.side_effect=[llm({'intent':'image','reference_needed':True})]
        with self.assertRaisesRegex(ValueError,'image_reference_missing'):await generate(self.plugin,self.provider,Event('照这张图的动作画你'))
        self.create.assert_not_awaited();self.edit.assert_not_awaited()

class NativeHistory(unittest.IsolatedAsyncioTestCase):
    async def test_interleaved_unload_does_not_access_cancelled_cache(self):
        from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import install as install_visual
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        with tempfile.TemporaryDirectory() as tmp:
            plugins=[SimpleNamespace(_data_dir=Path(tmp)/str(i),_cfg=lambda k,d:d,context=SimpleNamespace()) for i in range(2)]
            restores=[install_visual(p) for p in plugins];provider=Provider.__new__(Provider)
            restores[0]()
            try:self.assertEqual(await provider._transform_content_part({'type':'text','text':'normal'}),{'type':'text','text':'normal'})
            finally:
                restores[1]()
                await asyncio.gather(*(p._incoming_visual.cache.task for p in plugins if p._incoming_visual.cache.task),return_exceptions=True)
            self.assertEqual(await provider._transform_content_part({'type':'text','text':'normal'}),{'type':'text','text':'normal'})
    async def test_native_materialization_expires_old_paths_but_keeps_fresh_images(self):
        from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import install as install_visual
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'fresh.img';p.write_bytes(picture())
            plugin=SimpleNamespace(_data_dir=root,_cfg=lambda k,d:d,context=SimpleNamespace())
            restore=install_visual(plugin);provider=Provider.__new__(Provider)
            try:
                cache=plugin._incoming_visual.cache
                # Wait for normal initialization via one non-image materialization.
                await provider._transform_content_part({'type':'text','text':'hello'})
                path=await cache.retain(p);part={'type':'image_url','image_url':{'url':path}}
                materialized=await provider._materialize_context_image_parts([{'role':'user','content':[part]}])
                self.assertTrue(materialized[0]['content'][0]['image_url']['url'].startswith('data:image/'))
                cache.rows[Path(path).name]['expires_at']=0
                materialized=await provider._materialize_context_image_parts([{'role':'user','content':[part,{'type':'text','text':'保留文字'}]}])
                self.assertEqual([v['type'] for v in materialized[0]['content']],['text','text'])
                self.assertIn('过1小时',materialized[0]['content'][0]['text'])
                self.assertEqual(part['image_url']['url'],path)
                stale=cache.portable_history([{'role':'user','content':[part]}])
                self.assertEqual(stale[0]['content'][0]['type'],'text')
                for bad in ('missing-file.jpg','data:image/png;base64,not-a-valid-image','http://'):
                    result=await provider._transform_content_part({'type':'image_url','image_url':{'url':bad}})
                    self.assertEqual(result['type'],'text',bad)
            finally:
                restore()
                if plugin._incoming_visual.cache.task:await asyncio.gather(plugin._incoming_visual.cache.task,return_exceptions=True)

class ResentCache(unittest.IsolatedAsyncioTestCase):
    async def test_resent_image_renews_compressed_copy_after_expiry_and_restart(self):
        from data.plugins.astrbot_plugin_proactive_agent.incoming_cache import IncomingCache
        from astrbot.core.provider.entities import ProviderRequest
        from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import install
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.png';source.write_bytes(picture());now=[100.]
            event=Event('');req=ProviderRequest(prompt='',image_urls=[str(source)])
            event.set_extra('qqbot_generation_request',req)
            manager=SimpleNamespace(update_conversation=AsyncMock())
            plugin=SimpleNamespace(_data_dir=root,_cfg=lambda k,d:d,context=SimpleNamespace(conversation_manager=manager))
            restore=install(plugin);token=current_job.set(SimpleNamespace(event=event))
            try:
                cache=plugin._incoming_visual.cache;cache.clock=lambda:now[0]
                first=await req.assemble_context();stored=cache.portable_history([first])
                file=Path(next(p['image_url']['url'] for p in stored[0]['content'] if p['type']=='image_url'))
                self.assertTrue(file.exists());now[0]+=3601;await cache.cleanup();self.assertFalse(file.exists())
                await cache.capture_message(first);self.assertFalse(file.exists()) # old save cannot renew
                cache._load() # same bytes after restart, including an expired URI tombstone
                second=await req.assemble_context();self.assertTrue(file.exists())
                await manager.update_conversation(event.unified_msg_origin,'cid',history=[second])
                self.assertTrue(any(p['type']=='image_url' for p in cache.portable_history([second])[0]['content']))
                self.assertEqual(len(next(iter(cache.rows.values()))['uri_keys']),1)
                now[0]+=3601;await cache.cleanup();self.assertFalse(file.exists())
                self.assertFalse(any(p['type']=='image_url' for p in cache.portable_history([second])[0]['content']))
            finally:
                current_job.reset(token);restore()
                if cache.task:await asyncio.gather(cache.task,return_exceptions=True)

class ReusedEvent(unittest.TestCase):
    def test_new_job_does_not_inherit_previous_image_result(self):
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyQueue
        class RealExtras(Event):
            def __init__(self):super().__init__('拍个视频');self._extras={}
            def get_extra(self,k):return self._extras.get(k)
            def set_extra(self,k,v):self._extras[k]=v
        queue=ReplyQueue(lambda k,d:d);first=RealExtras();job=queue.create(first)
        first.set_extra('qqbot_image_api_result',object());first.set_extra('qqbot_visual_route','animation');first.set_extra('qqbot_image_api_cancelled',True)
        copied=copy.copy(first);second=queue.create(copied)
        self.assertNotEqual(second.id,job.id)
        for k in ('qqbot_image_api_result','qqbot_visual_route','qqbot_image_api_cancelled'):self.assertIsNone(copied.get_extra(k));self.assertIsNotNone(first.get_extra(k))
