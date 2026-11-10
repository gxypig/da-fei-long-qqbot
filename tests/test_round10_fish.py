import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.fish_voice import FREE,PAID,BASE,validate,VoiceFailure,synthesize,paragraphs,install_fish_guard

def provider(model=FREE):
    config={'model':model,'api_base':BASE,'fishaudio-tts-reference-id':'a'*32,'api_key':'test-key'}
    return SimpleNamespace(provider_config=config,headers={'model':model},get_model=lambda:config['model'],api_base=BASE,reference_id='a'*32)

class FishTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_model_header_and_reference_before_network(self):
        for model in (FREE,PAID):self.assertEqual(validate(provider(model))[0],model)
        for bad in ('',None,'s2-pro','s2.1-pro-free ', 'S2.1-Pro-Free'):
            with self.assertRaises(VoiceFailure):validate(provider(bad))
        for headers in ({},{'model':PAID},{'Model':FREE,'model':FREE}):
            p=provider();p.headers=headers
            with self.assertRaises(VoiceFailure):validate(p)
        p=provider();p.api_base='https://api.fish-audio.cn/v1'
        with self.assertRaises(VoiceFailure):validate(p)
        p=provider();p.reference_id='b'*32
        with self.assertRaises(VoiceFailure):validate(p)

    def test_paragraphs_never_lose_long_transcript(self):
        text=('这段话。\n中英 hello 123？'*40)+'尾巴'
        parts=paragraphs(text)
        self.assertEqual(''.join(parts),text);self.assertTrue(all(len(x)<=200 for x in parts))

    async def test_one_total_timeout_returns_text_fallback_information(self):
        p=provider()
        async def slow(text):await asyncio.sleep(1)
        p.get_audio=slow
        audio,meta=await synthesize(p,'你好',budget=.01)
        self.assertIsNone(audio);self.assertEqual(meta['error'],'timeout');self.assertEqual(meta['bytes'],6)

    async def test_timeout_records_one_real_voice_span_without_extra_request(self):
        import time
        from data.plugins.astrbot_plugin_proactive_agent.reply_jobs import current_job
        from data.plugins.astrbot_plugin_proactive_agent.chain_metrics import timeline
        p=provider()
        async def slow(_):await asyncio.sleep(1)
        p.get_audio=AsyncMock(side_effect=slow)
        job=SimpleNamespace(received=time.monotonic(),timings={},spans=[])
        token=current_job.set(job)
        try:audio,meta=await synthesize(p,'你好',budget=.01)
        finally:current_job.reset(token)
        self.assertIsNone(audio);p.get_audio.assert_awaited_once()
        self.assertEqual(len(job.spans),1);self.assertEqual(job.spans[0]['stage'],'voice_synthesis')
        self.assertGreaterEqual(job.timings['voice_synthesis_ms'],9)
        self.assertEqual(timeline(job)['overlap_ms'],0)

    async def test_free_failure_never_calls_paid_and_only_retryable_status_retries(self):
        from astrbot.core.provider.sources.fishaudio_tts_api_source import ProviderFishAudioTTSAPI as Fish
        for error,count in [('401 unauthorized api_key=secret',1),('402 balance',1),('404 missing voice',1),('429 rate limit',2),('503 unavailable',2)]:
            network=AsyncMock(side_effect=Exception(error));p=provider()
            with patch.object(Fish,'get_audio',network):
                restore=install_fish_guard(SimpleNamespace(_cfg=lambda k,d:d))
                try:
                    p.get_audio=lambda text:Fish.get_audio(p,text)
                    audio,meta=await synthesize(p,'本龙在呢',budget=2)
                finally:restore()
            self.assertIsNone(audio);self.assertEqual(network.await_count,count)
            self.assertEqual(p.headers['model'],FREE);self.assertNotIn('secret',str(meta))

    async def test_invalid_configuration_cannot_submit(self):
        from astrbot.core.provider.sources.fishaudio_tts_api_source import ProviderFishAudioTTSAPI as Fish
        network=AsyncMock(return_value='x.wav');p=provider('s2-pro')
        with patch.object(Fish,'get_audio',network):
            restore=install_fish_guard(SimpleNamespace(_cfg=lambda k,d:d))
            try:
                p.get_audio=lambda text:Fish.get_audio(p,text)
                audio,meta=await synthesize(p,'嗨')
            finally:restore()
        self.assertIsNone(audio);network.assert_not_awaited();self.assertEqual(meta['error'],'model_header_mismatch')

    async def test_interleaved_guard_unload_is_transparent(self):
        from astrbot.core.provider.sources.fishaudio_tts_api_source import ProviderFishAudioTTSAPI as Fish
        network=AsyncMock(return_value='x.wav');p=provider()
        with patch.object(Fish,'get_audio',network):
            first=install_fish_guard(SimpleNamespace(_cfg=lambda k,d:d));second=install_fish_guard(SimpleNamespace(_cfg=lambda k,d:d))
            first()
            try:self.assertEqual(await Fish.get_audio(p,'嗨'),'x.wav')
            finally:second()
        self.assertEqual(network.await_count,1)
