from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from test_maintenance import Main
from test_dragon_character import event
from test_conversation import encoded
from data.plugins.astrbot_plugin_proactive_agent.dragon_character import CharacterState, character_note, speech_issues
from data.plugins.astrbot_plugin_proactive_agent.persona_authority import bind_persona, review_persona
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer, request_scope
from data.plugins.astrbot_plugin_proactive_agent.life_state import role_context


class PersonaAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def test_default_counterattack_does_not_force_apology(self):
        state = CharacterState().for_message('g', '1', '你再嘴欠试试，揍你', {}.get)
        self.assertEqual(state['mood'], 'confronted')
        self.assertFalse(state['apology_required'])
        self.assertEqual(speech_issues('嗓门挺大，道理呢', state), [])
        self.assertIn('本轮没有强制认怂要求', character_note(state))

    def test_switching_off_does_not_reuse_legacy_mood(self):
        cache = CharacterState()
        state = cache.for_message('g', '1', '你再嘴欠试试，揍你', {'counterattack_apology_enabled':True}.get)
        self.assertTrue(state['apology_required'])
        self.assertEqual(cache.for_message('g', '1', '今天吃啥', {}.get)['mood'], 'cheeky')
        self.assertFalse(cache.for_message('g', '1', '揍你', {}.get)['apology_required'])

    async def test_selected_persona_is_live_and_session_scoped(self):
        resolve = AsyncMock(side_effect=[('one', {'prompt':'保留锋芒'}, None, None), ('two', {'prompt':'当前已修改的口吻'}, None, None)])
        plugin = SimpleNamespace(context=SimpleNamespace(persona_manager=SimpleNamespace(resolve_selected_persona=resolve), get_config=lambda **kw: {'provider_settings':{'choice':'current'}}))
        req = SimpleNamespace(conversation=SimpleNamespace(persona_id='selected'))
        state = {}
        await bind_persona(plugin, event('你好'), req, state)
        self.assertIn('保留锋芒', review_persona(state))
        self.assertNotIn('保留锋芒', character_note(state))  # no duplicate state JSON
        await bind_persona(plugin, event('你好', sender='222'), req, state)
        self.assertIn('当前已修改的口吻', review_persona(state))
        self.assertEqual(resolve.call_args.kwargs['conversation_persona_id'], 'selected')
        self.assertTrue(resolve.call_args.kwargs['umo'].endswith(':222'))

    async def test_review_uses_current_persona_without_forcing_apology(self):
        state = CharacterState().for_message('g', '1', '你再嘴欠试试，揍你', {}.get)
        state['persona_prompt'] = '毒舌但就事论事，别自动认错'
        generate = AsyncMock(return_value=encoded(True))
        reply = await ReplyReviewer(generate, lambda *a: None).review('嗓门挺大，道理呢', origin='test', character=state)
        self.assertEqual(reply, '嗓门挺大，道理呢')
        self.assertEqual(generate.await_count, 1)
        self.assertIn(state['persona_prompt'], generate.call_args.args[0])

    async def test_proactive_generation_and_review_share_persona(self):
        manager = SimpleNamespace(resolve_selected_persona=AsyncMock(return_value=('p', {'prompt':'当前主动会话人格'}, None, None)))
        plugin = SimpleNamespace(_cfg={}.get, context=SimpleNamespace(persona_manager=manager, get_config=lambda **kw: {}))
        origin = 'p:FriendMessage:123'
        prompt = await role_context(plugin, origin)
        self.assertIn('当前主动会话人格', prompt)
        self.assertEqual(request_scope(origin)['character']['persona_prompt'], '当前主动会话人格')

    async def test_missing_manager_does_not_break_generation(self):
        state = {'persona_prompt':'old'}
        await bind_persona(SimpleNamespace(), event('你好'), SimpleNamespace(), state)
        self.assertNotIn('persona_prompt', state)
