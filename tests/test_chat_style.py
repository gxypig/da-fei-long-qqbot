import json
import unittest
from unittest.mock import AsyncMock

from test_maintenance import ROOT
from test_conversation import encoded
from data.plugins.astrbot_plugin_proactive_agent.chat_style_policy import output_issues
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer, ReviewUnavailable


class ReviewContract(unittest.IsolatedAsyncioTestCase):
    async def test_recent_assistant_context_is_bounded_and_supplied_to_review(self):
        generate = AsyncMock(return_value=encoded(True))
        previous = [f"回复{i}" + "长" * 250 for i in range(6)]
        await ReplyReviewer(generate, lambda *a: None).review("这次换个说法。", origin="p", prior_replies=previous)
        payload = generate.call_args.args[1]
        rows = payload["同一会话最近机器人回复（避免重复句式）"]
        self.assertEqual(rows, [text[:200] for text in previous[-3:]])

    async def test_persona_review_can_correct_technical_reply_without_keyword_ban(self):
        draft='这个脚本可以改文件名'
        generate=AsyncMock(side_effect=[encoded(False,'这个忙得先看你的文件规则'),encoded(True)])
        result=await ReplyReviewer(generate,lambda *a:None).review(draft,origin='p',character={'persona_status':'selected'})
        self.assertEqual(result,'这个忙得先看你的文件规则');self.assertEqual(generate.await_count,2)

    async def test_repeated_rejection_fails_closed(self):
        generate=AsyncMock(return_value=encoded(False,'仍然不合适'))
        reviewer=ReplyReviewer(generate,lambda *a:None)
        with self.assertRaises(ReviewUnavailable):await reviewer.review('原稿',origin='p')
        self.assertEqual(generate.await_count,3)

    async def test_approved_necessary_help_is_not_overridden_by_legacy_regex(self):
        for draft in ['答案是1/2，24种排列里有12种符合','把文件路径发来才能检查','```python\nprint(1)\n```']:
            reviewer=ReplyReviewer(AsyncMock(return_value=encoded(True)),lambda *a:None)
            self.assertEqual(await reviewer.review(draft,origin='p',character={'persona_status':'selected'}),draft)

    async def test_missing_or_negative_quality_judgement_cannot_be_pass(self):
        for verdict in ({"pass": True}, {"pass": True, "natural": "true", "chat_fit": True}):
            reviewer = ReplyReviewer(AsyncMock(return_value=json.dumps(verdict)), lambda *a: None)
            with self.assertRaises(ReviewUnavailable):
                await reviewer.review("普通句子", origin="p")
        verdict = {"pass": True, "natural": True, "chat_fit": False,
                   "reason": "仍然答题", "replacement": "好家伙，聊天还能突然开考啊。"}
        generate = AsyncMock(side_effect=[json.dumps(verdict), encoded(True)])
        result = await ReplyReviewer(generate, lambda *a: None).review("正确而很短的技术答案", origin="p")
        self.assertEqual(result, "好家伙，聊天还能突然开考啊")
        self.assertEqual(generate.await_count, 2)

    async def test_management_is_preserved_and_still_model_reviewed(self):
        draft = "## 状态\n管理员：123456789\n`/主动间隔 30`\n唤醒：30分钟"
        generate = AsyncMock(return_value=encoded(True))
        result = await ReplyReviewer(generate, lambda *a: None).review(draft, origin="p", mode="management")
        self.assertEqual(result, draft)
        generate.assert_awaited_once()

    async def test_truthful_identity_simple_math_and_help_are_not_blocked(self):
        for incoming, draft in [
            ("认真问，你是不是机器人？", "是机器人，用大肥龙这个角色陪你聊天。"),
            ("17加25是多少", "42呀。"),
            ("我今天很难过", "想说的话我听着，不用急着调整好情绪。"),
            ("我被困住了怎么联系救援", "先拨打119，告诉接线员你的位置。"),
        ]:
            self.assertEqual(output_issues(draft, recent=[incoming]), [])
            generate = AsyncMock(return_value=encoded(True))
            self.assertEqual(await ReplyReviewer(generate, lambda *a: None).review(draft, origin="p", recent=[incoming]), draft.removesuffix("。"))


class OutputGuards(unittest.TestCase):
    def test_artifacts_are_rejected_but_normal_chat_is_not(self):
        for draft in ["print('hello')", "print: `hello`", "计算 \\frac{1}{2}", "∫ f(x) dx", "## 答案", "**答案**", "Get-ChildItem '*.zip'", "SELECT * FROM users"]:
            self.assertTrue(output_issues(draft), draft)
        for draft in ["别突然给我加试啊。", "今晚8点见", "这个网站 https://example.org/ 看着就眼熟。", "AI这个话题先放一边。"]:
            self.assertFalse(output_issues(draft), draft)
