import asyncio,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
import httpx
from astrbot.api.message_components import Image,Reply
from test_round8_visual import Event
import test_round13_images as base
from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import IncomingVisual
from data.plugins.astrbot_plugin_proactive_agent import image_spec as spec
from data.plugins.astrbot_plugin_proactive_agent.image_generation import generate
from data.plugins.astrbot_plugin_proactive_agent.image_transport import analyze_image,failure

def request(history=(),images=()):return SimpleNamespace(contexts=list(history),image_urls=list(images),extra_user_content_parts=[])
def image_turn():return {'role':'user','content':[{'type':'text','text':'[图片]\n[图片来源] fixture'},{'type':'image_url','image_url':{'url':'new.png'}}]}

class Continuations(unittest.TestCase):
    def test_reference_photo_camera_is_not_forced_into_conflicting_selfie(self):
        for text in ('看看你这样的照片','看看你也这样穿的照片','照着这张图拍你'):
            plan=spec.normalize_plan({**base.PLAN,'camera_mode':'third_person','scene':'第三人称，双手托球'}, {'request':text},None)
            self.assertEqual(plan['camera_mode'],'third_person',text)
            self.assertNotIn('自持前置摄像头',spec.prompt(plan))
        for text in ('看看你','来张自拍','看看你穿这套的自拍'):
            self.assertEqual(spec.camera_mode(text,True,'third_person'),'selfie',text)
    def test_reference_wording_covers_actual_sentence_and_variants(self):
        for text in ('看看你这样的照片','拍个你这样的视频','拍一张你这样穿的自拍','看看你这种照片','来一张同样的照片'):
            self.assertTrue(spec.REFERENCE_CUE.search(text),text)
    def test_timeout_then_new_original_resumes_request_through_image_history(self):
        for notice in ('这次整理画面要求时连接中断，还没拿到可发送的图片','这次整理画面要求超时了，尚未提交生图；可以重新发来本次参考图继续'):
            history=[{'role':'user','content':'看看你这样的照片'},
                     {'role':'assistant','content':'这张动作参考图现在读不到了，再发一下原图'},image_turn(),{'role':'assistant','content':notice}]
            e=Event(origin='p:FriendMessage:a')
            self.assertEqual(spec.followup_request(e,request(history,['new.png'])),'看看你这样的照片')
    def test_refusal_unknown_result_and_intervening_topic_cannot_resume(self):
        e=Event(origin='p:FriendMessage:a');base_history=[{'role':'user','content':'看看你这样的照片'}]
        for notice in ('这次图片服务生成的结果未通过内容审核','这次无法确认服务端是否已完成','图片服务拒绝了这次请求'):
            self.assertIsNone(spec.followup_request(e,request(base_history+[{'role':'assistant','content':notice}],['new.png'])))
        for content in ('取消拍摄','今天吃饭了吗'):
            history=base_history+[{'role':'user','content':content},{'role':'assistant','content':'这次整理画面要求超时'}]
            self.assertIsNone(spec.followup_request(e,request(history,['new.png'])))
    def test_output_refusal_does_not_blame_input(self):
        err=SimpleNamespace(status_code=400,body={'code':'moderation_blocked','moderation_details':{'moderation_stage':'output'}})
        result=failure(err,{'stage':'rendering','submitted':True})
        self.assertIn('生成的结果',result[2]);self.assertNotIn('换一张',result[2])

