import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_maintenance import ROOT  # Framework imports stay inside the test sandbox.
from aiocqhttp import CQHttp
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import (
    ReplyReviewer, ReviewUnavailable, OutboundGuard, install_outbound_guard,
    sent_text, bind_request, request_scope,
    delivery_policy,
    approve_once,
)
from data.plugins.astrbot_plugin_proactive_agent.meme_memory import MemeMemory


def encoded(verdict, replacement=""):
    return json.dumps({"pass": verdict, "natural": verdict, "chat_fit": verdict,
                       "reason": "test", "replacement": replacement}, ensure_ascii=False)


class Reviews(unittest.IsolatedAsyncioTestCase):
    async def test_rewrite_is_independently_rechecked(self):
        generate = AsyncMock(side_effect=[encoded(False, "别急，先看第一步。"), encoded(True)])
        reviewer = ReplyReviewer(generate, lambda *a: None)
        result = await reviewer.review("首先其次总结公式", origin="chat", recent=["怎么算"])
        self.assertEqual(result, "别急，先看第一步")
        self.assertEqual(generate.await_count, 2)
        self.assertEqual(generate.call_args.args[1]["待发文字"], result)

    async def test_rejected_rewrites_and_invalid_output_fail_closed(self):
        for outputs in [[encoded(False, "换一句")] * 3, ["不是JSON"], [json.dumps({"pass": "true"})]]:
            reviewer = ReplyReviewer(AsyncMock(side_effect=outputs), lambda *a: None)
            with self.assertRaises(ReviewUnavailable):
                await reviewer.review("原始长代码", origin="chat")

    async def test_timeout_blocks_and_cancellation_propagates(self):
        async def slow(*a):
            await asyncio.sleep(10)
        with self.assertRaises(ReviewUnavailable):
            await ReplyReviewer(slow, lambda *a: None, timeout=0.01).review("原文", origin="chat")
        generate = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await ReplyReviewer(generate, lambda *a: None).review("原文", origin="chat")

    async def test_management_is_reviewed_but_numbers_never_rewritten(self):
        reviewer = ReplyReviewer(AsyncMock(return_value=encoded(False, "都开着呢")), lambda *a: None)
        with self.assertRaises(ReviewUnavailable):
            await reviewer.review("群=禁止;间隔=60", origin="chat", mode="management")

    async def test_concurrent_request_context_is_task_scoped(self):
        async def task(origin, secret, management):
            bind_request(origin, [secret], management)
            await asyncio.sleep(0)
            self.assertEqual(request_scope(origin)["recent"], [secret])
            return request_scope(origin)["management"]
        self.assertEqual(await asyncio.gather(task("same", "A", True), task("same", "B", False)), [True, False])


