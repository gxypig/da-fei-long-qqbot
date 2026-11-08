import asyncio,base64,io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from test_maintenance import ROOT
import test_round2_life as life_base
import test_round8_visual as visual_base
import test_round2_acceptance as learning_base
from PIL import Image
from data.plugins.astrbot_plugin_proactive_agent.image_generation import animated_request,validate_image,generate
from data.plugins.astrbot_plugin_proactive_agent.incoming_cache import IncomingCache

class Life(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=life_base.LifeTests.asyncSetUp
    asyncTearDown=life_base.LifeTests.asyncTearDown
    async def test_life_is_same_in_regular_chat_and_panel(self):
        self.life.advance();activity=self.life.activity()['current_activity']
        for text in ('在干啥','忙啥呢','你在干什么','晚饭吃啥'):
            self.assertIn(activity,self.life.context(self.origin,text))
        self.assertLess(len(self.life.context(self.origin,'你现在在干啥')),400)
        self.plugin._generate_json.assert_not_awaited()
    async def test_review_receives_the_same_current_activity_without_an_extra_call(self):
        from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer
        generate=AsyncMock(return_value=json.dumps({'pass':True,'natural':True,'chat_fit':True}))
        reviewer=ReplyReviewer(generate,lambda *_:None)
        state={'current_life':{'current_activity':'整理刚才那堆东西','source':'角色虚拟日常'}}
        await reviewer.review('还在整理刚才那堆东西',origin=self.origin,recent=['你在干啥'],character=state)
        self.assertEqual(generate.await_count,1)
        self.assertEqual(generate.call_args.args[1]['不可改变的事实']['角色当前生活'],state['current_life'])
    async def test_items_expire_without_fabricating_completion(self):
        e=self.life.event('plan','明天讨论画稿',visibility=self.origin,expires=self.now+1,extra={'kind':'promise'})
        self.life.store.put('life',{'promises':[{'id':e['id'],'summary':e['summary'],'visibility':self.origin}]})
        self.now+=2;self.life.maintain_items()
        self.assertEqual(self.life.activity()['promises'],[])
        r=self.life.store.rows['event:'+e['id']];self.assertEqual(r['archive_reason'],'expired');self.assertFalse(r['completed'])
    def event(self):return self.life.event('chat_confirmed','一起聊了游戏里反复失败的关卡，我讲了自己的想法',visibility=self.origin,completed=True)
    def answer(self,e):return {'event_id':e['id'],'basis_quote':'反复失败的关卡','summary':'今天和朋友聊起卡关，这个话题聊得挺起劲','thought':'有时一起吐槽比硬撑有意思','topic':'聊游戏',
        'check':{k:True for k in ('identifiers_removed','sensitive_details_removed','not_reidentifiable','grounded','no_private_quote')}}
    async def test_auto_private_projection_has_no_origin_or_raw_message(self):
        self.config['life_public_auto']=True;e=self.event();self.plugin._generate_json.side_effect=[json.dumps(self.answer(e)),json.dumps({'privacy_safe':True,'grounded':True})]
        await self.life.public.step();r=next(r for r in self.life.events('qzone') if r.get('operation')=='anonymized_shared_experience')
        self.assertNotIn(self.origin,str(r));self.assertNotEqual(r['summary'],e['summary']);self.assertEqual(self.sent,[])
        count=self.plugin._generate_json.await_count;self.now+=61;await self.life.public.step();self.assertEqual(count,self.plugin._generate_json.await_count)
        # Replay the crash window: projection survived but source acknowledgement did not.
        self.life.store.put('event:'+e['id'],e);self.now+=61;await self.life.public.step()
        self.assertEqual(count,self.plugin._generate_json.await_count)
        self.assertEqual(len([r for r in self.life.events('qzone') if r.get('public_projection_of')==e['id']]),1)
    async def test_sensitive_or_invalid_projection_never_publishes(self):
        self.config['life_public_auto']=True;e=self.event();data=self.answer(e);data['summary']='朋友QQ是123456789'
        self.plugin._generate_json.return_value=json.dumps(data);await self.life.public.step();self.assertFalse(self.life.events('qzone'))
        self.life.event('chat_confirmed','验证码是123456，不能公开',visibility=self.origin,completed=True)
        self.now+=61;await self.life.public.step();self.assertFalse(self.life.events('qzone'))
    async def test_final_dynamic_is_checked_after_rewrite(self):
        self.plugin._generate_json.return_value=json.dumps({'privacy_safe':False,'grounded':True})
        with self.assertRaises(ValueError):await self.life.public.verify_composed({'summary':'聊游戏'},'和朋友聊游戏')
        self.assertEqual(self.sent,[])
    async def test_published_dynamics_are_remembered_and_not_repeated(self):
        e=self.life.event('tool','读取了公开词条',visibility='public',completed=True)
        self.plugin._post_qzone=AsyncMock();result=await self.life.opportunity('qzone')
        self.assertEqual(result['decision'],'confirmed');self.assertIn('自己确实发过的动态',self.life.context(self.origin,'刚发的动态'))
        self.assertTrue(self.life.store.rows['event:'+e['id']]['published_at'])

class Burst(visual_base.Visual):
    async def test_multi_text_multi_image_burst_and_cross_sender(self):
        self.service.observe(visual_base.Event('看这个',mid='a'));self.now+=1
        self.service.observe(visual_base.Event(images=[self.image()],mid='b'));self.now+=1
        self.service.observe(visual_base.Event('右边是我修改的',mid='c'));self.now+=1
        self.service.observe(visual_base.Event(images=[self.image()],mid='d'));self.now+=1
        self.service.observe(visual_base.Event('另一个人说的话',sender='other',mid='foreign'))
        req=await self.enrich(visual_base.Event('两个哪个更好',mid='e'))
        self.assertEqual(len(req.image_urls),2)
        notes=''.join(getattr(p,'text','') for p in req.extra_user_content_parts)
        self.assertIn('右边是我修改的',notes);self.assertNotIn('另一个人说的话',notes)

class Cache(unittest.IsolatedAsyncioTestCase):
    async def test_native_cloned_request_and_history_hook_capture_incoming_only(self):
        from dataclasses import replace
        from astrbot.core.provider.entities import ProviderRequest
        from data.plugins.astrbot_plugin_proactive_agent.incoming_visual import install
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'gallery.png'
            with Image.new('RGB',(32,32),'white') as picture:picture.save(path)
            persisted=AsyncMock();manager=SimpleNamespace(update_conversation=persisted)
            plugin=SimpleNamespace(_data_dir=root,_cfg=lambda k,d:d,context=SimpleNamespace(conversation_manager=manager))
            event=visual_base.Event('看看这张图');req=ProviderRequest(prompt=event.text,image_urls=[str(path)])
            event.set_extra('qqbot_generation_request',req);restore=install(plugin)
            token=current_job.set(SimpleNamespace(event=event))
            try:
                message=await replace(req).assemble_context()
                await manager.update_conversation(event.unified_msg_origin,'cid',history=[message])
                self.assertTrue(plugin._incoming_visual.cache.uri_keys)
                history=persisted.call_args.kwargs['history']
                self.assertNotIn('base64,',json.dumps(history));self.assertTrue(path.exists())
            finally:
                current_job.reset(token);restore()
                if plugin._incoming_visual.cache.task:await asyncio.gather(plugin._incoming_visual.cache.task,return_exceptions=True)
    def test_temporary_incoming_images_do_not_enter_durable_backups(self):
        from data.plugins.astrbot_plugin_proactive_agent.data_backup import inventory
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cache=root/'plugin_data'/'agent'/'incoming-images';cache.mkdir(parents=True)
            (cache/'temporary.png').write_bytes(b'private incoming')
            gallery=root/'plugin_data'/'hub'/'gallery';gallery.mkdir(parents=True);(gallery/'own.png').write_bytes(b'own')
            names=inventory(root)
            self.assertFalse(any('incoming-images' in n for n in names));self.assertIn('plugin_data/hub/gallery/own.png',names)
    async def test_history_stores_only_temporary_reference_and_keeps_sent_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[100.];cache=IncomingCache(Path(tmp)/'cache',lambda:now[0]);await cache.start();cache.stop()
            uri='data:image/png;base64,'+base64.b64encode(b'new incoming image').decode()
            message={'role':'user','content':[{'type':'text','text':'这是哪种猫'},{'type':'image_url','image_url':{'url':uri}}]}
            sent={'role':'assistant','content':[{'type':'image_url','image_url':{'url':uri}}]}
            await cache.capture_message(message);result=cache.portable_history([message,sent])
            self.assertTrue(Path(result[0]['content'][1]['image_url']['url']).exists());self.assertEqual(result[1],sent)
            self.assertEqual(message['content'][1]['image_url']['url'],uri)
            now[0]+=3601;await cache.cleanup();result=cache.portable_history([message,sent])
            self.assertEqual(result[0]['content'][1]['type'],'text');self.assertEqual(result[1],sent)
            await cache.capture_message(message);self.assertEqual(cache.portable_history([message])[0]['content'][1]['type'],'text')
            await asyncio.gather(cache.task,return_exceptions=True)
    async def test_one_hour_cleanup_restart_and_gallery_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);gallery=root/'gallery.png';gallery.write_bytes(b'gallery')
            native=root/'temp';native.mkdir();source=native/'incoming.png';source.write_bytes(b'incoming')
            now=[100.];cache=IncomingCache(root/'cache',lambda:now[0],temp_root=native);await cache.start();cache.stop()
            own=await cache.retain(gallery);incoming=await cache.retain(source)
            now[0]+=3599;await cache.cleanup();self.assertTrue(Path(incoming).exists())
            again=IncomingCache(root/'cache',lambda:now[0],temp_root=native);await again.start();again.stop()
            now[0]+=2;await again.cleanup();self.assertFalse(Path(incoming).exists());self.assertFalse(Path(own).exists())
            self.assertTrue(gallery.exists());self.assertFalse(source.exists())
            if cache.task:await asyncio.gather(cache.task,return_exceptions=True)
            if again.task:await asyncio.gather(again.task,return_exceptions=True)