class References(unittest.IsolatedAsyncioTestCase):
    setup_flow=base.Flow.setup_flow
    async def test_native_normalizer_signals_readiness_and_preserves_cache(self):
        from astrbot.core.pipeline.preprocess_stage.stage import PreProcessStage
        from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import install
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'original.png';path.write_bytes(base.picture())
            release=asyncio.Event();entered=asyncio.Event()
            async def native(stage,e,c):
                entered.set();await release.wait();c.path=c.file=str(path)
            p=SimpleNamespace(_data_dir=Path(tmp)/'data',_cfg=lambda k,d:d,context=SimpleNamespace())
            with patch.object(PreProcessStage,'_normalize_image_component',native):
                restore=install(p);service=p._incoming_visual
                try:
                    c=Image(file='https://example.invalid/image');photo=Event(images=[c],mid='photo')
                    normalizing=asyncio.create_task(PreProcessStage._normalize_image_component(None,photo,c));await entered.wait()
                    row=service.observe(photo);self.assertTrue(row['loading'])
                    e=Event('看看你这样的照片',mid='request');service.observe(e)
                    waiting=asyncio.create_task(service.wait_reference(e,seconds=1));await asyncio.sleep(0)
                    self.assertFalse(waiting.done());release.set();await normalizing
                    paths=await waiting;self.assertEqual(len(paths),1)
                    self.assertTrue(Path(paths[0]).is_file());self.assertTrue(Path(paths[0]).is_relative_to(service.cache.root))
                    self.assertEqual(Path(paths[0]).read_bytes(),base.picture());self.assertFalse(row['loading'])
                finally:
                    release.set();restore()
                    if service.cache.task:await asyncio.gather(service.cache.task,return_exceptions=True)
    async def test_empty_notifications_do_not_evict_original(self):
        p=SimpleNamespace(_cfg=lambda k,d:d,context=SimpleNamespace());service=IncomingVisual(p)
        photo=Event(images=[Image(file='first.png')],mid='photo');service.observe(photo)
        for i in range(20):self.assertIsNone(service.observe(Event(mid='notice'+str(i))))
        e=Event('看看你这样的照片',mid='request');service.observe(e)
        self.assertEqual(len(service.rows[service.key(e)]),2)
        self.assertEqual(spec.reference_paths(e,request(),SimpleNamespace(_incoming_visual=service,_cfg=p._cfg)),['first.png'])
    async def test_original_still_downloading_is_awaited_before_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'photo.png';path.write_bytes(base.picture())
            self.setup_flow([base.answer(base.picture())],[base.PASS]);service=IncomingVisual(self.plugin);self.plugin._incoming_visual=service
            c=Image(file='https://example.invalid/pending');photo=Event(images=[c],mid='photo');row=service.observe(photo);row['loading'].add(id(c))
            event=Event('看看你这样的照片',mid='request');service.observe(event)
            task=asyncio.create_task(generate(self.plugin,self.provider,event))
            for _ in range(1000):
                if row.get('generation_claimed_by'):break
                await asyncio.sleep(.001)
            self.assertEqual(row.get('generation_claimed_by'),'request')
            self.provider.text_chat.assert_not_awaited();self.create.assert_not_awaited()
            row['local'][id(c)]=str(path);row['loading'].clear();row['ready'].set()
            result=await task
            self.assertTrue(result.qqbot_generated_image);self.edit.assert_awaited_once();self.assertEqual(self.create.await_count,0)
            self.assertEqual(row['generation_claimed_by'],'request')
            self.assertEqual(len(self.provider.text_chat.call_args_list[0].kwargs['image_urls']),1)
    async def test_text_before_image_also_waits_for_its_normalizer(self):
        p=SimpleNamespace(_cfg=lambda k,d:d,context=SimpleNamespace());service=IncomingVisual(p)
        event=Event('看看你这样的照片',mid='request');service.observe(event)
        task=asyncio.create_task(service.wait_reference(event,seconds=.3));await asyncio.sleep(0)
        c=Image(file='pending.png');row=service.observe(Event(images=[c],mid='photo'));row['loading'].add(id(c))
        await asyncio.sleep(.02);self.assertFalse(task.done())
        row['local'][id(c)]='ready.png';row['loading'].clear();row['ready'].set()
        self.assertEqual(await task,['ready.png'])
    async def test_observed_remote_image_waits_even_before_normalizer_starts(self):
        p=SimpleNamespace(_cfg=lambda k,d:d,context=SimpleNamespace());service=IncomingVisual(p)
        event=Event('看看你这样的照片',mid='request');service.observe(event)
        task=asyncio.create_task(service.wait_reference(event,seconds=.3));await asyncio.sleep(0)
        c=Image(file='https://example.invalid/remote');row=service.observe(Event(images=[c],mid='photo'))
        await asyncio.sleep(.02);self.assertFalse(task.done())
        row['local'][id(c)]='ready.png';row['ready'].set()
        self.assertEqual(await task,['ready.png'])
    async def test_slow_reference_wait_has_deadline_and_cancel_cleanup(self):
        p=SimpleNamespace(_cfg=lambda k,d:d,context=SimpleNamespace());service=IncomingVisual(p)
        c=Image(file='pending.png');row=service.observe(Event(images=[c],mid='photo'));row['loading'].add(id(c))
        e=Event('看看你这样的照片',mid='request');service.observe(e)
        self.assertEqual(await service.wait_reference(e,seconds=.01),[])
        task=asyncio.create_task(service.wait_reference(e,seconds=30));await asyncio.sleep(0);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(service.reference_waiters,{})
    async def test_foreign_image_quote_and_topic_break_are_not_claimed(self):
        p=SimpleNamespace(_cfg=lambda k,d:d,context=SimpleNamespace());service=IncomingVisual(p)
        service.observe(Event(images=[Image(file='foreign.png')],sender='other',mid='other'))
        event=Event('看看你这样的照片',mid='request');service.observe(event)
        self.assertIsNone(service.preceding_reference(event))
        service.observe(Event(images=[Image(file='own.png')],mid='photo'));service.observe(Event('换个话题',mid='topic'))
        event=Event('看看你这样的照片',mid='request2');service.observe(event)
        self.assertIsNone(service.preceding_reference(event))

