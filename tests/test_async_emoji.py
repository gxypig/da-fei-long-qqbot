"""No live QQ/model traffic: exercise the opt-in hooks with gated local doubles."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

# Reuse the suite's import sandbox: AstrBot must not initialize in live data.
from test_maintenance import ROOT
from astrbot.api.event import MessageEventResult
from astrbot.api.message_components import Image, Plain
from astrbot.core.message.message_event_result import ResultContentType
from data.plugins.astrbot_plugin_smart_imagechat_hub.main import SmartImageSenderPlugin
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.retrieval import RetrievalMixin
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.latency_emoji import (
    EmojiTurn, LATENCY_EMOJI_KEY, LATE_GRACE_DEFAULT, compatible_with_reply, local_candidates,
)
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.common import SKIP_PROACTIVE_EMOJI_EXTRA_KEY
from data.plugins.astrbot_plugin_proactive_agent.route_guard import _route, bind_route


def item(image_id, tags):
    return dict(id=image_id, filename=image_id + ".png", rel_path=image_id + ".png", tags=tags,
                auto_tags=[], manual_tags=[], selected_global_tags=[])


class Event:
    def __init__(self, text="开心极了", origin="aiocqhttp:GroupMessage:100", sender="1"):
        self.unified_msg_origin = origin
        self.message_obj = SimpleNamespace(message_id="m-original", sender=SimpleNamespace(user_id=sender))
        self.text = text
        self.extras = {}
        self.result = MessageEventResult().message("哈哈，好开心呀")
        self.result.set_result_content_type(ResultContentType.LLM_RESULT)
        self.transport = AsyncMock()
        self.stopped = False
        self.trace = SimpleNamespace(record=Mock())
        self.plugins_name = None
        self.is_at_or_wake_command = True

    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
    def get_result(self): return self.result
    def clear_result(self): self.result = None
    def get_message_str(self): return self.text
    def get_sender_id(self): return self.message_obj.sender.user_id
    def get_sender_name(self): return "test sender"
    def get_platform_id(self): return "aiocqhttp"
    def get_platform_name(self): return "aiocqhttp"
    def get_group_id(self): return self.unified_msg_origin.rsplit(":", 1)[-1].rsplit("_", 1)[-1] if ":GroupMessage:" in self.unified_msg_origin else ""
    def is_stopped(self): return self.stopped
    def stop_event(self): self.stopped = True
    def should_call_llm(self, enabled): pass
    def chain_result(self, chain): return MessageEventResult(chain=chain)
    def plain_result(self, text): return MessageEventResult().message(text)
    def _outline_chain(self, chain): return "test message"
    async def send(self, message): return await self.transport(message)


class Companion:
    def __init__(self):
        self.config = {"latency_optimization_enabled": True, "meme_separate_probability": 100}
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.answer = {"risk": "low", "image_ids": ["happy"]}
        self.tasks = []
        self.candidates = []
        self.prepare_calls = 0

    def _cfg(self, key, default=None): return self.config.get(key, default)

    def prepare_reply_prediction(self, event, candidates=None):
        task = event.get_extra("test_prediction")
        if task is not None:
            return task
        self.prepare_calls += 1
        self.candidates.append(copy.deepcopy(candidates))
        async def predict():
            self.started.set()
            await self.release.wait()
            return copy.deepcopy(self.answer)
        task = asyncio.create_task(predict())
        self.tasks.append(task)
        event.set_extra("test_prediction", task)
        return task

    def reply_prediction(self, event):
        task = event.get_extra("test_prediction")
        return task.result() if task and task.done() and not task.cancelled() else None

    async def run_auxiliary(self, factory): return await factory()


class LocalRecall(unittest.TestCase):
    def test_full_library_scored_before_top10_and_never_pruned(self):
        rows = [item(str(i), ["其他"]) for i in range(150)] + [item("target", ["开心", "大笑"])]
        before = copy.deepcopy(rows)
        result = local_candidates("开心极了", rows)
        self.assertEqual(len(result), 10)
        self.assertEqual(result[0]["id"], "target")
        self.assertEqual(rows, before)

    def test_ambiguity_and_weak_queries_can_expand_to30(self):
        rows = [item(str(i), ["开心" if i % 2 else "难过"]) for i in range(80)]
        for text, force in [("开心却又难过", False), ("聊点什么", False), ("开心", True)]:
            with self.subTest(text=text):
                self.assertEqual(len(local_candidates(text, rows, ambiguous=force)), 30)
        self.assertEqual(len(rows), 80)

    def test_full_final_reply_reversal_negation_and_character(self):
        happy, sorry = item("h", ["开心", "大笑"]), item("s", ["对不起", "道歉", "认怂"])
        self.assertTrue(compatible_with_reply("哈哈开心极了", {}, happy))
        for text in ["我并不开心，我真的很难过", "哈哈我刚才在笑。" + "这是背景。" * 300 + "但其实我很难过", "别笑了，对不起，是我错了"]:
            self.assertFalse(compatible_with_reply(text, {}, happy))
        self.assertFalse(compatible_with_reply("我不同意", {}, item("a", ["同意", "点头"])))
        for mood in ("serious", "stop", "timid"):
            self.assertFalse(compatible_with_reply("哈哈开心", {"mood": mood}, happy))
        self.assertTrue(compatible_with_reply("对不起，是我错了", {"mood": "timid"}, sorry))


class AsyncEmoji(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.rows = [item("happy", ["表情包", "开心", "大笑"]), item("sad", ["表情包", "难过", "流泪"])]
        for row in self.rows:
            (self.directory / row["rel_path"]).write_bytes(b"fake image")
        self.companion = Companion()
        self.cfg = {"enabled": True, "retrieval_mode": "bot_reply_serial", "meme_only": True,
                    "embed_in_conversation": True, "trigger_probability": "1", "analysis_provider_id": ""}
        self.p = RetrievalMixin()
        self.p._init_latency_emoji()
        self.meta = SimpleNamespace(activated=True, star_cls=self.companion)
        self.p.context = SimpleNamespace(get_registered_star=lambda _: self.meta, send_message=AsyncMock(return_value=True))
        self.p._proactive_emoji_config = lambda: copy.deepcopy(self.cfg)
        self.p._proactive_emoji_candidates = lambda _: copy.deepcopy(self.rows)
        self.p._proactive_emoji_probability_hit = Mock(return_value=True)
        self.p._abs_plugin_data_path = lambda path: self.directory / path
        self.p._tags_from_item = lambda row: row["tags"]
        self.p._prepare_send_image_path = AsyncMock(side_effect=lambda path, tags: (path, []))
        self.p._cleanup_temp_paths = lambda paths: [Path(path).unlink(missing_ok=True) for path in paths]
        self.p._after_plugin_sent_image_for_meme_combat = AsyncMock()
        self.p._sync_library = AsyncMock()
        self.p._analyze_proactive_emoji = AsyncMock(return_value={"matched": True})
        self.p._select_proactive_emoji = Mock(return_value=self.rows[0])
        self.p._apply_proactive_emoji_image = AsyncMock()
        self.p._log_proactive_emoji_debug = Mock()
        self.p._log_proactive_emoji_error_debug = Mock()
        self.p._send_pending_proactive_emoji = AsyncMock()
        self.p._send_pending_meme_combat_burst = AsyncMock()
        self.p._cleanup_deferred_send_image_style_paths = Mock()
        self.event = Event()

    async def asyncTearDown(self):
        await self.p._terminate_latency_emoji()
        for task in self.companion.tasks:
            task.cancel()
        await asyncio.gather(*self.companion.tasks, return_exceptions=True)
        self.temp.cleanup()

    async def start(self, event=None):
        event = event or self.event
        await self.p._start_parallel_proactive_emoji(event, event.text)
        return event.get_extra(LATENCY_EMOJI_KEY)

    async def ready(self, event=None):
        turn = await self.start(event)
        self.companion.release.set()
        await asyncio.wait_for(asyncio.shield(turn.prepare), 1)
        return turn

    async def decorate(self, event=None):
        await asyncio.wait_for(self.p._maybe_append_proactive_emoji(event or self.event), .25)

    async def sent(self, event=None):
        event = event or self.event
        await event.send(event.result)
        await SmartImageSenderPlugin.after_message_sent(self.p, event)

    async def drain(self, turn):
        if turn.delivery:
            outcomes = await asyncio.wait_for(asyncio.gather(turn.delivery, return_exceptions=True), 1)
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
                    raise outcome

    async def test_parallel_starts_before_body_shared_task_only_once(self):
        turn = await self.start()
        same = self.companion.prepare_reply_prediction(self.event)
        self.assertIs(same, turn.prediction)
        await self.companion.started.wait()
        async def body():
            self.assertFalse(turn.prediction.done())
            await self.decorate()
            return self.event.result.get_plain_text()
        text = await asyncio.wait_for(body(), .25)
        self.assertEqual(text, "哈哈，好开心呀")
        self.assertEqual(self.companion.prepare_calls, 1)
        self.assertTrue(self.companion.candidates[0])
        await self.p._prefetch_reply_emoji(self.event, SimpleNamespace(completion_text=text))
        self.p._analyze_proactive_emoji.assert_not_awaited()
        self.p._sync_library.assert_not_awaited()

    async def test_zero_wait_late_image_only_after_confirmed_text(self):
        turn = await self.start()
        await self.decorate()
        self.assertFalse(turn.prediction.done())
        self.assertEqual(len(self.event.result.chain), 1)
        await self.sent()
        self.assertFalse(turn.prediction.cancelled())
        self.p.context.send_message.assert_not_awaited()
        self.companion.release.set()
        await self.drain(turn)
        self.p.context.send_message.assert_awaited_once()
        self.assertEqual(self.p.context.send_message.call_args.args[0], "aiocqhttp:GroupMessage:100")
        self.assertEqual(len(self.event.result.chain), 1)
        self.assertEqual(self.event.result.get_plain_text(), "哈哈，好开心呀")
        self.assertTrue(all(isinstance(p, Image) for p in self.p.context.send_message.call_args.args[1].chain))
        self.assertEqual(turn.settings["emoji_late_grace_seconds"], 3)
        self.assertEqual(LATE_GRACE_DEFAULT, 3)
        await SmartImageSenderPlugin.after_message_sent(self.p, self.event)
        self.p.context.send_message.assert_awaited_once()

    async def test_ready_preserves_original_mixed_separate_probability(self):
        for percentage, roll, embedded in [(0, 0, True), (50, .49, False), (50, .5, True), (100, .99, False)]:
            with self.subTest(percentage=percentage, roll=roll):
                self.companion.config["meme_separate_probability"] = percentage
                event = Event()
                turn = await self.ready(event)
                with patch("data.plugins.astrbot_plugin_smart_imagechat_hub.backend.retrieval.random.random", return_value=roll):
                    await self.decorate(event)
                self.assertEqual(any(isinstance(p, Image) for p in event.result.chain), embedded)
                before = self.p.context.send_message.await_count
                await self.sent(event)
                await self.drain(turn)
                self.assertEqual(self.p.context.send_message.await_count - before, int(not embedded))
                self.assertEqual(event.result.get_plain_text(), "哈哈，好开心呀")

    async def test_late_window_expires_without_cancelling_shared_prediction(self):
        self.companion.config["emoji_late_grace_seconds"] = .02
        turn = await self.start()
        await self.decorate()
        await self.sent()
        await self.drain(turn)
        self.assertTrue(turn.closed)
        self.assertFalse(turn.prediction.done())
        self.companion.release.set()
        await turn.prediction
        self.p.context.send_message.assert_not_awaited()

    async def test_zero_late_grace_keeps_already_ready_separate_image(self):
        self.companion.config["emoji_late_grace_seconds"] = 0
        turn = await self.ready()
        await self.decorate()
        await self.sent()
        await self.drain(turn)
        self.p.context.send_message.assert_awaited_once()

    async def test_next_reply_cancels_same_physical_group_not_other_target(self):
        turn = await self.start()
        await self.decorate()
        await self.sent()
        other = Event(origin="aiocqhttp:FriendMessage:200")
        await self.start(other)
        self.assertFalse(turn.closed)
        newer = Event(origin="aiocqhttp:GroupMessage:2_100", sender="2")
        await self.start(newer)
        self.assertTrue(turn.closed)
        self.assertFalse(turn.prediction.cancelled())
        self.companion.release.set()
        await asyncio.gather(*self.companion.tasks)
        self.p.context.send_message.assert_not_awaited()

    async def test_generation_next_reply_also_cancels_pending(self):
        turn = await self.start()
        await self.decorate()
        await self.sent()
        newer = Event(text="帮我生成一张猫咪图片")
        await SmartImageSenderPlugin.on_llm_request(self.p, newer, None)
        self.assertTrue(turn.closed)
        self.assertTrue(newer.get_extra(SKIP_PROACTIVE_EMOJI_EXTRA_KEY))
        self.assertEqual(self.companion.prepare_calls, 1)

    async def test_non_llm_next_reply_closes_previous_late_window(self):
        turn = await self.start()
        await self.decorate()
        await self.sent()
        command = Event(text="/help")
        command.result = MessageEventResult().message("命令帮助")
        await self.decorate(command)
        self.assertTrue(turn.closed)
        self.assertFalse(turn.prediction.cancelled())
        self.assertEqual(command.result.get_plain_text(), "命令帮助")

    async def test_older_reply_hooks_cannot_cancel_a_newer_turn(self):
        old = await self.start()
        newer_event = Event()
        newer = await self.start(newer_event)
        self.assertTrue(old.closed)
        await self.decorate()
        await SmartImageSenderPlugin.on_llm_request(self.p, self.event, None)
        self.assertFalse(newer.closed)

    async def test_config_changes_cancel_and_never_fall_back_to_waiting(self):
        for change in ("master", "gallery", "probability", "window", "review", "unload"):
            with self.subTest(change=change):
                self.companion.config = {"latency_optimization_enabled": True, "meme_separate_probability": 100}
                self.cfg["enabled"] = True
                self.meta.activated = True
                event = Event()
                turn = await self.start(event)
                if change == "master": self.companion.config["latency_optimization_enabled"] = False
                if change == "gallery": self.cfg["enabled"] = False
                if change == "probability": self.companion.config["meme_separate_probability"] = 0
                if change == "window": self.companion.config["emoji_late_grace_seconds"] = 8
                if change == "review": self.companion.config["review_policy"] = "speed_first"
                if change == "unload": self.meta.activated = False
                await self.decorate(event)
                self.assertTrue(turn.closed)
                self.p._analyze_proactive_emoji.assert_not_awaited()

    async def test_config_change_after_text_or_library_delete_suppresses_late(self):
        for reason in ("disabled", "index", "file", "tags", "replaced"):
            with self.subTest(reason=reason):
                self.companion.release.clear()
                self.cfg["enabled"] = True
                self.rows[0] = item("happy", ["表情包", "开心", "大笑"])
                path = self.directory / "happy.png"
                path.write_bytes(b"original")
                event = Event()
                turn = await self.start(event)
                if reason == "replaced":
                    self.companion.release.set()
                    await turn.prepare
                await self.decorate(event)
                await self.sent(event)
                if reason == "disabled": self.cfg["enabled"] = False
                if reason == "index": self.rows.pop(0)
                if reason == "file": path.unlink()
                if reason == "tags": self.rows[0]["tags"] = ["悲伤"]
                if reason == "replaced":
                    path.write_bytes(b"different replacement")
                self.companion.release.set()
                await self.drain(turn)
                self.p.context.send_message.assert_not_awaited()
                if reason == "index": self.rows.insert(0, item("happy", ["开心"]))

    async def test_body_failure_and_false_result_never_allow_late(self):
        for failure in (RuntimeError("transport failed"), False):
            with self.subTest(failure=failure):
                event = Event()
                turn = await self.start(event)
                await self.decorate(event)
                if isinstance(failure, Exception):
                    event.transport.side_effect = failure
                    with self.assertRaises(RuntimeError): await event.send(event.result)
                else:
                    event.transport.return_value = False
                    await event.send(event.result)
                await SmartImageSenderPlugin.after_message_sent(self.p, event)
                self.assertTrue(turn.closed)
                self.assertFalse(turn.prediction.cancelled())
        self.p.context.send_message.assert_not_awaited()

    async def test_framework_failure_hook_is_not_delivery_confirmation(self):
        from astrbot.core.pipeline.respond.stage import RespondStage
        stage = RespondStage()
        stage.enable_seg = False
        stage.platform_settings = {}
        stage.config = {}
        turn = await self.start()
        await self.decorate()
        self.event.transport.side_effect = RuntimeError("mock transport only")
        async def hook(event, _):
            await SmartImageSenderPlugin.after_message_sent(self.p, event)
            return False
        with patch("astrbot.core.pipeline.respond.stage.call_event_hook", side_effect=hook) as called, \
                patch("astrbot.core.pipeline.respond.stage.logger"):
            await stage.process(self.event)
        called.assert_awaited_once()
        self.assertTrue(turn.closed)
        self.p.context.send_message.assert_not_awaited()

    async def test_partial_body_or_hook_without_body_cannot_open_window(self):
        for partial in (False, True):
            event = Event()
            turn = await self.start(event)
            await self.decorate(event)
            if partial:
                await event.send(MessageEventResult().message("哈哈，"))
            await SmartImageSenderPlugin.after_message_sent(self.p, event)
            self.assertTrue(turn.closed)
        self.p.context.send_message.assert_not_awaited()

    async def test_final_emotion_reversal_suppresses_ready_and_late_image(self):
        for ready in (False, True):
            self.companion.release.clear()
            event = Event()
            turn = await self.ready(event) if ready else await self.start(event)
            event.result.chain[0].text = "我刚才还很开心。但其实我现在真的很难过。"
            await self.decorate(event)
            await self.sent(event)
            self.companion.release.set()
            if turn.delivery: await self.drain(turn)
            self.assertFalse(any(isinstance(p, Image) for p in event.result.chain))
        self.p.context.send_message.assert_not_awaited()

    async def test_fixed_original_context_and_existing_route_guard(self):
        seen = []
        async def send(origin, message):
            seen.append((origin, copy.deepcopy(_route.get())))
            return True
        self.p.context.send_message.side_effect = send
        turn = await self.start()
        await self.decorate()
        await self.sent()
        self.event.unified_msg_origin = "aiocqhttp:FriendMessage:999"
        self.event.message_obj.message_id = "wrong-message"
        self.event.message_obj.sender.user_id = "999"
        bind_route("aiocqhttp:FriendMessage:888", "888", "other-message", False)
        self.companion.release.set()
        await self.drain(turn)
        self.assertEqual(seen, [("aiocqhttp:GroupMessage:100", {
            "origin": "aiocqhttp:GroupMessage:100", "sender": "1", "message_id": "m-original", "group": True,
        })])

    async def test_completed_reply_owner_does_not_cancel_late_window(self):
        async def reply():
            turn = await self.start()
            await self.decorate()
            await self.sent()
            return turn
        turn = await asyncio.create_task(reply())
        self.assertFalse(turn.closed)
        self.assertFalse(turn.prediction.done())
        self.companion.release.set()
        await self.drain(turn)
        self.p.context.send_message.assert_awaited_once()

    async def test_ready_invalidated_before_transport_keeps_all_text(self):
        for change in ("config", "deleted", "next_turn", "reversal"):
            with self.subTest(change=change):
                self.cfg["enabled"] = True
                self.companion.config["meme_separate_probability"] = 0
                path = self.directory / "happy.png"
                path.write_bytes(b"test")
                event = Event()
                turn = await self.ready(event)
                await self.decorate(event)
                self.assertTrue(turn.embedded)
                if change == "config": self.cfg["enabled"] = False
                if change == "deleted": path.unlink()
                if change == "next_turn": self.p._advance_latency_turn(Event())
                if change == "reversal": event.result.chain[0].text = "其实我很难过"
                expected = event.result.get_plain_text()
                await self.sent(event)
                outgoing = event.transport.call_args.args[0]
                self.assertEqual(outgoing.get_plain_text(), expected)
                self.assertFalse(any(isinstance(p, Image) for p in outgoing.chain))
                self.assertTrue(turn.closed)
        self.p.context.send_message.assert_not_awaited()

    async def test_formatter_and_segmented_body_confirm_whole_actual_text(self):
        turn = await self.start()
        await self.decorate()
        # AstrBot's formatter runs AFTER decorating hooks.
        self.event.result.chain = [Plain("前缀 哈哈"), Plain("好开心呀")]
        await self.event.send(MessageEventResult().message("前缀 哈哈"))
        await self.event.send(MessageEventResult().message("好开心呀"))
        await SmartImageSenderPlugin.after_message_sent(self.p, self.event)
        self.companion.release.set()
        await self.drain(turn)
        self.assertEqual(turn.text, "前缀 哈哈好开心呀")
        self.p.context.send_message.assert_awaited_once()

    async def test_no_image_or_malformed_prediction_does_not_touch_body(self):
        for answer in ({"risk": "unknown", "image_ids": []}, {"risk": "low", "image_ids": ["outside"]},
                       {"risk": "wrong", "image_ids": ["happy"]}, {"risk": "low", "image_ids": "happy"}):
            self.companion.answer = answer
            event = Event()
            turn = await self.ready(event)
            await self.decorate(event)
            self.assertTrue(turn.closed)
            self.assertEqual(event.result.get_plain_text(), "哈哈，好开心呀")
            self.assertFalse(any(isinstance(p, Image) for p in event.result.chain))
        self.p.context.send_message.assert_not_awaited()

    async def test_existing_image_and_explicit_search_are_never_auto_replaced(self):
        event = Event()
        generated = Image.fromFileSystem(str(self.directory / "sad.png"))
        event.result.chain.append(generated)
        turn = await self.ready(event)
        await self.decorate(event)
        self.assertTrue(turn.closed)
        self.assertEqual(event.result.chain[-1], generated)
        event = Event("帮我找一张表情包")
        await self.start(event)
        await self.decorate(event)
        self.assertEqual(self.companion.prepare_calls, 1)
        self.p.context.send_message.assert_not_awaited()

    async def test_real_companion_contract_receives_gallery_candidates_first(self):
        from data.plugins.astrbot_plugin_proactive_agent.reply_pipeline import ReplyPipelineMixin
        class RealCompanion(ReplyPipelineMixin):
            def __init__(inner):
                inner._cfg = self.companion._cfg
                inner.character_context = lambda event: {"mood": "cheeky"}
                inner._generate_json = AsyncMock(return_value=json.dumps({"risk": "low", "image_ids": ["happy"]}))
                inner._audit = Mock()
        real = RealCompanion()
        self.meta.star_cls = real
        await SmartImageSenderPlugin.on_llm_request(self.p, self.event, None)
        turn = self.event.get_extra(LATENCY_EMOJI_KEY)
        self.assertIs(real.prepare_reply_prediction(self.event), turn.prediction)
        await turn.prepare
        self.assertEqual(self.event.get_extra("qqbot_prediction_candidates")[0]["id"], "happy")
        real._generate_json.assert_awaited_once()
        await self.decorate()
        await self.sent()
        await self.drain(turn)
        self.p.context.send_message.assert_awaited_once()

    async def test_review_cancel_termination_and_owner_cancel_leave_prediction_alive(self):
        turn = await self.start()
        await self.event.get_extra("qqbot_cancel_reply_preparation")()
        self.assertTrue(turn.closed)
        self.assertFalse(turn.prediction.cancelled())
        newer = Event()
        owned = await self.start(newer)
        await self.p._terminate_latency_emoji()
        self.assertTrue(owned.closed)
        self.assertTrue(owned.prepare.cancelled())
        self.assertFalse(owned.prediction.cancelled())
        self.assertFalse(self.p._latency_emoji_tasks)
        self.assertFalse(self.p._latency_emoji_turns)

    async def test_cancelled_reply_owner_closes_only_gallery_preparation(self):
        entered = asyncio.Event()
        async def owner():
            await self.start()
            entered.set()
            await asyncio.Future()
        task = asyncio.create_task(owner())
        await entered.wait()
        turn = self.event.get_extra(LATENCY_EMOJI_KEY)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertTrue(turn.closed)
        self.assertFalse(turn.prediction.cancelled())

    async def test_slow_image_conversion_does_not_hold_text(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def prepare(path, tags):
            entered.set()
            await release.wait()
            return path, []
        self.p._prepare_send_image_path.side_effect = prepare
        turn = await self.start()
        self.companion.release.set()
        await entered.wait()
        await self.decorate()
        await self.sent()
        self.assertEqual(len(self.event.result.chain), 1)
        self.p.context.send_message.assert_not_awaited()
        release.set()
        await self.drain(turn)
        self.p.context.send_message.assert_awaited_once()

    async def test_cancelled_conversion_is_owned_and_cleans_temp_after_finishing(self):
        entered, release = asyncio.Event(), asyncio.Event()
        converted = self.directory / "converted.gif"
        async def prepare(path, tags):
            entered.set()
            await release.wait()
            converted.write_bytes(b"converted")
            return converted, [converted]
        self.p._prepare_send_image_path.side_effect = prepare
        turn = await self.start()
        self.companion.release.set()
        await entered.wait()
        await self.event.get_extra("qqbot_cancel_reply_preparation")()
        self.assertIn(turn.conversion, self.p._latency_emoji_tasks)
        self.assertFalse(turn.conversion.cancelled())
        closing = asyncio.create_task(self.p._terminate_latency_emoji())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await asyncio.wait_for(closing, 1)
        self.assertFalse(converted.exists())
        self.assertFalse(self.p._latency_emoji_tasks)
        self.p.context.send_message.assert_not_awaited()

    async def test_probability_miss_disabled_and_absent_companion_keep_legacy(self):
        self.p._proactive_emoji_probability_hit.return_value = False
        await self.start()
        await self.decorate()
        self.assertEqual(self.companion.prepare_calls, 0)
        self.p._proactive_emoji_probability_hit.assert_called_once()
        self.p._proactive_emoji_probability_hit.return_value = True
        for absent in (False, True):
            self.companion.config["latency_optimization_enabled"] = False
            self.meta.activated = not absent
            event = Event()
            await self.start(event)
            await self.p._maybe_append_proactive_emoji(event)
        self.assertEqual(self.p._analyze_proactive_emoji.await_count, 2)
        self.assertEqual(self.p._apply_proactive_emoji_image.await_count, 2)

    async def test_explicit_library_request_runs_full_legacy_handler(self):
        event = Event("给我找一个开心的表情包")
        self.p._cfg_bool = lambda key: {"user_search_enabled": True, "use_custom_reply": True}.get(key, False)
        self.p._has_request_keyword = lambda text: True
        self.p._library_candidates = Mock(return_value=self.rows)
        self.p._rank_search_candidates = Mock(return_value=({}, self.rows))
        self.p._analyze_request = AsyncMock(return_value={"matched": True, "image_ids": ["happy"]})
        self.p._select_image = Mock(return_value=self.rows[0])
        self.p._render_custom_reply = lambda *args: "找到你要的表情包啦"
        self.p._defer_send_image_style_cleanup = lambda event, paths: paths
        with patch("data.plugins.astrbot_plugin_smart_imagechat_hub.main._is_explicit_user_search_wake", return_value=True):
            replies = [reply async for reply in SmartImageSenderPlugin.on_wake_message(self.p, event)]
        self.p._analyze_request.assert_awaited_once()
        self.p._rank_search_candidates.assert_called_once_with(event.text, self.rows)
        self.assertTrue(any(isinstance(p, Image) for p in replies[0].chain))
        self.assertTrue(event.stopped)
        self.assertEqual(self.companion.prepare_calls, 0)
        self.assertIsNone(event.get_extra(LATENCY_EMOJI_KEY))
        self.p._proactive_emoji_probability_hit.assert_not_called()

    async def test_background_caption_preemption_passes_through_and_cleans_input(self):
        from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import BackgroundPaused
        paused = BackgroundPaused("foreground work started")
        preview = self.directory / "caption-preview.png"
        preview.write_bytes(b"preview")
        self.p._default_image_caption_provider_id = lambda: "mock"
        self.p._caption_category_labels = lambda: []
        self.p._caption_category_prompt_rules = lambda: ""
        self.p._prepare_caption_image_input = AsyncMock(return_value=(str(preview), [preview]))
        self.p._run_image_caption_provider_request = AsyncMock(side_effect=paused)
        with self.assertRaises(BackgroundPaused) as caught:
            await self.p._caption_image(self.directory / "happy.png")
        self.assertIs(caught.exception, paused)
        self.assertFalse(preview.exists())


if __name__ == "__main__":
    unittest.main()
