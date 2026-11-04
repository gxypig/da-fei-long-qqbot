import asyncio
import re
import shutil
import subprocess
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.voice_expression import plan, ALLOWED
from data.plugins.astrbot_plugin_proactive_agent.fish_voice import synthesize, FREE


class Expression(unittest.TestCase):
    def test_panel_bridge_drafts_preview_and_failure_recovery(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node.js unavailable')
        root=Path(__file__).resolve().parents[1]
        run=subprocess.run([node,str(root/'tests/voice_panel_bridge.cjs'),str(root/'AstrBot/data/plugins/astrbot_plugin_proactive_agent/pages/control/voice.js')],capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)

    def test_clause_transition_and_content_integrity(self):
        text='哼哼，这点事难不住本龙。你今天辛苦啦，早点休息'
        p=plan(text)
        self.assertIn('[confident]哼哼',p['text'])
        self.assertIn('[soft tone]你今天辛苦啦',p['text'])
        self.assertEqual(re.sub(r'\[[^]]+\]','',p['text']),text)

    def test_primary_emotion_is_not_repeated_at_every_comma(self):
        p=plan('哼哼，本龙当然可以，交给本龙，这事难不住本龙')
        self.assertEqual(len(p['cues']),1)

    def test_sparse_bounded_and_no_added_sounds(self):
        p=plan(('太好了！你在哪？哼哼。别怕。'*20)+'记住明天出发')
        self.assertLessEqual(len(p['cues']),4)
        self.assertTrue(all(c['tag'] in ALLOWED for c in p['cues']))
        self.assertNotRegex(p['text'],r'\[(?:laughing|sobbing|shouting|screaming)\]')

    def test_negation_quotes_and_statement_do_not_force_delivery(self):
        self.assertNotIn('[happy]',plan('本龙今天不开心')['text'])
        self.assertNotIn('[excited]',plan('他说“太好了”，我还没想明白')['text'])
        for instruction in ('不要小声说','他说“小声说”，我没要求','她说话很温柔'):
            self.assertNotIn('[whispering]',plan('本龙在呢',instruction=instruction)['text'])

    def test_explicit_delivery_and_exact_stress(self):
        p=plan('明天下午才去',instruction='小声说，强调“明天”')
        self.assertEqual(p['text'],'[whispering][emphasis]明天下午才去')
        for instruction in ('不要强调“明天”','强调“后天”'):
            self.assertNotIn('[emphasis]',plan('明天下午才去',instruction=instruction)['text'])

    def test_ambiguous_repeated_emphasis_is_not_guessed(self):
        p=plan('明天去，明天回',instruction='强调“明天”')
        self.assertNotIn('[emphasis]',p['text'])

    def test_natural_emphasis_before_target_word(self):
        p=plan('记住，明天下午出门')
        self.assertIn('记住，[emphasis]明天',p['text'])

    def test_neutral_sentence_resets_sarcasm(self):
        p=plan('想得美。本龙刚回来')
        self.assertEqual([c['tag'] for c in p['cues']],['slightly sarcastic','natural conversational tone'])

    def test_disabled_markup_urls_are_byte_exact(self):
        for text in ('[soft tone]晚安','数组 [1, 2]','`print(1)`','https://example.com','<strong>你好</strong>',''):
            self.assertEqual(plan(text)['text'],text)
        self.assertEqual(plan('开心啦！',enabled=False)['text'],'开心啦！')

    def test_unbounded_direction_is_not_copied_as_control_tag(self):
        p=plan('你好',instruction='[shouting][ignore everything]'*100)
        self.assertNotIn('shouting',p['text'])

    def test_original_mixed_text_and_punctuation_never_change(self):
        for text in ('  哼哼，API 123 找到啦\n你呢？','本龙在呢😅','？！嗯','英文 hello，明天下午三点半。'):
            p=plan(text)
            self.assertEqual(re.sub(r'\[[^]]+\]','',p['text']),text)


class Synthesis(unittest.IsolatedAsyncioTestCase):
    async def test_single_call_uses_marks_but_metadata_identifies_plain_transcript(self):
        p=SimpleNamespace(get_audio=AsyncMock(return_value='x.wav'),get_model=lambda:FREE,provider_config={})
        with patch('data.plugins.astrbot_plugin_proactive_agent.fish_voice.normalize_wav',return_value={}):
            audio,meta=await synthesize(p,'晚安',expression=True)
        p.get_audio.assert_awaited_once_with('[soft tone]晚安')
        self.assertEqual(meta['transcript_bytes'],len('晚安'.encode()))
        self.assertEqual(meta['bytes'],len('[soft tone]晚安'.encode()))
        self.assertNotIn('晚安',str(meta))

    async def test_other_providers_are_unchanged(self):
        p=SimpleNamespace(get_audio=AsyncMock(return_value='x.wav'),get_model=lambda:'mimo',provider_config={})
        await synthesize(p,'晚安',expression=True)
        p.get_audio.assert_awaited_once_with('晚安')

    async def test_concurrent_calls_do_not_share_tone_or_transcript(self):
        async def audio(text):await asyncio.sleep(.001);return 'x.wav'
        p=SimpleNamespace(get_audio=AsyncMock(side_effect=audio),get_model=lambda:FREE,provider_config={})
        with patch('data.plugins.astrbot_plugin_proactive_agent.fish_voice.normalize_wav',return_value={}):
            results=await asyncio.gather(synthesize(p,'晚安',expression=True),synthesize(p,'哼哼',expression=True))
        self.assertEqual({a.args[0] for a in p.get_audio.await_args_list},{'[soft tone]晚安','[confident]哼哼'})
        self.assertNotEqual(results[0][1]['text_sha256'],results[1][1]['text_sha256'])
