import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from test_async_emoji import Event
from data.plugins.astrbot_plugin_proactive_agent.voice_memory import explicit_voice_request,install_voice_memory
from astrbot.api.message_components import Record,Plain
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.star.star_handler import star_handlers_registry


class VoicePolicy(unittest.IsolatedAsyncioTestCase):
    def test_request_not_history_negation_quotation_or_configuration(self):
        for text in ['发条语音','能用语音回复我吗','用声音说你好','我想听你的声音','刚才没听清，再发一条语音','用语音说“你好”','用语音','语音回我','换成语音吧']:
            self.assertTrue(explicit_voice_request(text),text)
        for text in ['别发语音','发语音吧，不用发语音了','你刚才发的语音说什么','你发过语音吗','我发语音你能听懂吗','我用语音说给你听','不是让你发语音','语音概率设置10%','他说“发语音”是什么意思','今天好开心']:
            self.assertFalse(explicit_voice_request(text),text)

    async def test_forced_voice_and_one_character_preserve_normal_ten_percent_setting(self):
        config=copy.deepcopy(DEFAULT_CONFIG)
        config['provider_tts_settings'].update(enable=True,trigger_probability=.1,dual_output=False,use_file_service=False)
        config['platform_settings']['segmented_reply']['enable']=False
        config['content_safety']['also_use_in_response']=False
        config['t2i']=False
        provider=SimpleNamespace(get_audio=AsyncMock(return_value='generated.wav'))
        ctx=SimpleNamespace(astrbot_config=config,plugin_manager=SimpleNamespace(context=SimpleNamespace(
            get_using_tts_provider_async=AsyncMock(return_value=provider))))
        stage=ResultDecorateStage();await stage.initialize(ctx)
        plugin=SimpleNamespace(_platform=lambda:None,_cfg={}.get,_audit=lambda *_:None)
        restore=install_voice_memory(plugin)
        try:
            with patch.object(star_handlers_registry,'get_handlers_by_event_type',return_value=[]),patch.object(SessionServiceManager,'should_process_tts_request',AsyncMock(return_value=True)),patch('astrbot.core.pipeline.result_decorate.stage.random.random',return_value=.9):
                for request,reply,wanted in [('发条语音','好呀',True),('用语音回复我','嗯',True),('今天真开心','好呀',False)]:
                    e=Event(text=request);e.result.chain=[Plain(reply)]
                    async for _ in stage.process(e):pass
                    self.assertEqual(any(isinstance(p,Record) for p in e.result.chain),wanted)
                    if wanted:self.assertEqual(e.get_extra('qqbot_voice_result_text'),reply)
                self.assertEqual(stage.tts_trigger_probability,.1)
                self.assertEqual(config['provider_tts_settings']['trigger_probability'],.1)
        finally:restore()
