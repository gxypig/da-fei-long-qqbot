import asyncio,json,unittest,copy
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from test_maintenance import ROOT
import test_image_delivery_memory as image_tests
from test_fullchain_foundation import event
from test_async_emoji import Event
from astrbot.api.message_components import Plain,Record,Image
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.star.star_handler import star_handlers_registry
from data.plugins.astrbot_plugin_proactive_agent.voice_memory import install_voice_memory,forbidden_voice_request
from data.plugins.astrbot_plugin_bili_scroll.share_recovery import reconcile

class MediaScope(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=image_tests.ImageTests.asyncSetUp
    capture=image_tests.ImageTests.capture
    send=image_tests.ImageTests.send
    async def test_group_followup_cannot_select_another_speakers_later_image(self):
        first=await self.capture();id1=await self.send(first)
        self.ledger.store.put(id1,{**self.ledger.store.rows[id1],'sender':'1'})
        second=await self.capture(image_tests.PNG+b'other');id2=await self.send(second)
        self.ledger.store.put(id2,{**self.ledger.store.rows[id2],'sender':'2'})
        e=event(text='刚才那张图是什么');e.get_sender_id=lambda:'1';e.get_group_id=lambda:'1'
        req=SimpleNamespace(conversation=SimpleNamespace(cid=self.cid),system_prompt='',image_urls=[])
        await self.images.enrich(e,req)
        self.assertIn(first['content_hash'],req.system_prompt);self.assertNotIn(second['content_hash'],req.system_prompt)
    async def test_current_image_takes_precedence_over_unquoted_history(self):
        await self.send(await self.capture());e=event(text='这张图上写的什么')
        e.get_messages=lambda:[Image(file='current.png')]
        req=SimpleNamespace(conversation=SimpleNamespace(cid=self.cid),system_prompt='',image_urls=['current.png'])
        await self.images.enrich(e,req)
        self.assertEqual(req.image_urls,['current.png']);self.assertIn('这次附件',req.system_prompt)
    async def test_gallery_retag_updates_first_caption_without_model(self):
        attachment=await self.capture();digest=attachment['content_hash']
        self.images.gallery.rows[digest]={'sha256':digest,'caption_status':'done','auto_tags':['旧标签']}
        self.assertTrue(self.images.reuse_gallery(digest));old=self.images.store.rows[digest]['source_revision']
        self.images.gallery.rows[digest]['caption_semantic']={'content_hash':digest,'description':'新确认的画面','ocr':[],
            'emotion':[],'inferred_usage':[],'text_complete':True}
        self.images.reuse_gallery(digest)
        self.assertNotEqual(self.images.store.rows[digest]['source_revision'],old)
        self.assertEqual(self.images.store.rows[digest]['semantic']['description'],'新确认的画面')
        self.provider.text_chat.assert_not_awaited()

class VoiceNegation(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_text_request_blocks_random_tts_even_at_probability_one(self):
        from astrbot.core.config.default import DEFAULT_CONFIG
        config=copy.deepcopy(DEFAULT_CONFIG)
        config['provider_tts_settings'].update(enable=True,trigger_probability=1.0,dual_output=False,use_file_service=False)
        config['platform_settings']['segmented_reply']['enable']=False
        config['content_safety']['also_use_in_response']=False;config['t2i']=False
        provider=SimpleNamespace(get_audio=AsyncMock(return_value='generated.wav'))
        ctx=SimpleNamespace(astrbot_config=config,plugin_manager=SimpleNamespace(context=SimpleNamespace(get_using_tts_provider_async=AsyncMock(return_value=provider))))
        stage=ResultDecorateStage();await stage.initialize(ctx)
        restore=install_voice_memory(SimpleNamespace(_platform=lambda:None,_cfg={}.get,_audit=lambda *_:None))
        try:
            with patch.object(star_handlers_registry,'get_handlers_by_event_type',return_value=[]),patch.object(SessionServiceManager,'should_process_tts_request',AsyncMock(return_value=True)),patch('astrbot.core.pipeline.result_decorate.stage.random.random',return_value=.01):
                for text in ['别发语音','用文字回复我','用语音说，算了不用发语音']:
                    e=Event(text=text);e.result.chain=[Plain('可以呀')]
                    async for _ in stage.process(e):pass
                    self.assertFalse(any(isinstance(p,Record) for p in e.result.chain))
                provider.get_audio.assert_not_awaited();self.assertEqual(stage.tts_trigger_probability,1)
            self.assertFalse(forbidden_voice_request('不要语音，还是用语音回复吧'))
        finally:restore()

class ShareRecovery(unittest.TestCase):
    def test_restart_distinguishes_unused_reserved_submitted_unknown_and_confirmed(self):
        reservation={'origin':'p:GroupMessage:1','attempt_id':'token','ledger_expected':True,'url':'https://www.bilibili.com/video/BVtest'}
        self.assertEqual(reconcile(reservation,[])['state'],'failed')
        row={'id':'delivery','origin':reservation['origin'],'external_operation':{'token':'token'},'state':'unknown','submission_state':'submitted'}
        self.assertEqual(reconcile(reservation,[row])['state'],'unknown')
        self.assertEqual(reconcile(reservation,[{**row,'state':'confirmed','text':reservation['url']}])['state'],'confirmed')
        self.assertEqual(reconcile(reservation,[{**row,'state':'confirmed','text':'看这个视频'}])['state'],'unknown')
        self.assertEqual(reconcile(reservation,[{**row,'state':'confirmed','text':'看这个视频'},row])['state'],'unknown')
        self.assertEqual(reconcile(reservation,[{**row,'state':'failed'}])['state'],'failed')
        self.assertEqual(reconcile({'origin':reservation['origin']},[])['state'],'unknown')
        self.assertEqual(reconcile(reservation,[{**row,'state':'confirmed','origin':'other'}])['state'],'failed')