class Sending(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = object.__new__(CQHttp)
        self.transport = AsyncMock(return_value={"message_id": 1})
        self.bot._api = SimpleNamespace(call_action=self.transport)
        self.plugin = SimpleNamespace(_platform=lambda: SimpleNamespace(bot=self.bot),
            _cfg=lambda k, d: d, review_context=lambda origin: ["当前聊天"], review_mode=lambda origin: "chat")
        self.generate = AsyncMock(side_effect=lambda *a: encoded(True))
        self.reviewer = ReplyReviewer(self.generate, lambda *a: None)
        self.restore = install_outbound_guard(self.plugin, self.reviewer)

    async def asyncTearDown(self):
        self.restore()

    async def test_dynamic_direct_and_qzone_all_go_through_review(self):
        await self.bot.send_private_msg(user_id=1, message="私聊")
        await self.bot.send_group_msg(group_id=2, message=[{"type": "text", "data": {"text": "群聊"}}])
        await self.bot.call_action("send_qzone_msg", content="公开动态", ugc_right=1)
        self.assertEqual(self.generate.await_count, 3)
        self.assertEqual(self.generate.call_args.args[1]["同一会话近期消息"], [])
        self.assertEqual(self.transport.call_args.kwargs["ugc_right"], 1)
        self.assertEqual(sent_text("qzone", "公开动态"), "公开动态")

    async def test_image_route_mention_and_caller_payload_preserved(self):
        self.generate.side_effect = [encoded(False, "给你画好啦。"), encoded(True)]
        chain = [{"type": "at", "data": {"qq": "3"}}, {"type": "text", "data": {"text": "冗长说明"}},
                 {"type": "image", "data": {"file": "base64://aW1hZ2U="}}, {"type": "reply", "data": {"id": "4"}}]
        await self.bot.send_private_msg(user_id=1, message=chain)
        sent = self.transport.call_args.kwargs
        self.assertEqual(sent["user_id"], 1)
        self.assertEqual(sent["message"][2], chain[2])
        self.assertEqual(sent["message"][0], chain[0])
        self.assertEqual(chain[1]["data"]["text"], "冗长说明")
        self.assertEqual(sent["message"][1]["data"]["text"], "给你画好啦")
        self.assertEqual(sent_text("aiocqhttp:FriendMessage:1", "冗长说明"), "给你画好啦")

    async def test_forward_nodes_reviewed_and_failed_review_never_sends(self):
        self.generate.side_effect = [encoded(False, "短句"), encoded(True)]
        await self.bot.send_group_forward_msg(group_id=2, messages=[{"type": "node", "data": {
            "name": "大肥龙", "uin": "3", "content": [{"type": "text", "data": {"text": "代码长文"}}]}}])
        self.assertEqual(self.transport.call_args.kwargs["messages"][0]["data"]["content"][0]["data"]["text"], "短句")
        self.transport.reset_mock()
        self.generate.side_effect = RuntimeError("provider unavailable")
        with self.assertRaises(ReviewUnavailable):
            await self.bot.send_group_msg(group_id=2, message="不能未经审阅发送")
        self.transport.assert_not_awaited()

    async def test_non_text_and_read_actions_are_not_rewritten(self):
        await self.bot.get_friend_list()
        await self.bot.send_private_msg(user_id=1, message=[{"type": "image", "data": {"file": "x"}}])
        self.generate.assert_not_awaited()

    async def test_literal_cq_string_does_not_become_new_segment(self):
        await self.bot.send_private_msg(user_id=1, message="[CQ:at,qq=all]", auto_escape=True)
        self.assertEqual(self.transport.call_args.kwargs["message"], [{"type": "text", "data": {"text": "[CQ:at,qq=all]"}}])

    async def test_policy_change_during_review_blocks_actual_send(self):
        check = AsyncMock(side_effect=RuntimeError("quiet time began"))
        with delivery_policy(check):
            with self.assertRaises(RuntimeError):
                await self.bot.send_group_msg(group_id=2, message="主动发言")
        self.generate.assert_awaited_once()
        check.assert_awaited_once()
        self.transport.assert_not_awaited()
        await self.bot.send_group_msg(group_id=2, message="普通回复")
        self.transport.assert_awaited_once()

    async def test_approval_is_exact_text_same_route_and_consumed_once(self):
        approve_once("aiocqhttp:FriendMessage:1", "已审阅的文本")
        await self.bot.send_private_msg(user_id=1, message="已审阅的文本")
        self.generate.assert_not_awaited()
        await self.bot.send_private_msg(user_id=1, message="已审阅的文本")
        self.generate.assert_awaited_once()
        approve_once("aiocqhttp:FriendMessage:1", "已审阅的文本")
        await self.bot.send_private_msg(user_id=1, message="插件改动后的文本")
        self.assertEqual(self.generate.await_count, 2)


class ConversationHistory(unittest.IsolatedAsyncioTestCase):
    async def test_final_history_contains_reviewed_text_and_images_survive(self):
        from test_maintenance import Main
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Plain, Image
        from astrbot.core.provider.entities import LLMResponse
        from astrbot.core.agent.message import Message, TextPart
        plugin = object.__new__(Main)
        plugin._reviewer = ReplyReviewer(AsyncMock(side_effect=[encoded(False, "短句"), encoded(True)]), lambda *a: None)
        plugin.review_context = lambda origin: ["你好"]
        plugin.review_mode = lambda origin: "chat"
        event = SimpleNamespace(unified_msg_origin="aiocqhttp:FriendMessage:1", get_group_id=lambda: "",
            get_sender_id=lambda: "1", get_platform_id=lambda: "aiocqhttp", stop_event=lambda: self.fail("should pass"))
        response = LLMResponse(role="assistant", completion_text="长篇", result_chain=MessageChain([Plain("长篇"), Image(file="https://example.org/image.png")]))
        context = SimpleNamespace(messages=[Message(role="assistant", content=[TextPart(text="长篇")])])
        await plugin.review_agent_reply(event, context, response)
        self.assertEqual(response.completion_text, "短句")
        self.assertEqual(context.messages[-1].content[0].text, "短句")
        self.assertEqual(response.result_chain.chain[1].file, "https://example.org/image.png")

    async def test_failed_hook_stops_event_instead_of_leaking_original(self):
        from test_maintenance import Main
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Plain
        from astrbot.core.provider.entities import LLMResponse
        plugin = object.__new__(Main)
        plugin._reviewer = ReplyReviewer(AsyncMock(side_effect=RuntimeError), lambda *a: None)
        plugin.review_context = lambda origin: []
        plugin.review_mode = lambda origin: "chat"
        stopped = []
        event = SimpleNamespace(unified_msg_origin="chat", stop_event=lambda: stopped.append(True))
        response = LLMResponse(role="assistant", completion_text="不该发送", result_chain=MessageChain([Plain("不该发送")]))
        await plugin.review_agent_reply(event, SimpleNamespace(messages=[]), response)
        self.assertTrue(stopped)
        self.assertEqual(response.completion_text, "")
        self.assertEqual(response.result_chain.chain, [])


class Memories(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "meme_memory.json"
        self.search = AsyncMock(return_value={"results": [{"title": "公共梗资料", "url": "https://example.org/meme", "snippet": "啊对对对经常带有敷衍反讽语气"}]})
        self.generate = AsyncMock(return_value=json.dumps({"verified": True, "meaning": "敷衍反讽", "usage": "看上下文，不是总在赞同", "examples": ["好好好，你说了算"], "source_ids": [1]}, ensure_ascii=False))
        self.memory = MemeMemory(self.path, self.generate, self.search, lambda *a: None)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_search_save_reload_and_concurrent_dedup(self):
        a, b = await asyncio.gather(self.memory.lookup("啊对对对", "private:1"), self.memory.lookup("啊对对对", "group:2"))
        self.assertEqual({a["status"], b["status"]}, {"learned", "remembered"})
        self.search.assert_awaited_once()
        reloaded = MemeMemory(self.path, self.generate, self.search, lambda *a: None)
        self.assertEqual(reloaded.match("啊对对对全靠你")[0]["meaning"], "敷衍反讽")
        self.assertNotIn("private:1", self.path.read_text(encoding="utf-8"))
        self.assertEqual((await reloaded.lookup("啊对对对", "other"))["status"], "remembered")
        self.assertEqual(self.search.await_count, 1)

    async def test_unknown_and_fabricated_source_ids_never_become_memory(self):
        for result in [{"verified": False}, {"verified": True, "meaning": "瞎编", "source_ids": [999]}]:
            self.memory._retry_after.clear()
            self.generate.return_value = json.dumps(result)
            self.assertEqual((await self.memory.lookup("虚构新梗", "group:1"))["status"], "unverified")
        self.assertEqual(self.memory.entries, {})
        self.assertFalse(self.path.exists())

    async def test_search_failure_backoff_and_stale_refresh(self):
        await self.memory.lookup("啊对对对", "chat")
        self.memory.entries["啊对对对"]["verified_at"] = time.time() - 91 * 86400
        self.assertEqual(self.memory.match("啊对对对"), [])
        self.search.side_effect = TimeoutError
        await self.memory.lookup("啊对对对", "chat")
        await self.memory.lookup("啊对对对", "chat")
        self.assertEqual(self.search.await_count, 2)
        self.assertEqual(self.memory.entries["啊对对对"]["meaning"], "敷衍反讽")

    async def test_auto_detection_ignores_invented_phrases_and_private_ids(self):
        self.generate.return_value = json.dumps({"phrases": ["输入里没有的梗", "QQ123456789"]})
        await self.memory.enrich("QQ123456789", ["私聊内容"], "private:1")
        self.search.assert_not_awaited()

    async def test_auto_detection_actually_searches_and_saves_new_meme(self):
        self.generate.side_effect = [json.dumps({"phrases": ["啊对对对"]}), self.generate.return_value]
        result = await self.memory.enrich("啊对对对，全靠你", [], "chat")
        self.assertEqual(result[0]["status"], "learned")
        self.assertEqual(self.search.call_args.args[0], "啊对对对 梗 含义 用法")

    async def test_forget_persists_and_corrupt_store_is_not_overwritten(self):
        await self.memory.lookup("啊对对对", "chat")
        self.assertTrue(await self.memory.forget("啊对对对"))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["entries"], {})
        self.path.write_text("broken", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            MemeMemory(self.path, self.generate, self.search, lambda *a: None)
        self.assertEqual(self.path.read_text(), "broken")