class Preparation(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_retries_preparation_once_only(self):
        p=SimpleNamespace(_audit=Mock());provider=SimpleNamespace(text_chat=AsyncMock(side_effect=[httpx.ConnectError('transient'),base.llm(base.PLAN)]))
        result=await analyze_image(p,provider,'plan',{},[],planning=True)
        self.assertEqual(result,base.PLAN);self.assertEqual(provider.text_chat.await_count,2)
    async def test_repeated_failure_and_content_refusal_are_not_unbounded(self):
        from openai import BadRequestError
        p=SimpleNamespace(_audit=Mock())
        for exc,count in ((httpx.ConnectError('network'),2),(BadRequestError('blocked',response=httpx.Response(400,request=httpx.Request('POST','http://example.invalid')),body={'code':'content_policy_violation'}),1)):
            provider=SimpleNamespace(text_chat=AsyncMock(side_effect=exc))
            with self.assertRaises(type(exc)):await analyze_image(p,provider,'plan',{},[],planning=True)
            self.assertEqual(provider.text_chat.await_count,count)
    async def test_real_deadline_cancels_without_starting_another_model_call(self):
        ended=asyncio.Event()
        async def slow(**kwargs):
            try:await asyncio.sleep(10)
            finally:ended.set()
        provider=SimpleNamespace(text_chat=AsyncMock(side_effect=slow))
        with self.assertRaises(TimeoutError):await analyze_image(SimpleNamespace(_audit=Mock()),provider,'plan',{},[],planning=True,timeout_seconds=.02)
        self.assertTrue(ended.is_set());self.assertEqual(provider.text_chat.await_count,1)
    async def test_verification_does_not_retry_or_bypass_failed_check(self):
        provider=SimpleNamespace(text_chat=AsyncMock(side_effect=TimeoutError()))
        with self.assertRaises(TimeoutError):await analyze_image(SimpleNamespace(_audit=Mock()),provider,'verify',{},[],planning=False)
        self.assertEqual(provider.text_chat.await_count,1)
