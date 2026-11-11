import asyncio,base64,hashlib,io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from PIL import Image
from test_round8_visual import Event
from data.plugins.astrbot_plugin_proactive_agent import image_spec as spec
from data.plugins.astrbot_plugin_proactive_agent.image_animation import frames_to_gif,sample_sheet
from data.plugins.astrbot_plugin_proactive_agent.image_generation import generate,install,validate_image

def picture(sheet=False):
    out=io.BytesIO()
    with Image.new('RGB',(1536,1024) if sheet else (128,128),'white') as img:
        if sheet:
            for n in range(6):img.paste((n*35,100,200),(n%3*512,n//3*512,(n%3+1)*512,(n//3+1)*512))
        img.save(out,'PNG')
    return out.getvalue()
def answer(raw):return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(raw).decode())])
def llm(data):return SimpleNamespace(completion_text=json.dumps(data,ensure_ascii=False))
PASS={'pass':True,'identity_ok':True,'requirements_ok':True,'camera_ok':True,'motion_ok':True,'failures':[]}
PLAN={'intent':'image','subject_self':True,'scene':'成年白毛龙娘拿着一颗红苹果','requirements':['手里有一颗红苹果']}

class Specifications(unittest.TestCase):
    def test_huge_archive_never_reaches_images_api(self):
        event=Event('生成一个你拿着苹果的图片')
        req=SimpleNamespace(contexts=[{'role':'user','content':'旧话题'*100000}],extra_user_content_parts=[])
        material=spec.context_material(event,req);self.assertEqual(material['recent'],[])
        plan=spec.normalize_plan(PLAN,material,{'description':'白毛龙娘'})
        prompt=spec.prompt(plan);self.assertIn('苹果',prompt);self.assertNotIn('旧话题',prompt);self.assertLess(len(prompt),2000)
    def test_self_subject_and_scene_only_boundaries(self):
        for s in ('生成一个你拿着苹果的图片','看看你','拍一张自拍','画大肥龙拿苹果'):
            self.assertTrue(spec.self_subject(s),s)
        for s in ('你帮我画一只猫','不要自拍，画一颗苹果','不要画你，画苹果'):
            self.assertFalse(spec.self_subject(s),s)
        from data.plugins.astrbot_plugin_proactive_agent.visual_intent import generation_kind
        for s in ('生成一段代码','生成语音','剪辑一段视频','帮我写画图脚本','生成提示词'):
            self.assertEqual(generation_kind(s),'none',s)
    def test_explicit_current_request_is_not_silently_cut(self):
        with self.assertRaisesRegex(ValueError,'request_too_long'):spec.context_material(Event('画'+('字'*12000)),None)
    def test_selfie_camera_is_not_merely_character_identity(self):
        for text,own,expected in [('看看你',True,'selfie'),('自拍拿苹果',True,'selfie'),('自拍，另一只手拿着前置镜头',True,'selfie'),('对镜头自拍',True,'selfie'),('镜子自拍',True,'mirror_selfie'),('镜前自拍',True,'mirror_selfie'),('不要自拍，画你拿苹果',True,'third_person'),('画你的全身立绘',True,'third_person'),('你帮我画个苹果',False,'scene')]:
            self.assertEqual(spec.camera_mode(text,own,'third_person'),expected,text)
        plan=spec.normalize_plan(PLAN,{'request':'画你的全身立绘'},None)
        self.assertNotIn('自拍保持脸部',spec.prompt(plan,True))
        plan=spec.normalize_plan(PLAN,{'request':'来张自拍'},None)
        self.assertIn('自持前置摄像头',spec.prompt(plan,True))
    def test_projected_context_only_reaches_requirement_extraction(self):
        event=Event('那就按刚才说的画',origin='p:FriendMessage:test')
        event.set_extra('qqbot_image_projected_context',[{'role':'user','content':'整理后的要求：手里拿着一颗苹果。历史闲聊标记ABC'}])
        req=SimpleNamespace(contexts=[{'role':'user','content':'未压缩原历史'*90000}],extra_user_content_parts=[])
        material=spec.context_material(event,req)
        self.assertIn('历史闲聊标记ABC',str(material));self.assertNotIn('未压缩原历史',str(material))
        plan=spec.normalize_plan(PLAN,material,{'description':'白毛龙娘'})
        image_prompt=spec.prompt(plan)
        self.assertNotIn('历史闲聊标记ABC',image_prompt);self.assertNotIn('那就按刚才说的画',image_prompt)
        self.assertIn('苹果',image_prompt)
    def test_other_speaker_image_not_used_for_pose(self):
        def msg(uid,path):return {'role':'user','content':[{'type':'text','text':'[群聊发言身份] '+json.dumps({'QQ':uid})},{'type':'image_url','image_url':{'url':path}}]}
        req=SimpleNamespace(image_urls=[],contexts=[msg('one','same.png'),msg('other','foreign.png')])
        self.assertEqual(spec.reference_paths(Event('生成你做出这个图片相同的动作'),req),['same.png'])
    def test_fixed_reference_tamper_or_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'visual-identity';folder.mkdir();file=folder/'reference.png';file.write_bytes(picture())
            p=SimpleNamespace(_data_dir=root,_cfg=lambda k,d:True)
            profile={'reference':file.name,'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'description':'白毛龙娘'}
            (folder/'profile.json').write_text(json.dumps(profile),'utf-8');self.assertEqual(spec.identity(p)['path'],str(file.resolve()))
            file.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'changed'):spec.identity(p)
            profile['reference']='../../elsewhere.png';(folder/'profile.json').write_text(json.dumps(profile),'utf-8')
            with self.assertRaisesRegex(ValueError,'unavailable'):spec.identity(p)
    def test_generated_frames_remain_six_and_looped(self):
        raw=frames_to_gif(picture(True));self.assertEqual(validate_image(raw,True),'gif')
        with Image.open(io.BytesIO(raw)) as image:self.assertEqual(image.n_frames,6);self.assertEqual(image.info['loop'],0)
        self.assertTrue(sample_sheet(raw).startswith(b'\x89PNG'))
    def test_single_identical_sheet_cannot_pretend_to_move(self):
        raw=io.BytesIO()
        with Image.new('RGB',(1536,1024),'white') as image:image.save(raw,'PNG')
        with self.assertRaisesRegex(ValueError,'not_distinct'):frames_to_gif(raw.getvalue())
        with self.assertRaisesRegex(ValueError,'dimensions'):frames_to_gif(picture())

