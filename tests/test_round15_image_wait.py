import asyncio,json,unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
import httpx
import test_round13_images as r13
from test_round8_visual import Event
from data.plugins.astrbot_plugin_proactive_agent import image_transport as transport
from data.plugins.astrbot_plugin_proactive_agent.image_generation import generate,install


class ImageWait(unittest.IsolatedAsyncioTestCase):
    setup_flow=r13.Flow.setup_flow

    async def test_wait_budget_replaces_short_deadline_without_retries_or_quality_changes(self):
        self.setup_flow([r13.answer(r13.picture())],[r13.PASS])
        options=Mock(return_value=self.client);self.client.with_options=options
        event=Event('画你拿着苹果的自拍')
        await generate(self.plugin,self.provider,event)
        timeout=options.call_args.kwargs['timeout']
        self.assertGreater(timeout.read,599);self.assertEqual(timeout.connect,10)
        self.assertEqual(options.call_args.kwargs['max_retries'],0)
        self.assertEqual(self.create.call_args.kwargs['quality'],'high')
        self.assertEqual(self.provider.text_chat.await_count,2)
        stages=[v.args[1]['stage'] for v in self.plugin._audit.call_args_list if v.args[0]=='image_generation_stage']
        self.assertEqual(stages,['preparing','planning','waiting_slot','rendering','result_received','verifying','ready'])
        self.assertEqual(event.get_extra('qqbot_image_progress')['stage'],'ready')

    async def test_shared_budget_does_not_restart_for_quality_correction(self):
        now=[10.];event=Event('画你');p=SimpleNamespace(_cfg=lambda k,d:d,_audit=Mock())
        wait=transport.ImageWait(p,event,clock=lambda:now[0]);client=SimpleNamespace(images=SimpleNamespace(generate=AsyncMock(return_value='image')))
        client.with_options=Mock(return_value=client)
        self.assertEqual(await wait.request(client,[],{'prompt':'safe','quality':'high'}),'image')
        now[0]+=240
        await wait.request(client,[],{'prompt':'safe correction','quality':'high'})
        self.assertEqual(client.with_options.call_args.kwargs['timeout'].read,360)
        now[0]+=361
        with self.assertRaisesRegex(TimeoutError,'budget_exhausted'):await wait.request(client,[],{})
        self.assertEqual(client.images.generate.await_count,2)

    async def test_hard_deadline_cancels_stalled_transport(self):
        cancelled=asyncio.Event()
        async def stalled(**kwargs):
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        create=AsyncMock(side_effect=stalled);client=SimpleNamespace(images=SimpleNamespace(generate=create));client.with_options=lambda **_:client
        event=Event('画你');wait=transport.ImageWait(SimpleNamespace(_cfg=lambda k,d:d,_audit=Mock()),event);wait.seconds=.02
        with self.assertRaises(TimeoutError):await wait.request(client,[],{})
        self.assertTrue(cancelled.is_set());self.assertEqual(create.await_count,1)
        self.assertEqual(transport.failure(TimeoutError(),event.get_extra('qqbot_image_progress'))[1],'unknown')

    async def test_ambiguous_failure_cached_no_repeated_post_and_no_fallback(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        self.setup_flow([TimeoutError()],[],{'image_generation_api_enabled':True})
        event=Event('拍一段视频');self.provider.text_chat.side_effect=[r13.llm({**r13.PLAN,'intent':'animation'})]
        with patch.object(Provider,'_query',AsyncMock()) as ordinary:
            restore=install(self.plugin);token=current_job.set(SimpleNamespace(event=event))
            try:
                a,b=await asyncio.gather(Provider._query(self.provider),Provider._query(self.provider))
                self.assertIs(a,b);self.assertEqual(self.create.await_count,1);ordinary.assert_not_awaited()
                self.assertIn('不确定',a.qqbot_status_receipt);self.assertEqual(event.get_extra('qqbot_image_progress')['outcome'],'unknown')
                self.assertNotIn('没生成成功',a.completion_text)
            finally:current_job.reset(token);restore()

    async def test_cancelled_request_stays_cancelled_and_releases_wait(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial as Provider
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        self.setup_flow([],[],{'image_generation_api_enabled':True})
        entered=asyncio.Event();ended=asyncio.Event()
        async def slow(**kwargs):
            entered.set()
            try:await asyncio.Event().wait()
            finally:ended.set()
        self.create.side_effect=slow;event=Event('画你')
        with patch.object(Provider,'_query',AsyncMock()):
            restore=install(self.plugin);token=current_job.set(SimpleNamespace(event=event))
            try:
                task=asyncio.create_task(Provider._query(self.provider));await entered.wait();task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertTrue(ended.is_set())
                with self.assertRaises(asyncio.CancelledError):await Provider._query(self.provider)
                self.assertEqual(self.create.await_count,1)
                self.assertEqual(event.get_extra('qqbot_image_progress')['stage'],'cancelled')
            finally:current_job.reset(token);restore()


class Diagnostics(unittest.TestCase):
    def test_precise_error_categories_hide_provider_details(self):
        from openai import APIStatusError,APITimeoutError,APIConnectionError
        request=httpx.Request('POST','http://example.test/images')
        secret='secret-token-private-url'
        def error(status,code=None):return APIStatusError(secret,response=httpx.Response(status,request=request),body={'error':{'code':code,'message':secret}})
        cases=[(error(401),'rendering','authorization_failed','failed'),(error(403),'rendering','authorization_failed','failed'),
               (error(429),'rendering','rate_limited','failed'),(error(400),'rendering','request_rejected','rejected'),
               (error(400,'content_policy_violation'),'rendering','content_rejected','rejected'),
               (APITimeoutError(request=request),'rendering','render_timeout','unknown'),
               (APIConnectionError(request=request),'rendering','render_connection_lost','unknown'),
               (error(504),'rendering','render_connection_lost','unknown'),
               (TimeoutError(),'verifying','verification_unavailable','unverified'),
               (TimeoutError(),'downloading','download_unavailable','unretrieved'),
               (TimeoutError(),'planning','preparation_unavailable','failed')]
        for exc,stage,reason,outcome in cases:
            result=transport.failure(exc,{'stage':stage,'submitted':stage not in {'planning'},'received':stage in {'verifying','downloading'}})
            self.assertEqual(result[:2],(reason,outcome),stage);self.assertNotIn(secret,str(result))

    def test_configuration_validation_and_legacy_default(self):
        from data.plugins.astrbot_plugin_proactive_agent import panel
        for value in (None,True,0,149,901,'600',float('inf')):
            self.assertEqual(transport.wait_seconds(lambda k,d:value),600)
        for value in (180,600,900):
            self.assertEqual(transport.wait_seconds(lambda k,d:value),value)
            self.assertEqual(panel.validate({**panel.FIELDS,'image_generation_timeout_seconds':value})['image_generation_timeout_seconds'],value)
        for value in (0,901,True):
            with self.assertRaises(ValueError):panel.validate({**panel.FIELDS,'image_generation_timeout_seconds':value})

    def test_queue_progress_is_snapshot_not_mutable_live_reference(self):
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import ReplyJob
        e=Event('画你');e.set_extra('qqbot_image_progress',{'stage':'rendering','attempts':1})
        job=ReplyJob('test:FriendMessage:one','one','id','画你',event=e)
        row=job.public(job.received);e.get_extra('qqbot_image_progress')['stage']='ready'
        self.assertEqual(row['image_progress']['stage'],'rendering')


class DelayedReference(unittest.IsolatedAsyncioTestCase):
    setup_flow=r13.Flow.setup_flow

    async def test_same_outfit_wording_finds_prior_image_without_waiting(self):
        import tempfile
        from pathlib import Path
        from astrbot.api.message_components import Image
        from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import IncomingVisual
        from data.plugins.astrbot_plugin_proactive_agent.image_spec import reference_paths
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'reference.png';path.write_bytes(r13.picture());now=[100.]
            p=SimpleNamespace(_cfg=lambda k,d:d,context=SimpleNamespace())
            service=IncomingVisual(p,lambda:now[0]);p._incoming_visual=service
            service.observe(Event(images=[Image.fromFileSystem(str(path))],mid='image'));now[0]+=9
            e=Event('看看你也这么穿的自拍',mid='request');service.observe(e)
            self.assertEqual([Path(v).resolve() for v in reference_paths(e,SimpleNamespace(image_urls=[],contexts=[]),p)],[path.resolve()])

    async def test_delayed_image_joins_before_planning_and_does_not_resume_twice(self):
        import tempfile
        from pathlib import Path
        from astrbot.api.message_components import Image
        from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import IncomingVisual
        from data.plugins.astrbot_plugin_proactive_agent.image_spec import followup_request
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'reference.png';path.write_bytes(r13.picture());now=[100.]
            self.setup_flow([r13.answer(r13.picture())],[r13.PASS]);service=IncomingVisual(self.plugin,lambda:now[0]);self.plugin._incoming_visual=service
            event=Event('看看你也这么穿的自拍',mid='request');service.observe(event)
            task=asyncio.create_task(generate(self.plugin,self.provider,event))
            for _ in range(100):
                if service.reference_waiters:break
                await asyncio.sleep(.001)
            self.assertTrue(service.reference_waiters);self.provider.text_chat.assert_not_awaited()
            now[0]+=18;image_event=Event(images=[Image.fromFileSystem(str(path))],mid='image');service.observe(image_event)
            result=await task;self.assertTrue(result.qqbot_generated_image);self.edit.assert_awaited_once()
            self.assertEqual(len(self.provider.text_chat.call_args_list[0].kwargs['image_urls']),1)
            self.assertEqual(service.reference_waiters,{})
            req=SimpleNamespace(image_urls=[str(path)],contexts=[{'role':'user','content':event.text},{'role':'assistant','content':'这张动作参考图现在读不到了，再发一下原图，本龙照着拍'}])
            self.assertIsNone(followup_request(image_event,req))

    async def test_reference_wait_actor_reset_quote_and_cancellation_boundaries(self):
        from astrbot.api.message_components import Image,Reply
        from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import IncomingVisual
        p=SimpleNamespace(_cfg=lambda k,d:d,context=SimpleNamespace());now=[100.]
        service=IncomingVisual(p,lambda:now[0]);event=Event('照着这张图画',mid='request');service.observe(event)
        task=asyncio.create_task(service.wait_reference(event,seconds=.1));await asyncio.sleep(0)
        now[0]+=1;service.observe(Event(images=[Image(file='wrong.png')],sender='other',mid='foreign'))
        self.assertFalse(task.done())
        service.observe(Event('换个话题',mid='break'));self.assertEqual(await task,[])
        event=Event('照着这张图画',mid='request2');now[0]+=1;service.observe(event)
        task=asyncio.create_task(service.wait_reference(event,seconds=.1));await asyncio.sleep(0);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(service.reference_waiters,{})
        now[0]+=1;event=Event('照着这张图画',mid='request3');service.observe(event)
        task=asyncio.create_task(service.wait_reference(event,seconds=.1));await asyncio.sleep(0)
        now[0]+=1;service.observe(Event(images=[Image(file='quoted.png')],mid='quote',extra=[Reply(id='q')]))
        self.assertEqual(await task,[])

    async def test_image_arriving_during_preparation_is_not_missed(self):
        from astrbot.api.message_components import Image
        from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import IncomingVisual
        from data.plugins.astrbot_plugin_proactive_agent.image_spec import followup_request
        now=[100.];p=SimpleNamespace(_cfg=lambda k,d:d,context=SimpleNamespace());service=IncomingVisual(p,lambda:now[0])
        event=Event('照着这张图画',mid='request');service.observe(event);now[0]+=1
        picture_event=Event(images=[Image(file='current.png')],mid='image');service.observe(picture_event)
        self.assertEqual(await service.wait_reference(event),['current.png'])
        req=SimpleNamespace(image_urls=['current.png'],contexts=[{'role':'user','content':event.text},{'role':'assistant','content':'这张动作参考图现在读不到了，再发一下原图'}])
        self.assertIsNone(followup_request(picture_event,req))
