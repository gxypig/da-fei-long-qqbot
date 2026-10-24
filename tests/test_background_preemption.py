"""Offline integration regressions for caption preemption and provider fallback."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

# Import FIRST: AstrBot initializes its paths inside the maintenance sandbox.
from test_maintenance import ROOT
from PIL import Image
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import (
    BackgroundPaused, ModelScheduler, model_work,
)
from data.plugins.astrbot_plugin_smart_imagechat_hub.main import SmartImageSenderPlugin
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend import llm_context, utils
from data.plugins.astrbot_plugin_smart_imagechat_hub.backend.common import CAPTION_PROMPT_VERSION


NEW_TAGS = ["表情包", "开心", "大笑", "庆祝", "被夸", "得意"]


class LocalProvider:
    """Only the model I/O boundary is fake; admission/preemption stays real."""

    def __init__(self, provider_id, scheduler):
        self.provider_config = {"id": provider_id, "provider_source_id": provider_id}
        self.scheduler = scheduler
        self.calls = []
        self.started = asyncio.Queue()
        self.hold_calls = set()
        self.release = asyncio.Event()
        self.pauses = []
        self.response = SimpleNamespace(completion_text=json.dumps(NEW_TAGS, ensure_ascii=False))

    def meta(self):
        return SimpleNamespace(id=self.provider_config["id"], type="offline-test", model="local")

    async def text_chat(self, **kwargs):
        async def model_io():
            self.calls.append(kwargs)
            number = len(self.calls)
            self.started.put_nowait(number)
            if number in self.hold_calls:
                await self.release.wait()
            return self.response

        try:
            return await self.scheduler.run(self, model_io)
        except BackgroundPaused as exc:
            self.pauses.append(exc)
            raise


class BackgroundPreemption(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="caption-preemption-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.audit = []
        self.scheduler = ModelScheduler({}, lambda name, row: self.audit.append(row), idle_seconds=0)
        self.tasks = []
        self.primary = LocalProvider("primary", self.scheduler)
        self.backup = LocalProvider("backup", self.scheduler)
        self.providers = {"primary": self.primary, "backup": self.backup}
        self.reply_idle = asyncio.Event()
        self.reply_idle.set()
        companion = SimpleNamespace(
            _cfg=lambda key, default=None: True if key == "latency_optimization_enabled" else default,
            wait_for_reply_idle=self.reply_idle.wait,
        )
        meta = SimpleNamespace(activated=True, star_cls=companion)
        system_config = {"provider_settings": {
            "default_image_caption_provider_id": "primary", "fallback_chat_models": ["backup"],
        }}

        async def llm_generate(chat_provider_id, **kwargs):
            return await self.providers[chat_provider_id].text_chat(**kwargs)

        # Skip plugin startup, handlers and live configuration. All methods under
        # test, tag parsing/merging, pending selection and index persistence are real.
        self.p = object.__new__(SmartImageSenderPlugin)
        self.p.context = SimpleNamespace(
            get_registered_star=lambda name: meta if name == "astrbot_plugin_proactive_agent" else None,
            get_all_providers=lambda: list(self.providers.values()),
            get_provider_by_id=self.providers.get,
            get_config=lambda: system_config,
            llm_generate=AsyncMock(side_effect=llm_generate),
        )
        self.p.config = {
            "default_image_caption_provider_id": "primary",
            "library_builder": {"global_tags": ["全局标签"]},
        }
        self.p.data_dir = self.directory
        self.p.index_path = self.directory / "image_index.json"
        self.p._index = {"version": 1, "images": {}}
        self.p._lock = asyncio.Lock()
        self.p._caption_task = None
        self.p._caption_cleanup_tasks = set()
        self.p._external_import_state = {}
        self.p._imagebed_import_state = {"caption_paused": False}
        self.p._model_provider_failure_until = {"unrelated": time.time() + 600}
        self.failure_state = dict(self.p._model_provider_failure_until)
        self.p._caption_progress = self.p._make_caption_progress()
        self.p._set_caption_progress = Mock(wraps=self.p._set_caption_progress)
        self.p._mark_model_provider_failure = Mock(wraps=self.p._mark_model_provider_failure)
        # Configuration publishing is outside these integration boundaries.
        self.p._sync_config_image_files = Mock()
        self.p._sync_config_tags = Mock()
        self.p._refresh_image_tag_schema = Mock()
        self.p._save_plugin_config = Mock(side_effect=AssertionError("Unexpected config write"))
        # Avoid wall-clock throttling and unrelated compression. Real preparation
        # still converts GIF input and returns cleanup paths to _caption_image.
        for target, name, value in (
            (llm_context, "IMAGE_CAPTION_PROVIDER_MIN_INTERVAL_SECONDS", 0),
            (utils, "compress_image", None),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        try:
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.p._wait_caption_cleanup_tasks()
        finally:
            await self.scheduler.close()

    def add_image(self, name, *, manual_override=False):
        path = self.directory / name
        with Image.new("RGB", (8, 8), "white") as image:
            image.save(path)
        image_id = self.p._image_id(name)
        item = {
            "id": image_id, "rel_path": name, "filename": name,
            "caption_status": "pending", "captioned_at": 123, "caption_prompt_version": 0,
            "auto_tags": ["旧AI标签", "旧情绪"], "manual_tags": ["人工标签"],
            "selected_global_tags": ["全局标签"], "manual_tags_override": manual_override,
            "import_extra_tags": ["导入标签"],
        }
        item["tags"] = self.p._tags_from_item(item)
        self.p._index["images"][image_id] = item
        self.p._save_index()
        return item

    def background_call(self, factory):
        async def run():
            with model_work("background"):
                return await factory()

        task = asyncio.create_task(run())
        self.tasks.append(task)
        return task

    async def next_model_call(self, number):
        self.assertEqual(await asyncio.wait_for(self.primary.started.get(), 3), number)

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0)

    def assert_no_provider_failure(self):
        self.assertEqual(self.backup.calls, [])
        self.assertEqual(self.p._model_provider_failure_until, self.failure_state)
        self.p._mark_model_provider_failure.assert_not_called()
        self.assertEqual(getattr(self.p, "_caption_retry_after", 0), 0)

    async def test_caption_batch_survives_repeated_preemption_and_keeps_old_tags_searchable(self):
        first = self.add_image("a.png")
        second = self.add_image("b.png", manual_override=True)
        old_second = copy.deepcopy(second)
        self.primary.hold_calls = {2, 3}
        # The actual background entry point is _caption_pending_images, launched
        # here through its real task starter; no manual restart after either pause.
        self.p._start_caption_background_task()
        task = self.p._caption_task
        self.tasks.append(task)
        await self.next_model_call(1)

        for number in (2, 3):
            await self.next_model_call(number)
            self.assertEqual(first["caption_status"], "done")
            self.assertEqual(second["caption_status"], "running")
            self.reply_idle.clear()
            with self.scheduler.foreground():
                await self.until(lambda: self.p._caption_progress["status"] == "pending" or task.done())
                self.assertFalse(task.done(), "Preemption must not end the caption batch")
                self.assertIs(self.p._caption_task, task)
                self.assertEqual(second, old_second)
                saved = json.loads(self.p.index_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["images"][second["id"]], old_second)
                self.assertEqual(self.p._pending_caption_rel_paths(), ["b.png"])
                self.assertEqual(self.p._running_caption_count(), 0)
                candidates = {item["id"]: item for item in self.p._library_candidates()}
                self.assertEqual(set(candidates), {first["id"], second["id"]})
                self.assertEqual(candidates[second["id"]]["auto_tags"], old_second["auto_tags"])
                self.assertEqual(candidates[second["id"]]["tags"], old_second["tags"])
                self.assertEqual(self.p._caption_progress["completed"], 1)
                self.assertEqual(self.p._caption_progress["failed"], 0)
                self.assertEqual(len(self.primary.calls), number)
                self.assert_no_provider_failure()
            self.reply_idle.set()

        await asyncio.wait_for(task, 3)
        await self.p._wait_caption_cleanup_tasks()
        self.assertEqual(len(self.primary.calls), 4)
        self.assertEqual([Path(call["image_urls"][0]).name for call in self.primary.calls],
                         ["a.png", "b.png", "b.png", "b.png"])
        for item in (first, second):
            self.assertEqual(item["caption_status"], "done")
            self.assertEqual(item["auto_tags"], NEW_TAGS + ["导入标签"])
            self.assertEqual(item["manual_tags"], ["人工标签"])
            self.assertEqual(item["selected_global_tags"], ["全局标签"])
            self.assertGreater(item["captioned_at"], 123)
            self.assertEqual(item["caption_prompt_version"], CAPTION_PROMPT_VERSION)
        self.assertEqual(first["tags"], NEW_TAGS + ["导入标签", "人工标签", "全局标签"])
        self.assertTrue(second["manual_tags_override"])
        self.assertEqual(second["tags"], ["人工标签", "全局标签"])
        self.assertEqual(json.loads(self.p.index_path.read_text(encoding="utf-8")), self.p._index)
        self.assertEqual(self.p._pending_caption_rel_paths(), [])
        progress = self.p._caption_progress
        self.assertEqual((progress["status"], progress["total"], progress["completed"],
                          progress["failed"], progress["remaining"]), ("done", 2, 2, 0, 0))
        for update in self.p._set_caption_progress.call_args_list:
            self.assertNotIn(update.kwargs.get("status"), {"failed", "cancelled"})
            self.assertEqual(update.kwargs.get("failed", 0), 0)
        self.assertEqual([(row["kind"], row["status"]) for row in self.audit],
                         [("background", "ok"), ("background", "paused"),
                          ("background", "paused"), ("background", "ok")])
        self.assert_no_provider_failure()
        self.assertEqual(self.scheduler.status()["running"], 0)
        self.assertEqual(self.scheduler.status()["waiting"], 0)

    async def test_context_fallback_propagates_pause_without_cooldown_then_reuses_primary(self):
        self.primary.hold_calls = {1}

        def request():
            return self.p._llm_generate_with_provider_fallback(
                primary_provider_id="primary", prompt="离线后台分析",
                timeout_seconds=2, use_failure_cooldown=True, direct_provider_call=False,
            )

        task = self.background_call(request)
        await self.next_model_call(1)
        with self.scheduler.foreground():
            with self.assertRaises(BackgroundPaused) as caught:
                await asyncio.wait_for(task, 3)
        self.assertIs(caught.exception, self.primary.pauses[0])
        self.assertFalse(task.cancelled())
        self.assert_no_provider_failure()
        self.assertEqual(await self.p._model_provider_candidate_ids(primary_provider_id="primary"),
                         ["primary", "backup"])

        result = await asyncio.wait_for(self.background_call(request), 3)
        self.assertIs(result, self.primary.response)
        self.assertEqual(len(self.primary.calls), 2)
        calls = self.p.context.llm_generate.await_args_list
        self.assertEqual([call.kwargs["chat_provider_id"] for call in calls], ["primary", "primary"])
        self.assertEqual([row["status"] for row in self.audit], ["paused", "ok"])
        self.assert_no_provider_failure()

    async def test_caption_and_direct_fallback_propagate_same_pause_then_cleanly_retry(self):
        item = self.add_image("preview.gif")
        original = self.directory / item["rel_path"]
        original_bytes = original.read_bytes()
        self.primary.hold_calls = {1}
        request = lambda: self.p._caption_image(original)
        task = self.background_call(request)
        await self.next_model_call(1)
        preview = Path(self.primary.calls[0]["image_urls"][0])
        self.addCleanup(preview.unlink, missing_ok=True)
        self.assertNotEqual(preview, original)
        self.assertTrue(preview.is_file())
        with self.scheduler.foreground():
            with self.assertRaises(BackgroundPaused) as caught:
                await asyncio.wait_for(task, 3)
        self.assertIs(caught.exception, self.primary.pauses[0])
        self.assertFalse(preview.exists())
        self.assertFalse(self.p._caption_provider_call_lock.locked())
        self.assert_no_provider_failure()

        tags = await asyncio.wait_for(self.background_call(request), 3)
        self.assertEqual(tags, NEW_TAGS)
        self.assertEqual(len(self.primary.calls), 2)
        retry_preview = Path(self.primary.calls[1]["image_urls"][0])
        self.addCleanup(retry_preview.unlink, missing_ok=True)
        self.assertNotEqual(preview, retry_preview)
        self.assertFalse(retry_preview.exists())
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertFalse(self.p._caption_provider_call_lock.locked())
        self.p.context.llm_generate.assert_not_awaited()
        self.assertEqual([row["status"] for row in self.audit], ["paused", "ok"])
        self.assert_no_provider_failure()


if __name__ == "__main__":
    unittest.main()
