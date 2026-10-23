import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from test_maintenance import Main
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from data.plugins.astrbot_plugin_proactive_agent.auxiliary_lane import AuxiliaryLane
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import approve_once, OutboundGuard


class FastAuxiliary(unittest.IsolatedAsyncioTestCase):
    def provider(self, extra=None, model='gpt-6.1-sol'):
        provider = object.__new__(ProviderOpenAIOfficial)
        provider.provider_config = {'model': model, 'custom_extra_body': extra or {}}
        provider.client = object()
        return provider

    async def test_only_auxiliary_copy_changes_main_configuration_and_pool_untouched(self):
        original = self.provider({'temperature': .5})
        before = copy.deepcopy(original.provider_config)
        used = []
        async def call(provider, **kwargs):
            used.append(provider)
            self.assertIs(provider.client, original.client)
            return SimpleNamespace(completion_text='{"pass":true}')
        with patch.object(ProviderOpenAIOfficial, 'text_chat', call):
            result = await AuxiliaryLane().generate(original, prompt='test', request_max_retries=1)
        self.assertEqual(original.provider_config, before)
        self.assertIsNot(used[0], original)
        self.assertEqual(used[0].provider_config['custom_extra_body']['reasoning_effort'], 'low')
        self.assertEqual(result.completion_text, '{"pass":true}')

    async def test_other_models_providers_and_user_reasoning_overrides_keep_original(self):
        seen = []
        async def call(provider, **kwargs):
            seen.append(provider)
        with patch.object(ProviderOpenAIOfficial, 'text_chat', call):
            for provider in (self.provider(model='other-model'), self.provider({'reasoning_effort': 'high'})):
                await AuxiliaryLane().generate(provider)
                self.assertIs(seen[-1], provider)
        other = SimpleNamespace(provider_config={}, text_chat=AsyncMock(return_value='ok'))
        self.assertEqual(await AuxiliaryLane().generate(other), 'ok')

    async def test_rejected_parameter_falls_back_once_without_retrying_timeouts(self):
        original = self.provider()
        lane = AuxiliaryLane()
        used = []
        class Rejected(Exception):
            status_code = 400
        async def call(provider, **kwargs):
            used.append(provider)
            if provider is not original:
                raise Rejected('unsupported reasoning_effort')
            return 'original result'
        with patch.object(ProviderOpenAIOfficial, 'text_chat', call):
            self.assertEqual(await lane.generate(original), 'original result')
            self.assertEqual(await lane.generate(original), 'original result')
        self.assertEqual(len(used), 3)
        self.assertIs(used[-1], original)
        broken = AsyncMock(side_effect=TimeoutError())
        with patch.object(ProviderOpenAIOfficial, 'text_chat', broken):
            with self.assertRaises(TimeoutError):
                await AuxiliaryLane().generate(original)
        broken.assert_awaited_once()

    async def test_switch_off_keeps_original_generate_and_audit(self):
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text='{}')))
        plugin = object.__new__(Main)
        plugin.config = {'review_low_latency': False}
        plugin.context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider))
        plugin._audit = lambda *a: None
        self.assertEqual(await plugin._generate_json('test', {}, 'origin'), '{}')
        provider.text_chat.assert_awaited_once()

    async def test_empty_parts_reuse_exact_review_once_but_real_edits_still_review(self):
        reviewer = SimpleNamespace(review=AsyncMock(return_value='重新审查过的文字'))
        plugin = SimpleNamespace(_cfg=lambda k,d:d, review_context=lambda o:[], review_mode=lambda o:'chat')
        guard = OutboundGuard(plugin, reviewer)
        origin = 'aiocqhttp:FriendMessage:20'
        params = {'user_id':20, 'message':[{'type':'text','data':{'text':'已审过的正文'}},
                                         {'type':'image','data':{'file':'image'}},
                                         {'type':'text','data':{'text':''}}]}
        approve_once(origin, '已审过的正文')
        self.assertEqual(await guard.prepare('send_private_msg', params), params)
        reviewer.review.assert_not_awaited()
        await guard.prepare('send_private_msg', params)
        reviewer.review.assert_awaited_once()
        approve_once(origin, '已审过的正文')
        params['message'][-1]['data']['text'] = '另一插件追加的文字'
        await guard.prepare('send_private_msg', params)
        self.assertEqual(reviewer.review.await_count, 2)
