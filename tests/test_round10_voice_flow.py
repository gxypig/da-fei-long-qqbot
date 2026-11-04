import asyncio,copy,json,tempfile,unittest,wave,struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from test_maintenance import Main
from test_async_emoji import Event
from astrbot.api.message_components import Plain,Record,Image
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.star.star_handler import star_handlers_registry
from data.plugins.astrbot_plugin_proactive_agent.voice_memory import install_voice_memory,component_body
from data.plugins.astrbot_plugin_proactive_agent.fish_voice import normalize_wav,VoiceFailure,replace_stale_audio,FREE

class VoiceFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg=copy.deepcopy(DEFAULT_CONFIG)
        self.cfg['provider_tts_settings'].update(enable=True,trigger_probability=.05,dual_output=False,use_file_service=False)
        self.cfg['platform_settings']['segmented_reply']['enable']=False
        self.cfg['content_safety']['also_use_in_response']=False;self.cfg['t2i']=False
        self.provider=SimpleNamespace(get_audio=AsyncMock(return_value='test.wav'),get_model=lambda:'test-tts',provider_config={'id':'test'})
        self.context=SimpleNamespace(get_using_tts_provider_async=AsyncMock(return_value=self.provider))
        self.stage=ResultDecorateStage();await self.stage.initialize(SimpleNamespace(astrbot_config=self.cfg,plugin_manager=SimpleNamespace(context=self.context)))
        self.plugin=SimpleNamespace(_cfg={}.get,_platform=lambda:None,_audit=Mock(),context=self.context)
        self.addCleanup(install_voice_memory(self.plugin))
        for target,value in [(patch.object(star_handlers_registry,'get_handlers_by_event_type',return_value=[]),None),
                             (patch.object(SessionServiceManager,'should_process_tts_request',AsyncMock(return_value=True)),None)]:
            target.start();self.addCleanup(target.stop)

    async def process(self,event,roll=.99):
        with patch('random.random',return_value=roll):
            async for _ in self.stage.process(event):pass

    async def test_adjacent_short_chunks_one_synthesis_and_final_body_callback(self):
        e=Event('用语音回复');e.result.chain=[Plain('本龙在呢'),Plain('，你说')]
        seen=[];e.set_extra('qqbot_emoji_final_ready',seen.append)
        await self.process(e)
        self.provider.get_audio.assert_awaited_once_with('本龙在呢，你说')
        self.assertEqual(seen,['本龙在呢，你说']);self.assertEqual(e.get_extra('qqbot_voice_result_text'),'本龙在呢，你说')
        self.assertEqual(e.get_extra('qqbot_voice_synthesis')[0]['state'],'synthesized')

    async def test_native_pipeline_empty_reentry_keeps_completed_body_and_voice_fact(self):
        # The real scheduler re-enters later stages after RespondStage clears
        # the result. This is lifecycle cleanup, not a new empty reply.
        e=Event('用语音回复');e.result.chain=[Plain('本龙在呢')]
        seen=[];e.set_extra('qqbot_emoji_final_ready',seen.append)
        await self.process(e)
        e.clear_result()
        await self.process(e)
        self.assertEqual(seen,['本龙在呢'])
        self.assertEqual(e.get_extra('qqbot_voice_result_text'),'本龙在呢')
        self.provider.get_audio.assert_awaited_once()

    async def test_probability_and_200_limit_no_call_for_ordinary_text(self):
        for text,roll,count in [('短句',.01,1),('短句',.1,0),('长'*201,.01,0)]:
            self.provider.get_audio.reset_mock();e=Event('聊聊');e.result.chain=[Plain(text)]
            await self.process(e,roll);self.assertEqual(self.provider.get_audio.await_count,count)
        self.assertEqual(self.cfg['provider_tts_settings']['trigger_probability'],.05)

    async def test_explicit_long_voice_keeps_every_character_and_attachment_order(self):
        text=('第一段。\n'*70)+'最后一句'
        e=Event('用语音读出来');attachment=Image(file='x.png');e.result.chain=[Plain(text),attachment,Plain('再见')]
        await self.process(e)
        transcripts=''.join(p.text for p in e.result.chain if isinstance(p,Record))
        self.assertEqual(transcripts,text+'再见');self.assertIs(e.result.chain[-2],attachment)
        self.assertTrue(all(len(call.args[0])<=200 for call in self.provider.get_audio.await_args_list))

    async def test_failed_synthesis_returns_complete_text_without_switch(self):
        self.provider.get_audio.side_effect=RuntimeError('upstream failure')
        e=Event('用语音');e.result.chain=[Plain('完整台词')]
        await self.process(e)
        self.assertEqual(component_body(e.result),'完整台词');self.assertFalse(any(isinstance(p,Record) for p in e.result.chain))
        self.assertEqual(e.get_extra('qqbot_voice_synthesis')[0]['state'],'failed')
        self.provider.get_audio.assert_awaited_once()

    async def test_dual_output_is_one_logical_transcript(self):
        self.cfg['provider_tts_settings']['dual_output']=True
        e=Event('用语音');e.result.chain=[Plain('晚安')]
        await self.process(e)
        self.assertEqual([type(p).__name__ for p in e.result.chain],['Record','Plain'])
        self.assertEqual(component_body(e.result),'晚安');self.assertEqual(e.get_extra('qqbot_voice_result_text'),'晚安')

    async def test_rewritten_audio_invalidates_original_and_one_resynthesis_limit(self):
        e=Event('语音回复');e.set_extra('qqbot_voice_synthesis',[{'state':'synthesized','model':'test-tts','voice':None}])
        old=[{'type':'at','data':{'qq':'123'}},{'type':'record','data':{'file':'old.wav'}},{'type':'image','data':{'file':'x.png'}}]
        with patch('data.plugins.astrbot_plugin_proactive_agent.fish_voice.synthesize',AsyncMock(return_value=(None,{'state':'failed'}))) as call:
            once=await replace_stale_audio(self.plugin,e,old,'新台词',e.unified_msg_origin)
            twice=await replace_stale_audio(self.plugin,e,old,'再改台词',e.unified_msg_origin)
        call.assert_awaited_once();self.assertEqual([p['type'] for p in once],['at','text','image'])
        self.assertEqual(once[1]['data']['text'],'新台词');self.assertEqual(twice[1]['data']['text'],'再改台词')
        self.assertNotIn('old.wav',json.dumps(once));self.assertNotIn('old.wav',json.dumps(twice))

class WAV(unittest.TestCase):
    def test_streaming_placeholder_is_fixed_without_changing_pcm(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'voice.wav';pcm=b'\x01\x00'*8000
            with wave.open(str(p),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(pcm)
            raw=bytearray(p.read_bytes());raw[4:8]=struct.pack('<I',0xffffffff);raw[40:44]=struct.pack('<I',0xffffff00);p.write_bytes(raw)
            info=normalize_wav(p);self.assertEqual(info['duration_seconds'],1);self.assertTrue(info['header_finalized'])
            with wave.open(str(p),'rb') as w:self.assertEqual(w.readframes(w.getnframes()),pcm)
            self.assertFalse(normalize_wav(p)['header_finalized'])
    def test_true_truncation_is_not_disguised_as_streaming_header(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'voice.wav'
            with wave.open(str(p),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\0\0'*8000)
            p.write_bytes(p.read_bytes()[:-100])
            with self.assertRaises(VoiceFailure):normalize_wav(p)