class ImageRoute(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_gif_is_clear_and_not_retried_or_claimed_success(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        from data.plugins.astrbot_plugin_proactive_agent.image_generation import install
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        class Unavailable(Exception):status_code=400
        create=AsyncMock(side_effect=Unavailable('GIF output is not enabled for this request.'))
        client=SimpleNamespace(images=SimpleNamespace(generate=create),with_options=lambda **_:client)
        plugin=SimpleNamespace(_cfg=lambda k,d:True if k=='image_generation_api_enabled' else d,context=SimpleNamespace(),_audit=Mock())
        event=visual_base.Event('做个GIF');job=SimpleNamespace(event=event)
        with patch.object(ProviderOpenAIOfficial,'_query',AsyncMock(return_value='ordinary')) as ordinary:
            restore=install(plugin);token=current_job.set(job)
            try:
                provider=SimpleNamespace(client=client,text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps({'intent':'animation','scene':'眨眼','requirements':[]}))))
                a=await ProviderOpenAIOfficial._query(provider);b=await ProviderOpenAIOfficial._query(provider)
                self.assertIn('还没开放动图',a.completion_text);self.assertIs(a,b);self.assertEqual(create.await_count,1)
                self.assertEqual(job.reply_purpose,'error_notification');ordinary.assert_not_awaited()
                event.text='吃了没';self.assertEqual(await ProviderOpenAIOfficial._query(provider),'ordinary')
            finally:current_job.reset(token);restore()
    def test_only_explicit_animation(self):
        from data.plugins.astrbot_plugin_proactive_agent.visual_intent import generation_kind
        for s in ('生成一张眨眼动图','做个GIF','把这张图改成会动的表情','来张动图','能不能生成动图'):
            self.assertTrue(animated_request(s),s);self.assertNotEqual(generation_kind(s),'none',s)
        for s in ('画只猫','不要动图，生成一张静态图','gif是什么','怎么生成动图'):self.assertFalse(animated_request(s),s)
    def test_real_frames_validation(self):
        a=Image.new('RGB',(16,16),'white');b=Image.new('RGB',(16,16),'black');out=io.BytesIO();a.save(out,'GIF',save_all=True,append_images=[b],duration=200)
        self.assertEqual(validate_image(out.getvalue(),True),'gif')
        single=io.BytesIO();a.save(single,'GIF')
        with self.assertRaises(ValueError):validate_image(single.getvalue(),True)
        a.close();b.close()
    async def test_images_api_result_keeps_gif_bytes_and_normal_chain(self):
        a=Image.new('RGB',(16,16),'white');b=Image.new('RGB',(16,16),'black');out=io.BytesIO();a.save(out,'GIF',save_all=True,append_images=[b],duration=200);a.close();b.close()
        create=AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(out.getvalue()).decode())]))
        client=SimpleNamespace(images=SimpleNamespace(generate=create),with_options=lambda **_:client)
        plugin=SimpleNamespace(_cfg=lambda k,d:d,context=SimpleNamespace(),_audit=Mock())
        event=visual_base.Event('生成眨眼动图')
        provider=SimpleNamespace(client=client,text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps({'intent':'animation','scene':'眨眼','requirements':[]}))))
        response=await generate(plugin,provider,event)
        self.assertEqual(event.get_extra('qqbot_generated_format'),'gif');self.assertTrue(response.qqbot_generated_image)
        sent=response.result_chain.chain[-1].file;self.assertEqual(base64.b64decode(sent.removeprefix('base64://')),out.getvalue())
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        segment=await AiocqhttpMessageEvent._from_segment_to_dict(response.result_chain.chain[-1])
        self.assertEqual(base64.b64decode(segment['data']['file'].removeprefix('base64://')),out.getvalue())
        self.assertEqual(create.call_args.kwargs['output_format'],'gif');self.assertNotIn('complexity',create.call_args.kwargs)