class Flow(unittest.IsolatedAsyncioTestCase):
    def setup_flow(self,creates,verdicts,extra=None):
        self.cfg={'image_quality_check_enabled':True,'image_animation_frames_fallback':True,**(extra or {})}
        self.create=AsyncMock(side_effect=creates);self.edit=AsyncMock(side_effect=creates)
        self.client=SimpleNamespace(images=SimpleNamespace(generate=self.create,edit=self.edit),with_options=lambda **_:self.client)
        self.provider=SimpleNamespace(client=self.client,text_chat=AsyncMock(side_effect=[llm(PLAN),*[llm(v) for v in verdicts]]))
        self.plugin=SimpleNamespace(_cfg=lambda k,d:self.cfg.get(k,d),context=SimpleNamespace(),_audit=Mock())
    async def test_required_object_rejection_gets_one_correction(self):
        self.setup_flow([answer(picture()),answer(picture())],[{**PASS,'pass':False,'requirements_ok':False,'failures':['没有苹果']},PASS])
        result=await generate(self.plugin,self.provider,Event('生成你拿苹果的图片'))
        self.assertTrue(result.qqbot_generated_image);self.assertEqual(self.create.await_count,2)
        self.assertIn('没有苹果',self.create.call_args.kwargs['prompt']);self.assertEqual(self.provider.text_chat.await_count,3)
    async def test_second_bad_picture_is_never_returned(self):
        bad={**PASS,'pass':False,'identity_ok':False,'failures':['变成真人']}
        self.setup_flow([answer(picture()),answer(picture())],[bad,bad])
        with self.assertRaisesRegex(ValueError,'quality_rejected'):await generate(self.plugin,self.provider,Event('画你'))
        self.assertEqual(self.create.await_count,2)
    async def test_portrait_is_rejected_when_selfie_was_requested(self):
        bad={**PASS,'pass':False,'camera_ok':False,'failures':['普通立绘，没有自拍视角']}
        self.setup_flow([answer(picture()),answer(picture())],[bad,PASS])
        event=Event('来张自拍拿苹果');await generate(self.plugin,self.provider,event)
        self.assertEqual(self.create.await_count,2)
        self.assertIn('没有自拍视角',self.create.call_args.kwargs['prompt'])
        self.assertEqual(event.get_extra('qqbot_image_provenance')['camera_mode'],'selfie')
    async def test_gif_preview_denial_falls_back_to_real_generated_frames(self):
        class Preview(Exception):status_code=400
        self.setup_flow([Preview('GIF output is not enabled for this request.'),answer(picture(True))],[PASS])
        self.provider.text_chat.side_effect=[llm({**PLAN,'intent':'animation'}),llm(PASS)]
        event=Event('生成你拿苹果挥手的动图');result=await generate(self.plugin,self.provider,event)
        self.assertEqual(self.create.await_count,2)
        self.assertEqual([x.kwargs['output_format'] for x in self.create.call_args_list],['gif','png'])
        raw=base64.b64decode(result.result_chain.chain[-1].file[9:]);self.assertEqual(validate_image(raw,True),'gif')
        self.assertEqual(event.get_extra('qqbot_image_provenance')['method'],'generated_action_frames')
        from data.plugins.astrbot_plugin_proactive_agent.image_generation import review_media
        from data.plugins.astrbot_plugin_proactive_agent.review_contract import original_evidence
        facts=review_media(event,result)
        self.assertEqual(facts['frames'],6);self.assertEqual(facts['format'],'gif')
        self.assertEqual(facts['generation_state'],'ready');self.assertEqual(facts['delivery_state'],'pending')
        evidence,_=original_evidence('case',[],[],{'generated_media':facts},'chat',1)
        self.assertEqual(evidence['可信状态']['generated_media'],facts)
        self.assertEqual(evidence['不可改变的事实']['本轮已完成媒体'],facts)
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        segment=await AiocqhttpMessageEvent._from_segment_to_dict(result.result_chain.chain[-1])
        self.assertEqual(base64.b64decode(segment['data']['file'][9:]),raw)
    async def test_auth_errors_do_not_trigger_another_generator(self):
        class Auth(Exception):status_code=403
        self.setup_flow([Auth('not authorized')],[])
        with self.assertRaises(Auth):await generate(self.plugin,self.provider,Event('生成挥手动图'))
        self.assertEqual(self.create.await_count,1)
    async def test_verification_timeout_does_not_send_unchecked_picture(self):
        self.setup_flow([answer(picture())],[])
        self.provider.text_chat.side_effect=[llm(PLAN),TimeoutError()]
        with self.assertRaises(TimeoutError):await generate(self.plugin,self.provider,Event('画你拿苹果'))
        self.assertEqual(self.create.await_count,1)
    async def test_two_concurrent_calls_only_generate_once_and_ordinary_chat_passes(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        self.setup_flow([answer(picture())],[PASS],{'image_generation_api_enabled':True})
        event=Event('画你拿苹果');job=SimpleNamespace(event=event)
        with patch.object(ProviderOpenAIOfficial,'_query',AsyncMock(return_value='ordinary')) as ordinary:
            restore=install(self.plugin);token=current_job.set(job)
            try:
                a,b=await asyncio.gather(ProviderOpenAIOfficial._query(self.provider),ProviderOpenAIOfficial._query(self.provider))
                self.assertIs(a,b);self.assertEqual(self.create.await_count,1)
                event.text='今天吃了没';self.assertEqual(await ProviderOpenAIOfficial._query(self.provider),'ordinary');ordinary.assert_awaited_once()
            finally:current_job.reset(token);restore()
    async def test_reference_works_for_different_accounts_and_jpeg_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'visual-identity';folder.mkdir();p=folder/'canonical.png';p.write_bytes(picture())
            profile={'reference':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'description':'成年白毛紫眼龙娘'}
            (folder/'profile.json').write_text(json.dumps(profile),'utf-8')
            for sender in ('father','another_account'):
                self.setup_flow([answer(picture())],[PASS],{'image_identity_enabled':True});self.plugin._data_dir=root
                event=Event('生成你拿苹果的图片',sender=sender);event.set_extra('qqbot_character',{'persona_status':'selected','persona_variants':{'image':'错误的真人描述'}})
                await generate(self.plugin,self.provider,event)
                self.create.assert_not_awaited();self.edit.assert_awaited_once()
                self.assertIn('成年白毛紫眼龙娘',self.edit.call_args.kwargs['prompt']);self.assertNotIn('错误的真人描述',self.edit.call_args.kwargs['prompt'])
                with Image.open(io.BytesIO(self.edit.call_args.kwargs['image'][0][1])) as uploaded,Image.open(p) as original:
                    self.assertEqual(uploaded.convert('RGBA').tobytes(),original.convert('RGBA').tobytes())
