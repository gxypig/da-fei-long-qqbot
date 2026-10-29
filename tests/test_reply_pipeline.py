import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_maintenance import Main
from test_chat_flow import event
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.agent.message import Message, TextPart
from data.plugins.astrbot_plugin_proactive_agent.reply_pipeline import (
    fast_path_issues, explicit_meme_request, current_turn_used_tools, casual_tail_allowed,
)
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import (
    OutboundGuard, bind_request, approve_once, consume_approval,
)


class Classifying(unittest.TestCase):
    def test_new_turn_cannot_reuse_old_success_for_identical_text(self):
        from data.plugins.astrbot_plugin_proactive_agent.conversation_review import _receipt, confirmed_sent
        bind_request('a', ['first'], False)
        _receipt.set({'origin':'a', 'text':'同一句正文'})
        self.assertTrue(confirmed_sent('a', '同一句正文'))
        bind_request('a', ['second'], False)
        self.assertFalse(confirmed_sent('a', '同一句正文'))

    def test_tail_rescue_excludes_tools_high_risk_emotions_and_media(self):
        self.assertTrue(casual_tail_allowed(event('下班奶茶真香'), {'mood':'bold','visual_intent':'none'}))
        for text in ['帮我写Python脚本', '帮我找个视频', '看看你', '典中典是什么梗', '/主动状态', '发布一条消息']:
            state = {'visual_intent':'selfie' if text == '看看你' else 'none'}
            self.assertFalse(casual_tail_allowed(event(text), state))
        self.assertFalse(casual_tail_allowed(event('今天困啦'), {'mood':'serious'}))
        e = event('这个真有趣'); e.get_messages = lambda: [SimpleNamespace()]
        self.assertFalse(casual_tail_allowed(e, {}))

    def test_draft_is_checked_even_after_low_risk_prediction(self):
        for text in ['```python\nprint(1)\n```', '下面是答案：∫x dx', '作为一个AI，我可以帮你', '已经帮你删除文件']:
            self.assertTrue(fast_path_issues(text, '聊聊呗', prediction={'risk': 'low'}))
        self.assertFalse(fast_path_issues('你这班上的，工资还没到手先交给奶茶店了', '下班买杯奶茶',prediction={'risk':'low'}))
        self.assertIn('prediction_unavailable',fast_path_issues('下班了', '下班买杯奶茶'))

    def test_explicit_risks_tools_and_state_changes_are_never_fast_passed(self):
        for prompt in ['帮我写个Python脚本', '证明这道题', '你到底是什么模型']:
            self.assertTrue(fast_path_issues('这题看得本龙头大', prompt))
        for kwargs in [{'used_tools': True}, {'media': 1}, {'mode': 'management'},
                       {'state': {'mood': 'stop'}}, {'state': {'proactive': True}}]:
            self.assertTrue(fast_path_issues('本龙来了', '你好', **kwargs))

    def test_old_tool_history_does_not_force_every_future_chat_through_review(self):
        self.assertFalse(current_turn_used_tools([{'role': 'tool'}, {'role': 'user'}, {'role': 'assistant'}]))
        self.assertTrue(current_turn_used_tools([{'role': 'user'}, {'role': 'assistant', 'tool_calls': ['call']}, {'role': 'tool'}]))

    def test_explicit_meme_requests_keep_foreground_lookup(self):
        for prompt in ['典中典是什么梗', '帮我记住这个梗', '绷不住了是什么意思', '这话出自哪里']:
            self.assertTrue(explicit_meme_request(prompt))
        for prompt in ['这下真绷不住了', '典中典，又让我来收拾残局']:
            self.assertFalse(explicit_meme_request(prompt))

    def test_receipt_is_one_use_and_bound_to_turn_target_text_and_policy(self):
        bind_request('a', ['hi'], False)
        approve_once('a', 'hello', policy_version='v1')
        self.assertTrue(consume_approval('a', 'hello', policy_version='v1'))
        self.assertFalse(consume_approval('a', 'hello', policy_version='v1'))
        for target, text, version in [('b', 'hello', 'v1'), ('a', 'changed', 'v1'), ('a', 'hello', 'v2')]:
            approve_once('a', 'hello', policy_version='v1')
            self.assertFalse(consume_approval(target, text, policy_version=version))
        approve_once('a', 'hello', policy_version='v1')
        bind_request('a', ['next turn'], False)
        self.assertFalse(consume_approval('a', 'hello', policy_version='v1'))


def plugin():
    p = object.__new__(Main)
    p.config = {'latency_optimization_enabled': True, 'review_policy': 'speed_first'}
    p._init_reply_pipeline()
    p._audit = Mock()
    p._reviewer = SimpleNamespace(review=AsyncMock(return_value='本龙投降，别凶'))
    p.review_context = lambda origin: ['下班买杯奶茶']
    p.review_mode = lambda origin: 'chat'
    p._learning = SimpleNamespace(enqueue_review=Mock(), enqueue_meme=Mock(), style_note=lambda _: '')
    return p