class Recovery(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=learning_base.LearningAcceptance.asyncSetUp
    async def test_old_failure_backoff_is_not_restarted_by_reboot(self):
        import time
        now=time.time();self.s.enqueue_meme('旧网络失败',[],'p:FriendMessage:1')
        with self.s._db:self.s._db.execute("UPDATE jobs SET status='failed',progress=?",(json.dumps({'last_error':'TimeoutError','last_failed_at':now-86400}),))
        self.s._recover_backlog();row=self.s._db.execute('SELECT * FROM jobs').fetchone()
        self.assertLess(row['ready'],now+2);self.assertEqual(row['status'],'pending')
        self.s._next_recovery=0;self.s._recover_backlog()
        self.assertEqual(row['ready'],self.s._db.execute('SELECT ready FROM jobs').fetchone()[0])
    async def test_legacy_delayed_job_does_not_block_missing_example_batch(self):
        self.cfg['meme_understanding_enabled']=True
        self.memory.entries={'test':{'phrase':'测试梗','meaning':'测试','usage':'测试','examples':[]}}
        self.memory.fresh=lambda _:True
        self.s._enqueue('usage',{'text':'旧任务'},key='legacy-usage')
        self.s._enqueue_examples()
        self.assertEqual(self.s._db.execute("SELECT count(*) FROM jobs WHERE key LIKE 'usage-v2:%'").fetchone()[0],1)
    async def test_transient_recovery_and_unverified_archive_are_distinct(self):
        import time
        self.s.enqueue_meme('测试网络恢复',[],'p:FriendMessage:1')
        row=self.s._db.execute('SELECT * FROM jobs').fetchone();now=time.time()
        with self.s._db:self.s._db.execute("UPDATE jobs SET status='failed',attempts=3,progress=? WHERE id=?",(json.dumps({'last_error':'TimeoutError'}),row['id']))
        self.s._recover_backlog();job=self.s._db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(job['status'],'pending');self.assertGreater(job['ready'],now+1700)
        with self.s._db:self.s._db.execute("UPDATE jobs SET status='unverified',created=?",(now-90000,))
        self.s._recovery_check_at=0
        with patch('data.plugins.astrbot_plugin_proactive_agent.idle_learning.time.time',return_value=now+120):self.s._recover_backlog()
        self.assertEqual(self.s.details(active_only=True)['jobs'],[])
        self.assertEqual(self.s.details()['jobs'][0]['status'],'archived_unverified')
