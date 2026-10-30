import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_maintenance import ROOT, RetrievalMixin, Main
from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import Image
from astrbot.core.message.message_event_result import ResultContentType
from data.plugins.astrbot_plugin_proactive_agent.meme_memory import MemeMemory, DETECT_SYSTEM
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import request_scope
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.retrieval import REPLY_PREFETCH_KEY
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.common import SKIP_PROACTIVE_EMOJI_EXTRA_KEY


class MemoryConcurrency(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.json"
        self.search = AsyncMock(return_value={"results": [{"url": "https://example.org/meme", "snippet": "公共资料"}]})
        self.generate = AsyncMock(return_value=json.dumps({"verified": True, "meaning": "测试公开含义", "source_ids": [1]}))
        self.memory = MemeMemory(self.path, self.generate, self.search, lambda *args: None)

    async def asyncTearDown(self):
        # Windows antivirus can briefly retain a just-written directory during cleanup.
        for attempt in range(3):
            try:
                self.temp.cleanup()
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (32, 145) or attempt == 2:
                    raise
                await asyncio.sleep(.05)

    async def test_different_memes_overlap_and_cached_hits_do_not_wait(self):
        await self.memory.lookup("已有梗", "a")
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def search(query, origin):
            calls.append(query)
            if len(calls) == 2:
                started.set()
            await release.wait()
            return self.search.return_value
        self.search.side_effect = search
        tasks = [asyncio.create_task(self.memory.lookup(term, "a")) for term in ("新梗甲", "新梗乙")]
        try:
            await asyncio.wait_for(started.wait(), 1)
            cached = await asyncio.wait_for(self.memory.lookup("已有梗", "b"), .2)
            self.assertEqual(cached["status"], "remembered")
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["entries"]), 3)
        self.assertFalse(self.memory._key_locks)

    async def test_two_extracted_memes_both_finish_before_generation_continues(self):
        reached, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def search(*args):
            nonlocal count
            count += 1
            if count == 2:
                reached.set()
            await release.wait()
            return self.search.return_value
        async def generate(system, *args):
            return json.dumps({"phrases": ["新梗甲", "新梗乙"]}) if system == DETECT_SYSTEM else self.generate.return_value
        self.search.side_effect, self.generate.side_effect = search, generate
        task = asyncio.create_task(self.memory.enrich("新梗甲，新梗乙", [], "a"))
        try:
            await asyncio.wait_for(reached.wait(), 1)
            self.assertFalse(task.done())
        finally:
            release.set()
        self.assertEqual([row["status"] for row in await task], ["learned", "learned"])

    async def test_detection_cache_is_bounded_expiring_and_context_scoped(self):
        self.generate.return_value = '{"phrases":[]}'
        await asyncio.gather(*(self.memory.enrich("你好", ["上文"], "a") for _ in range(3)))
        self.assertEqual(self.generate.await_count, 1)
        for text, recent, origin in [("你好", ["上文"], "b"), ("你好", ["另一上文"], "a"), ("晚上好", ["上文"], "a")]:
            await self.memory.enrich(text, recent, origin)
        self.assertEqual(self.generate.await_count, 4)
        for key, (_, phrases) in self.memory._detections.items():
            self.memory._detections[key] = (0, phrases)
        await self.memory.enrich("你好", ["上文"], "a")
        self.assertEqual(self.generate.await_count, 5)
        for i in range(130):
            await self.memory.enrich(f"你好{i}", [], "a")
        self.assertLessEqual(len(self.memory._detections), 128)
        self.assertFalse(self.path.exists())

    async def test_cancelled_same_key_waiter_does_not_break_dedup_or_forget(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def search(*args):
            entered.set()
            await release.wait()
            return self.search.return_value
        self.search.side_effect = search
        first = asyncio.create_task(self.memory.lookup("新梗甲", "a"))
        await entered.wait()
        waiter = asyncio.create_task(self.memory.lookup("新梗甲", "b"))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        forget = asyncio.create_task(self.memory.forget("新梗甲"))
        release.set()
        await first
        self.assertTrue(await forget)
        self.assertFalse(self.memory.entries)
        self.assertFalse(self.memory._key_locks)
        self.search.assert_awaited_once()

    async def test_cancelled_lookup_can_retry_and_failed_forget_rolls_back(self):
        self.search.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.memory.lookup("新梗甲", "a")
        self.assertFalse(self.memory._retry_after)
        self.search.side_effect = None
        await self.memory.lookup("新梗甲", "a")
        self.memory._save = Mock(side_effect=OSError)
        with self.assertRaises(OSError):
            await self.memory.forget("新梗甲")
        self.assertIn("新梗甲", self.memory.entries)


def emoji_fixture():
    plugin = RetrievalMixin()
    cfg = {"enabled": True, "retrieval_mode": "bot_reply_serial", "meme_only": True,
           "analysis_provider_id": "", "embed_in_conversation": True}
    plugin._proactive_emoji_config = lambda: copy.deepcopy(cfg)
    plugin._log_proactive_emoji_debug = lambda *a: None
    plugin._log_proactive_emoji_error_debug = lambda *a: None
    plugin._proactive_emoji_probability_hit = Mock(return_value=True)
    plugin._sync_library = AsyncMock()
    candidates = [{"id": "meme", "tags": ["开心"], "rel_path": "meme.png"}]
    plugin._proactive_emoji_candidates = lambda *a: copy.deepcopy(candidates)
    plugin._analyze_proactive_emoji = AsyncMock(return_value={"matched": True})
    plugin._select_proactive_emoji = lambda decision, candidates, cfg: candidates[0]
    plugin._apply_proactive_emoji_image = AsyncMock()
    extra = {}
    result = MessageEventResult().message("说得对")
    result.set_result_content_type(ResultContentType.LLM_RESULT)
    event = SimpleNamespace(get_extra=lambda key, default=None: extra.get(key, default),
                            set_extra=lambda key, val: extra.__setitem__(key, val), get_result=lambda: result)
    response = SimpleNamespace(result_chain=None, completion_text="说得对")
    return plugin, event, response, result, cfg, candidates


class EmojiConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_matching_starts_during_review_and_sends_only_when_decorated(self):
        plugin, event, response, result, *_ = emoji_fixture()
        entered, release = asyncio.Event(), asyncio.Event()
        async def analyze(*a, **kw):
            entered.set()
            await release.wait()
            return {"matched": True}
        plugin._analyze_proactive_emoji.side_effect = analyze
        await plugin._prefetch_reply_emoji(event, response)
        await asyncio.wait_for(entered.wait(), 1)
        plugin._apply_proactive_emoji_image.assert_not_awaited()
        release.set()
        await plugin._maybe_append_proactive_emoji(event)
        plugin._analyze_proactive_emoji.assert_awaited_once()
        plugin._proactive_emoji_probability_hit.assert_called_once()
        plugin._apply_proactive_emoji_image.assert_awaited_once()
        self.assertIsNone(event.get_extra(REPLY_PREFETCH_KEY))

    async def test_rewritten_text_is_rematched_without_second_probability_roll(self):
        plugin, event, response, result, *_ = emoji_fixture()
        await plugin._prefetch_reply_emoji(event, response)
        await asyncio.sleep(0)
        result.chain[0].text = "我并不同意"
        await plugin._maybe_append_proactive_emoji(event)
        self.assertEqual(plugin._analyze_proactive_emoji.await_count, 2)
        self.assertEqual(plugin._analyze_proactive_emoji.call_args.args[1], "我并不同意")
        plugin._proactive_emoji_probability_hit.assert_called_once()

    async def test_library_changes_invalidate_prefetched_match(self):
        plugin, event, response, result, cfg, candidates = emoji_fixture()
        await plugin._prefetch_reply_emoji(event, response)
        await asyncio.sleep(0)
        candidates[0]["tags"] = ["新标签"]
        await plugin._maybe_append_proactive_emoji(event)
        self.assertEqual(plugin._analyze_proactive_emoji.await_count, 2)
        self.assertEqual(plugin._analyze_proactive_emoji.call_args.args[2][0]["tags"], ["新标签"])

    async def test_missed_roll_stays_missed_and_failure_falls_back(self):
        plugin, event, response, *_ = emoji_fixture()
        plugin._proactive_emoji_probability_hit.return_value = False
        await plugin._prefetch_reply_emoji(event, response)
        await plugin._maybe_append_proactive_emoji(event)
        plugin._analyze_proactive_emoji.assert_not_awaited()
        plugin._proactive_emoji_probability_hit.assert_called_once()
        plugin, event, response, *_ = emoji_fixture()
        plugin._analyze_proactive_emoji.side_effect = [RuntimeError("temporary"), {"matched": True}]
        await plugin._prefetch_reply_emoji(event, response)
        await asyncio.sleep(0)
        await plugin._maybe_append_proactive_emoji(event)
        plugin._apply_proactive_emoji_image.assert_awaited_once()

    async def test_generation_images_and_disabled_or_blocked_reply_never_get_extra_emoji(self):
        for reason in ("generation", "image", "disabled", "blocked"):
            plugin, event, response, result, cfg, _ = emoji_fixture()
            await plugin._prefetch_reply_emoji(event, response)
            if reason == "generation":
                event.set_extra(SKIP_PROACTIVE_EMOJI_EXTRA_KEY, True)
            elif reason == "image":
                result.chain.append(Image(file="https://example.org/generated.png"))
            elif reason == "disabled":
                cfg["enabled"] = False
            else:
                result.chain.clear()
            await plugin._maybe_append_proactive_emoji(event)
            plugin._apply_proactive_emoji_image.assert_not_awaited()
            self.assertIsNone(event.get_extra(REPLY_PREFETCH_KEY))

    async def test_overload_uses_original_serial_path_and_cancel_cleans_task(self):
        plugin, event, response, *_ = emoji_fixture()
        plugin._reply_prefetch_tasks = {object(), object()}
        await plugin._prefetch_reply_emoji(event, response)
        self.assertIsNone(event.get_extra(REPLY_PREFETCH_KEY))
        await plugin._maybe_append_proactive_emoji(event)
        plugin._apply_proactive_emoji_image.assert_awaited_once()
        plugin, event, response, *_ = emoji_fixture()
        started = asyncio.Event()
        async def slow(*a, **kw):
            started.set()
            await asyncio.Future()
        plugin._analyze_proactive_emoji.side_effect = slow
        await plugin._prefetch_reply_emoji(event, response)
        await started.wait()
        pending = asyncio.create_task(plugin._maybe_append_proactive_emoji(event))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(plugin._reply_prefetch_tasks)
        plugin._apply_proactive_emoji_image.assert_not_awaited()


class CommandReview(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_review_stops_framework_and_cancels_matching(self):
        plugin = object.__new__(Main)
        plugin.config={"reply_review_enabled":True,"review_policy":"all"}
        plugin._reviewer = SimpleNamespace(review=AsyncMock(side_effect=asyncio.CancelledError))
        plugin.review_context = lambda origin: []
        plugin.review_mode = lambda origin: "chat"
        cleanup = AsyncMock()
        event = SimpleNamespace(unified_msg_origin="private:1", stop_event=Mock(),
                                get_extra=lambda key: cleanup if key=="qqbot_cancel_reply_preparation" else None, set_extra=lambda *a:None,get_message_str=lambda:"你好")
        response = SimpleNamespace(result_chain=None, completion_text="不能发送的原稿")
        with self.assertRaises(asyncio.CancelledError):
            await plugin.review_agent_reply(event, None, response)
        event.stop_event.assert_called_once()
        cleanup.assert_awaited_once()
        self.assertEqual(response.completion_text, "")

    async def test_only_framework_accepted_command_uses_management_review(self):
        from astrbot.core.star.filter.command import CommandFilter
        plugin = object.__new__(Main)
        plugin._recent_messages = {}
        extra = {"activated_handlers": [SimpleNamespace(event_filters=[object.__new__(CommandFilter)])]}
        event = SimpleNamespace(unified_msg_origin="private:1", get_message_str=lambda: "/help",
            get_sender_id=lambda: "1", get_self_id=lambda: "2", get_extra=lambda k: extra.get(k),
            set_extra=lambda k, v: extra.__setitem__(k, v))
        await plugin.track_session(event)
        self.assertTrue(request_scope("private:1")["management"])
        extra["activated_handlers"] = []
        await plugin.track_session(event)
        self.assertFalse(request_scope("private:1")["management"])