class Pipeline(unittest.IsolatedAsyncioTestCase):
    async def test_review_rule_update_invalidates_existing_approval(self):
        from unittest.mock import patch
        from data.plugins.astrbot_plugin_proactive_agent import persona_authority
        p=plugin();bind_request('a',['同一条输入'],False)
        before=p.review_policy_version();approve_once('a','同一段正文',policy_version=before)
        with patch.object(persona_authority,'REVIEW_FUNCTIONAL',persona_authority.REVIEW_FUNCTIONAL+'\n新的事实校验规则'):
            after=p.review_policy_version()
            self.assertNotEqual(before,after)
            self.assertFalse(consume_approval('a','同一段正文',policy_version=after))

    async def test_prediction_uses_pinned_role_once_before_and_after_enrichment(self):
        from data.plugins.astrbot_plugin_proactive_agent.persona_authority import _bound
        binding={'persona_id':'engineer','persona_version':'version-one','persona_status':'selected',
            'persona_prompt':'MASTER','persona_variants':{'chat':'ONE CHAT ROLE','review':'UNUSED REVIEW'*100,'bili':'UNUSED BILI'*100},
            'persona_fallbacks':{'gallery_empty':'UNUSED FALLBACK'}}
        token=_bound.set(binding)
        try:
            for enriched in (False,True):
                p=plugin();p._generate_json=AsyncMock(return_value=json.dumps({'risk':'low','image_ids':[]}))
                e=event('今天不错');e.set_extra('qqbot_character',{'mood':'cheeky','visual_intent':'none',**(binding if enriched else {})})
                await p.prepare_reply_prediction(e)
                payload=p._generate_json.call_args.args[1]
                state=payload['state'];self.assertEqual(state['persona_version'],'version-one')
                self.assertEqual(state['persona_prompt'],'ONE CHAT ROLE');self.assertNotIn('persona_variants',state)
                self.assertNotIn('UNUSED',json.dumps(payload));self.assertIsNotNone(p.reply_prediction(e))
                changed=_bound.set({**binding,'persona_version':'version-two'})
                try:self.assertIsNone(p.reply_prediction(e))
                finally:_bound.reset(changed)
                await p._close_reply_pipeline()
        finally:_bound.reset(token)

    async def test_optional_review_off_does_not_add_risk_only_model_call(self):
        p = plugin(); p.config['reply_review_enabled'] = False
        p._generate_json = AsyncMock()
        self.assertIsNone(p.prepare_reply_prediction(event('下班啦')))
        p._generate_json.assert_not_awaited()

    async def test_missing_prediction_uses_full_review_without_awaiting_prediction(self):
        p = plugin(); e = event('下班买杯奶茶')
        gate = asyncio.Event()
        task = asyncio.create_task(gate.wait())
        e.set_extra('qqbot_reply_prediction', task)
        draft = '你这工资还没到手，先交给奶茶店了'
        p._reviewer.review.return_value=draft
        response = SimpleNamespace(completion_text=draft, result_chain=MessageChain([Plain(draft)]))
        history = SimpleNamespace(messages=[Message(role='user', content='下班买杯奶茶'),
                                            Message(role='assistant', content=[TextPart(text=draft)])])
        bind_request(e.unified_msg_origin, ['下班买杯奶茶'], False)
        try:
            await asyncio.wait_for(p.review_agent_reply(e, history, response), .2)
            self.assertEqual(response.completion_text, draft)
            self.assertEqual(history.messages[-1].content[-1].text, draft)
            p._reviewer.review.assert_awaited_once()
            guard = OutboundGuard(p, p._reviewer)
            actual = await guard.prepare('send_group_msg', {'group_id': '100', 'message': draft})
            self.assertEqual(actual['message'][0]['data']['text'], draft)
            p._reviewer.review.assert_awaited_once()
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def test_high_risk_does_not_skip_review(self):
        p = plugin(); e = event('给我写个Python脚本')
        response = SimpleNamespace(completion_text='这单不接', result_chain=None)
        context = SimpleNamespace(messages=[Message(role='assistant', content='这单不接')])
        await p.review_agent_reply(e, context, response)
        p._reviewer.review.assert_awaited_once()
        self.assertEqual(response.completion_text, '本龙投降，别凶')

    async def test_local_meme_hit_never_awaits_detection_or_learning(self):
        p = plugin(); p.config.update(meme_context_enabled=True, meme_memory_enabled=True)
        e = event('典中典'); p._recent_messages = {}; p._meme_hints = SimpleNamespace(match=lambda _: [])
        p._memory = SimpleNamespace(contextual=lambda *_: [{'phrase': '典中典', 'meaning': '非常典型的套路'}])
        p._meme_prefetch = SimpleNamespace(get=AsyncMock(side_effect=AssertionError('foreground enrichment')))
        p.prepare_reply_prediction = Mock(return_value=None)
        req = SimpleNamespace(prompt='典中典', system_prompt='原人设')
        await asyncio.wait_for(p.enrich_conversation(e, req), .2)
        self.assertIn('非常典型的套路', req.system_prompt)
        p._meme_prefetch.get.assert_not_awaited()
        p._learning.enqueue_meme.assert_called_once()

    async def test_changed_final_payload_is_independently_reviewed(self):
        p = plugin(); bind_request('aiocqhttp:GroupMessage:100', ['hi'], False)
        approve_once('aiocqhttp:GroupMessage:100', '原稿', policy_version=p.review_policy_version())
        await OutboundGuard(p, p._reviewer).prepare('send_group_msg', {'group_id': '100', 'message': '被其他插件修改'})
        p._reviewer.review.assert_awaited_once()

    async def test_prediction_is_coalesced_and_candidates_are_validated(self):
        p = plugin(); e = event('今天赢啦')
        p._generate_json = AsyncMock(return_value=json.dumps({'risk': 'low', 'image_ids': ['not-a-candidate']}))
        first = p.prepare_reply_prediction(e, [{'id': 'happy', 'tags': ['开心']}])
        self.assertIs(first, p.prepare_reply_prediction(e))
        answer = await first
        self.assertEqual(answer['risk'], 'unknown')
        self.assertEqual(answer['image_ids'], [])
        p._generate_json.assert_awaited_once()
        p.config['review_policy'] = 'all'
        self.assertIsNone(p.reply_prediction(e))
