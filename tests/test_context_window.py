"""Offline context projection and private-summary lifecycle regressions."""

import asyncio
import copy
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_maintenance  # noqa: F401 -- initializes AstrBot in a temporary cwd
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from data.plugins.astrbot_plugin_proactive_agent import context_window as cw
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import _kind


def rounds(count=20, *, large=False):
    rows = []
    for i in range(count):
        user = {"role": "user", "content": f"Synthetic statement {i}"}
        if large:
            user["content"] = [{"type": "text", "text": f"Statement {i}"},
                               {"type": "image_url", "image_url": {
                                   "url": f"data:image/png;base64,offline-{i}"}}]
        rows.append([user, {"role": "assistant", "content": f"Synthetic answer {i}"}])
    return rows


def payload(history=None):
    history = rounds() if history is None else history
    return {"model": "offline-model", "messages": [
        {"role": "system", "content": "Synthetic private system policy"},
        {"role": "developer", "content": [{"type": "text", "text": "Developer policy"}]},
    ] + [m for rnd in history for m in rnd], "tool_choice": "auto"}


def cache_key(cid):
    return hashlib.sha256(cid.encode()).hexdigest()


SUMMARY = "A synthetic private summary retaining the original uncertainty."


class ContextWindowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="context-window-tests-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = {"context_window_enabled": True, "context_recent_turns": 12,
                       "context_recent_tokens": 6000}
        self.busy = True
        self.events = []
        self.windows = []
        self.provider = SimpleNamespace(text_chat=AsyncMock(
            return_value=SimpleNamespace(completion_text=json.dumps({"summary": SUMMARY}))))
        self.window = self.make_window()

    def make_window(self, name="summaries.json"):
        window = cw.ContextWindow(self.directory / name, self.config.get,
                                  lambda: self.busy,
                                  lambda kind, fields: self.events.append((kind, fields)))
        self.windows.append(window)
        return window

    async def asyncTearDown(self):
        for window in reversed(self.windows):
            await window.close()

    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0)

    def job(self, history=None, summary="", delta=None):
        history = rounds(8) if history is None else history
        return {"provider": self.provider, "old": copy.deepcopy(history),
                "delta": copy.deepcopy(history if delta is None else delta), "summary": summary}

    def seed(self, history, cid="session-a", summary=SUMMARY):
        self.window.cache[cache_key(cid)] = {
            "count": len(history), "fingerprint": cw.fingerprint(history),
            "summary": summary, "updated": 1,
        }

    async def test_whole_recent_rounds_systems_current_media_and_tool_pairs_survive(self):
        history = rounds()
        history[-1] = [
            {"role": "user", "content": [{"type": "text", "text": "Current request"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,current"}}]},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-a", "type": "function", "function": {"name": "search", "arguments": "{}"}},
                {"id": "call-b", "type": "function", "function": {"name": "read", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call-b", "content": "Synthetic result B"},
            {"role": "tool", "tool_call_id": "call-a", "content": "Synthetic result A"},
        ]
        source = payload(history)
        snapshot = copy.deepcopy(source)
        result = self.window.project(self.provider, source, "session-a")
        self.assertEqual(result["messages"][:2], source["messages"][:2])
        self.assertEqual(result["messages"][4:], [m for rnd in history[-12:] for m in rnd])
        self.assertEqual(result["messages"][-4:], history[-1])
        self.assertEqual(source, snapshot)
        self.assertEqual(result["model"], source["model"])
        self.provider.text_chat.assert_not_awaited()

    async def test_projection_isolated_from_downstream_mutation_including_system_parts(self):
        source = payload(rounds(20, large=True))
        snapshot = copy.deepcopy(source)
        result = self.window.project(self.provider, source, "session-a")
        result["messages"][1]["content"][0]["text"] = "Downstream rewrite"
        result["messages"][-2]["content"][1]["image_url"]["url"] = "Downstream image rewrite"
        result["messages"][-1]["content"] = "Downstream answer rewrite"
        self.assertEqual(source, snapshot)

    async def test_budget_is_soft_and_never_splits_four_mandatory_rounds(self):
        self.config.update(context_recent_tokens=2000, context_recent_turns=1)
        history = rounds(10, large=True)
        result = self.window.project(self.provider, payload(history), "session-a")
        self.assertEqual(result["messages"][4:], [m for rnd in history[-4:] for m in rnd])
        self.assertGreater(self.events[-1][1]["estimated_recent_tokens"], 2000)
        self.assertEqual(self.events[-1][1]["recent_rounds"], 4)

    async def test_sessions_and_changed_prefixes_cannot_reuse_each_others_summary(self):
        history = rounds()
        self.seed(history[:8])
        source = payload(history)
        used = self.window.project(self.provider, source, "session-a")
        self.assertIn(SUMMARY, used["messages"][2]["content"])
        other = self.window.project(self.provider, source, "session-b")
        self.assertNotIn(SUMMARY, other["messages"][2]["content"])
        changed = copy.deepcopy(history)
        changed[0][0]["content"] = "Corrected historical fact"
        invalidated = self.window.project(self.provider, payload(changed), "session-a")
        self.assertNotIn(SUMMARY, invalidated["messages"][2]["content"])
        self.assertEqual(self.window.jobs[cache_key("session-a")]["old"], changed[:8])
        audit_json = json.dumps(self.events)
        self.assertNotIn("session-a", audit_json)
        self.assertNotIn("Corrected historical fact", audit_json)

    async def test_cached_summary_combines_only_new_delta_and_refreshes_at_threshold(self):
        history = rounds(22)
        self.seed(history[:8])
        result = self.window.project(self.provider, payload(history), "session-a")
        note = result["messages"][2]["content"]
        self.assertIn(SUMMARY, note)
        self.assertIn("Synthetic statement 8", note)
        self.assertIn("Synthetic statement 9", note)
        self.assertNotIn("Synthetic statement 0", note)
        self.assertEqual(self.window.jobs, {})
        expanded = rounds(28)
        self.window.project(self.provider, payload(expanded), "session-a")
        job = self.window.jobs[cache_key("session-a")]
        self.assertEqual(job["summary"], SUMMARY)
        self.assertEqual(job["delta"], expanded[8:16])
        self.assertEqual(job["old"], expanded[:16])

    async def test_explicit_recall_returns_complete_context_without_jobs(self):
        for request in ("还记得之前那张图吗", "Please recall the earlier discussion", "昨天说了什么"):
            with self.subTest(request_type="recall"):
                history = rounds(20, large=True)
                history[-1] = [{"role": "user", "content": [{"type": "text", "text": request}]}]
                source = payload(history)
                self.assertIs(self.window.project(self.provider, source, "session-a"), source)
        self.assertEqual(self.window.jobs, {})
        self.provider.text_chat.assert_not_awaited()

    async def test_unsupported_shapes_and_cross_round_tool_pairs_fall_back_unchanged(self):
        variants = []
        unknown_role = payload()
        unknown_role["messages"][3]["role"] = "function"
        variants.append(unknown_role)
        mid_system = payload()
        mid_system["messages"].insert(8, {"role": "system", "content": "Historical instruction"})
        variants.append(mid_system)
        bad_text = payload()
        bad_text["messages"][-2]["content"] = [{"type": "text", "text": None}]
        variants.append(bad_text)
        opaque_part = payload()
        opaque_part["messages"][-2]["content"] = [object()]
        variants.append(opaque_part)
        orphan_tool = payload()
        orphan_tool["messages"][3] = {"role": "tool", "tool_call_id": "unknown", "content": "Result"}
        variants.append(orphan_tool)
        crossed_pair = payload()
        crossed_pair["messages"][3] = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "crossed", "type": "function", "function": {"name": "read", "arguments": "{}"}},
        ]}
        crossed_pair["messages"].insert(5, {"role": "tool", "tool_call_id": "crossed", "content": "Result"})
        variants.append(crossed_pair)
        orphan_assistant = payload()
        orphan_assistant["messages"].insert(2, {"role": "assistant", "content": "No initial user"})
        variants.append(orphan_assistant)
        for index, source in enumerate(variants):
            with self.subTest(shape=index):
                self.assertIs(self.window.project(self.provider, source, "session-a"), source)
        self.assertEqual(self.window.jobs, {})

    async def test_disabled_missing_session_and_small_context_are_passthrough(self):
        source = payload()
        self.config["context_window_enabled"] = False
        self.assertIs(self.window.project(self.provider, source, "session-a"), source)
        self.config["context_window_enabled"] = True
        self.assertIs(self.window.project(self.provider, source, None), source)
        small = payload(rounds(4))
        self.assertIs(self.window.project(self.provider, small, "session-a"), small)
        self.assertEqual(self.window.jobs, {})

    async def test_summary_uses_old_media_and_delta_only_in_background_without_tools(self):
        self.busy = False
        history = rounds(10, large=True)
        job = self.job(history, SUMMARY, history[8:])
        snapshot = copy.deepcopy(job["old"])
        observations = []

        async def generate(**kwargs):
            observations.append((_kind.get(), copy.deepcopy(kwargs)))
            kwargs["contexts"][-1]["content"] = "Provider mutation"
            return SimpleNamespace(completion_text=json.dumps({"summary": SUMMARY}))

        self.provider.text_chat.side_effect = generate
        await self.window.summarize(cache_key("session-a"), job)
        kind, kwargs = observations[0]
        self.assertEqual(kind, "background")
        self.assertEqual(kwargs["contexts"][2:], [m for rnd in history[8:] for m in rnd])
        self.assertNotIn("func_tool", kwargs)
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("conversation_id", kwargs)
        self.assertEqual(kwargs["request_max_retries"], 1)
        self.assertEqual(job["old"], snapshot)
        persisted = self.window.path.read_text(encoding="utf-8")
        self.assertNotIn("session-a", persisted)
        self.assertNotIn("data:image", persisted)
        self.assertNotIn("Synthetic statement", persisted)
        self.assertEqual(set(json.loads(persisted)[cache_key("session-a")]),
                         {"count", "fingerprint", "summary", "updated"})

    async def test_bad_summary_never_changes_valid_cache_or_disk(self):
        self.busy = False
        await self.window.summarize(cache_key("session-a"), self.job())
        saved, cache = self.window.path.read_bytes(), copy.deepcopy(self.window.cache)
        for raw in ("not JSON", "[]", '{"summary":42}', '{"summary":"short"}',
                    json.dumps({"summary": " " * 40}), json.dumps({"summary": "x" * 1801})):
            with self.subTest(result_type="invalid-summary"):
                self.provider.text_chat.return_value = SimpleNamespace(completion_text=raw)
                with self.assertRaises((ValueError, TypeError)):
                    await self.window.summarize(cache_key("session-a"), self.job(rounds(9)))
                self.assertEqual(self.window.cache, cache)
                self.assertEqual(self.window.path.read_bytes(), saved)

    async def test_persistence_failure_does_not_publish_an_uncommitted_summary(self):
        self.busy = False
        previous = copy.deepcopy(self.window.cache)
        with patch.object(Path, "replace", side_effect=OSError("Synthetic disk failure")):
            with self.assertRaises(OSError):
                await self.window.summarize(cache_key("session-a"), self.job())
        self.assertEqual(self.window.cache, previous)
        self.assertFalse(self.window.path.exists())
        self.assertEqual(self.window.stats["summarized"], 0)

    async def test_reload_accepts_matching_summary_but_rejects_corrupt_entries(self):
        self.busy = False
        await self.window.summarize(cache_key("session-a"), self.job())
        await self.window.close()
        loaded = self.make_window()
        result = loaded.project(self.provider, payload(), "session-a")
        self.assertIn(SUMMARY, result["messages"][2]["content"])
        bad = {cache_key("session-a"): {"count": 8, "summary": "", "fingerprint": "wrong"},
               "unhashed-private-id": {"count": 8, "summary": SUMMARY, "fingerprint": "wrong"}}
        (self.directory / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
        rejected = self.make_window("bad.json")
        self.assertEqual(rejected.cache, {})

    async def test_queue_is_bounded_and_keeps_newest_per_session(self):
        for i in range(12):
            self.window.project(self.provider, payload(), f"offline-session-{i}")
        self.assertEqual(len(self.window.jobs), 8)
        newer = rounds(21)
        self.window.project(self.provider, payload(newer), "offline-session-0")
        self.assertEqual(self.window.jobs[cache_key("offline-session-0")]["old"], newer[:9])
        self.assertEqual(len(self.window.jobs), 8)

    async def test_idle_worker_pauses_until_idle_and_does_not_inherit_reply_context(self):
        marker = ContextVar("context-window-test-origin", default=None)
        observed = []

        async def generate(**kwargs):
            observed.append((marker.get(), _kind.get()))
            return SimpleNamespace(completion_text=json.dumps({"summary": SUMMARY}))

        self.provider.text_chat.side_effect = generate
        with patch.object(cw, "IDLE_SECONDS", 0, create=True), \
                patch.object(cw, "POLL_SECONDS", .001, create=True):
            token = marker.set("must-not-inherit")
            try:
                self.window.project(self.provider, payload(), "session-a")
            finally:
                marker.reset(token)
            for _ in range(5):
                await asyncio.sleep(0)
            self.provider.text_chat.assert_not_awaited()
            self.busy = False
            await self.until(lambda: self.window.stats["summarized"] == 1)
        self.assertEqual(observed, [(None, "background")])

    async def test_background_pause_requeues_and_uses_newest_snapshot(self):
        self.busy = False
        entered, release = asyncio.Event(), asyncio.Event()
        attempts = []

        async def summarize(key, job):
            attempts.append(copy.deepcopy(job["old"]))
            if len(attempts) == 1:
                entered.set()
                await release.wait()
                raise cw.BackgroundPaused()

        with patch.object(cw, "IDLE_SECONDS", 0, create=True), \
                patch.object(cw, "POLL_SECONDS", .001, create=True), \
                patch.object(self.window, "summarize", side_effect=summarize):
            self.window.project(self.provider, payload(), "session-a")
            await asyncio.wait_for(entered.wait(), 1)
            newer = rounds(21)
            self.window.project(self.provider, payload(newer), "session-a")
            release.set()
            await self.until(lambda: len(attempts) == 2)
            await self.window.task
        self.assertEqual(attempts, [rounds(8), newer[:9]])
        self.assertEqual(self.window.stats["summary_paused"], 1)
        self.assertEqual(self.window.stats["summary_failed"], 0)

    async def test_timeout_cancels_model_and_does_not_write(self):
        self.busy = False
        cancelled = asyncio.Event()

        async def generate(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.provider.text_chat.side_effect = generate
        with patch.object(cw, "SUMMARY_TIMEOUT_SECONDS", .01, create=True):
            task = asyncio.create_task(self.window.summarize(cache_key("session-a"), self.job()))
            try:
                done, _ = await asyncio.wait({task}, timeout=.2)
                self.assertIn(task, done, "The summary's own timeout must stop model I/O")
                with self.assertRaises(TimeoutError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.window.cache, {})
        self.assertFalse(self.window.path.exists())

    async def test_disabling_during_summary_cancels_io_and_prevents_commit(self):
        self.busy = False
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def generate(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.provider.text_chat.side_effect = generate
        with patch.object(cw, "POLL_SECONDS", .001, create=True):
            task = asyncio.create_task(self.window.summarize(cache_key("session-a"), self.job()))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.config["context_window_enabled"] = False
                await asyncio.wait_for(cancelled.wait(), .3)
                with self.assertRaises(cw.BackgroundPaused):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.window.cache, {})
        self.assertFalse(self.window.path.exists())

    async def test_cancellation_is_not_swallowed_and_close_clears_pending_work(self):
        self.busy = False
        entered = asyncio.Event()

        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(cw, "IDLE_SECONDS", 0, create=True), \
                patch.object(self.window, "summarize", side_effect=blocked):
            self.window.project(self.provider, payload(), "session-a")
            await asyncio.wait_for(entered.wait(), 1)
            self.window.task.cancel()
            try:
                done, _ = await asyncio.wait({self.window.task}, timeout=.2)
                self.assertIn(self.window.task, done, "Worker cancellation must not become a retry")
                with self.assertRaises(asyncio.CancelledError):
                    await self.window.task
            finally:
                await self.window.close()
        await self.window.close()
        self.assertTrue(self.window.closed)
        self.assertEqual(self.window.jobs, {})
        source = payload()
        self.assertIs(self.window.project(self.provider, source, "session-a"), source)

    async def test_disable_pauses_real_worker_and_reenable_resumes_without_intermediate_publication(self):
        self.busy = False
        entered, cancelled = asyncio.Event(), asyncio.Event()
        calls = []

        async def generate(**kwargs):
            calls.append(copy.deepcopy(kwargs))
            if len(calls) == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return SimpleNamespace(completion_text=json.dumps({"summary": SUMMARY}))

        self.provider.text_chat.side_effect = generate
        with patch.object(cw, "IDLE_SECONDS", 0), patch.object(cw, "POLL_SECONDS", .001):
            self.window.project(self.provider, payload(), "session-a")
            await asyncio.wait_for(entered.wait(), 1)
            self.config["context_window_enabled"] = False
            await asyncio.wait_for(cancelled.wait(), 1)
            await self.until(lambda: self.window.stats["summary_paused"] == 1)
            self.assertEqual(self.window.cache, {})
            self.assertFalse(self.window.path.exists())
            self.assertIn(cache_key("session-a"), self.window.jobs)
            self.assertEqual(len(calls), 1)
            self.config["context_window_enabled"] = True
            await self.until(lambda: self.window.stats["summarized"] == 1)
            await self.window.task
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(self.window.stats["summary_failed"], 0)

    async def test_close_rejects_even_a_response_returned_during_cancellation_cleanup(self):
        self.busy = False
        entered = asyncio.Event()

        async def generate(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return SimpleNamespace(completion_text=json.dumps({"summary": SUMMARY}))

        self.provider.text_chat.side_effect = generate
        with patch.object(cw, "IDLE_SECONDS", 0), patch.object(cw, "POLL_SECONDS", .001):
            self.window.project(self.provider, payload(), "session-a")
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(self.window.close(), 1)
        self.assertEqual(self.window.cache, {})
        self.assertFalse(self.window.path.exists())
        self.assertEqual(self.window.jobs, {})
        self.assertEqual(self.window.stats["summarized"], 0)

    def test_extracts_keep_message_order_attribution_and_character_budget(self):
        history = [[{"role": "user", "content": "I prefer A"},
                    {"role": "assistant", "content": "A was only a tentative assumption"},
                    {"role": "user", "content": "Correction: I prefer B"}]]
        result = cw.extracts(history, 1600)
        self.assertEqual(result.splitlines(), ["user: I prefer A",
            "assistant: A was only a tentative assumption", "user: Correction: I prefer B"])
        self.assertLessEqual(len(cw.extracts(history, 50)), 50)

    async def test_wrappers_preserve_tools_kwargs_stream_and_restore_deactivates(self):
        registered = object()
        plugin = SimpleNamespace(_data_dir=self.directory, _cfg=self.config.get,
                                 _work=SimpleNamespace(busy=lambda: True), _audit=lambda *_: None)
        source = payload()
        observed = []

        async def query(prov, forwarded, tools, **kwargs):
            observed.append((prov, tools, kwargs))
            self.assertLess(len(forwarded["messages"]), len(source["messages"]))
            return "result"

        async def stream(prov, forwarded, tools, **kwargs):
            observed.append((prov, tools, kwargs))
            yield "chunk-a"
            yield "chunk-b"

        with patch.object(ProviderOpenAIOfficial, "_query", query), \
                patch.object(ProviderOpenAIOfficial, "_query_stream", stream):
            restore = cw.install_context_window(plugin)
            self.windows.append(plugin._context_window)
            try:
                options = {"conversation_id": "session-a", "request_max_retries": 5}
                self.assertEqual(await ProviderOpenAIOfficial._query(self.provider, source, registered, **options), "result")
                chunks = [r async for r in ProviderOpenAIOfficial._query_stream(
                    self.provider, source, registered, **options)]
                self.assertEqual(chunks, ["chunk-a", "chunk-b"])
                for prov, tools, kwargs in observed:
                    self.assertIs(prov, self.provider)
                    self.assertIs(tools, registered)
                    self.assertEqual(kwargs, options)
            finally:
                restore()
            self.assertIs(ProviderOpenAIOfficial._query, query)
            self.assertIs(ProviderOpenAIOfficial._query_stream, stream)
            self.assertTrue(plugin._context_window.closed)
            self.assertEqual(plugin._context_window.jobs, {})


if __name__ == "__main__":
    unittest.main()
