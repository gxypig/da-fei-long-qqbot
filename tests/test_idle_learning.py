"""Offline contract tests: only temporary SQLite/JSON files, never a bot or network."""
import asyncio
import builtins
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from temp_support import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "AstrBot"))
from data.plugins.astrbot_plugin_proactive_agent import idle_learning as idle
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import ReplyReviewer, ReviewUnavailable
from data.plugins.astrbot_plugin_proactive_agent.meme_memory import MemeMemory, DETECT_SYSTEM, LEARN_SYSTEM


class BackgroundPaused(Exception):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


PREFIXES = {
    "no_template": "根据你的需求，",
    "no_recap": "总而言之，",
    "no_preface": "让我来，",
    "few_fillers": "嗯嗯嗯，",
    "plain": "**",
    "concrete": "非常有意义，",
}


class IdleTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_revoke_removes_only_source_isolation_and_keeps_other_reasons(self):
        now=time.time()
        with self.s._db:
            for i,reason in enumerate(['','legacy_unverified_delivery']):
                body={'source':{'platform':'qq','account':'123','kind':'unverified'},'exclusion_reason':reason,'exclude_evidence':bool(reason)}
                self.s._db.execute('INSERT INTO cases(fingerprint,category,payload,created,expires) VALUES(?,?,?,?,?)',
                    (f'reclass-{i}','no_template',json.dumps(body),now,now+1000))
        await self.s.reclassify_source('qq','123','automatic_confirmed')
        await self.s.reclassify_source('qq','123','unverified')
        rows=[json.loads(r[0]) for r in self.s._db.execute('SELECT payload FROM cases ORDER BY id')]
        self.assertFalse(rows[0]['exclude_evidence']);self.assertTrue(rows[1]['exclude_evidence'])
        self.assertEqual(rows[1]['isolation_reasons'],['legacy_unverified_delivery'])

    async def test_learned_expression_is_pinned_to_persona_version(self):
        from data.plugins.astrbot_plugin_proactive_agent.persona_authority import _bound
        with self.s._db:
            self.s._db.execute('INSERT INTO versions(rules,published,evidence) VALUES(?,?,?)',
                (json.dumps(['no_template']),time.time(),json.dumps({'persona':{'persona_id':'dragon','persona_version':'v1'}})))
        self.assertEqual(self.s._active_rules({'persona_id':'dragon','persona_version':'v1'}),['no_template'])
        for state in [{'persona_id':'elf','persona_version':'v1'},{'persona_id':'dragon','persona_version':'v2'},
                      {'persona_id':'dragon','persona_version':'v1','persona_status':'disabled'}]:
            token=_bound.set(state)
            try:self.assertEqual(self.s.style_note('根据你的需求，开始吧'),'')
            finally:_bound.reset(token)

    async def test_rollback_switches_only_target_persona_and_zero_disables_all(self):
        with self.s._db:
            for pid,version,rule in [('dragon','v1','plain'),('elf','v2','concrete'),('dragon','v1','no_template')]:
                self.s._db.execute('INSERT INTO versions(rules,published,evidence) VALUES(?,?,?)',
                    (json.dumps([rule]),time.time(),json.dumps({'persona':{'persona_id':pid,'persona_version':version}})))
        dragon={'persona_id':'dragon','persona_version':'v1'};elf={'persona_id':'elf','persona_version':'v2'}
        self.assertEqual(self.s._active_rules(dragon),['no_template'])
        await self.s.rollback(1)
        self.assertEqual(self.s._active_rules(dragon),['plain']);self.assertEqual(self.s._active_rules(elf),['concrete'])
        await self.s.rollback(0)
        self.assertEqual(self.s._active_rules(dragon),[]);self.assertEqual(self.s._active_rules(elf),[])

    async def asyncSetUp(self):
        self.temp = TemporaryDirectory(prefix="idle-learning-tests-")
        self.root = Path(self.temp.name)
        self.cfg = {"latency_optimization_enabled": True, "idle_learning_enabled": True, "style_learning_enabled": True}
        self.busy = False
        self.events, self.calls = [], []
        self.category = "no_template"
        self.hook = None
        self.generate = AsyncMock(side_effect=self.response)
        self.search = AsyncMock(return_value={"results": [{"url": "https://example.invalid/public-meme", "title": "公开资料", "snippet": "公共用法"}]})
        self.memory = MemeMemory(self.root / "memes.json", self.generate, self.search, self.audit)
        self.reviewer = SimpleNamespace(review=AsyncMock(side_effect=self.preview))
        self.patches = [patch.object(idle, "IDLE_SECONDS", .015), patch.object(idle, "POLL_SECONDS", .002)]
        for item in self.patches:
            item.start()
        self.services = []
        self.s = self.make_service()

    def audit(self, kind, fields):
        self.events.append((kind, copy.deepcopy(fields)))

    def make_service(self, path=None, cfg=None):
        service = idle.IdleLearning(path or self.root / "learning.sqlite", self.cfg if cfg is None else cfg,
                                    self.memory, self.generate, self.reviewer, self.audit, lambda: self.busy)
        self.services.append(service)
        return service

    async def asyncTearDown(self):
        for service in reversed(self.services):
            await service.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    async def preview(self, text, **kwargs):
        return idle._canonical(text, kwargs)

    async def response(self, system, payload, origin):
        self.calls.append((system, copy.deepcopy(payload), origin))
        if self.hook is not None:
            result = await self.hook(system, payload, origin)
            if result is not None:
                return result
        if system == DETECT_SYSTEM:
            return encoded({"phrases": [x for x in ("甲梗", "乙梗", "啊对对对") if x in payload["当前发言"]][:2]})
        if system == LEARN_SYSTEM:
            return encoded({"verified": True, "meaning": "公共用法", "usage": "公开语境", "source_ids": [1]})
        if system == idle.CLASSIFY_SYSTEM:
            return encoded({"category": self.category})
        if system == idle.PROPOSE_SYSTEM:
            return encoded({"rules": payload["allowed"]})
        if system == idle.REWRITE_SYSTEM:
            text = payload["draft"]
            for category, prefix in PREFIXES.items():
                if idle.RULES[category][0] in payload["strategies"]:
                    text = text.replace(prefix, "")
            return encoded({"text": text})
        if system == idle.BLIND_SYSTEM:
            a, b = idle._cost(payload["A"]), idle._cost(payload["B"])
            return encoded({"winner": "tie" if a == b else ("A" if a < b else "B")})
        raise AssertionError("Unexpected model system")

    async def until(self, condition, timeout=4):
        deadline = time.monotonic() + timeout
        while not condition():
            if self.s._runner is not None and self.s._runner.done():
                self.s._runner.result()
                self.fail("Worker ended unexpectedly")
            if time.monotonic() >= deadline:
                self.fail("Timed out: " + str(self.s.snapshot()))
            await asyncio.sleep(.003)

    async def drain(self):
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["pending"] + self.s.snapshot()["running"] == 0)

    def examples(self, count=4, category=None, suffix=""):
        category = category or self.category
        prefix = PREFIXES[category]
        texts = [prefix + topic + suffix + ("**" if category == "plain" else "")
                 for topic in ("窗边听见雨声", "桌子先喝上了", "今天能歇口气", "眼皮正在打架", "闹钟也罢工了", "这局打得漂亮")[:count]]
        for text in texts:
            self.assertTrue(self.s.enqueue_review(text, ["聊聊今天"], "test:chat", {}, corrected=text.replace(prefix, "")))
        return texts

    def seed_case(self, phrase="无关的风格案例"):
        case = self.s._case(phrase, [], "test:style")
        case.update(fingerprint=idle._hash(phrase), force=True, corrected=None)
        with self.s._db:
            return self.s._db.execute("INSERT INTO cases(fingerprint,category,payload,created,expires) VALUES(?,?,?,?,?)",
                (case["fingerprint"], "brief", encoded(case), time.time(), time.time() + 30 * idle.DAY)).lastrowid

    def rows(self):
        return [dict(r) for r in self.s._db.execute("SELECT * FROM jobs ORDER BY id")]

    def calls_for(self, system):
        return [p for s, p, _ in self.calls if s == system]

    async def test_default_switches_off_and_disabled_worker_is_healthy(self):
        self.cfg.clear()
        self.memory.entries["旧梗"] = {"phrase": "旧梗", "meaning": "已有公开用法", "sources": [{"url": "https://example.invalid/old"}], "verified_at": time.time()}
        self.memory._save()
        before = self.memory.path.read_bytes()
        self.assertFalse(self.s.snapshot()["worker_running"])
        self.assertFalse(self.s.enqueue_meme("甲梗", [], "test"))
        self.assertFalse(self.s.enqueue_review("普通原稿", [], "test", {}, force=True))
        await self.s.start()
        await self.s.start()
        self.assertTrue(self.s.snapshot()["worker_running"])
        self.assertEqual(self.s.snapshot()["running"], 0)
        self.assertEqual(self.s.style_note("你好"), "")
        await self.s.close()
        self.assertFalse(self.s.snapshot()["worker_running"])
        self.assertEqual(before, self.memory.path.read_bytes())
        self.generate.assert_not_awaited()

    async def test_switches_are_independent_and_hot_path_never_calls_models(self):
        self.cfg["idle_learning_enabled"] = False
        self.assertFalse(self.s.enqueue_meme("甲梗", [], "test"))
        self.assertTrue(self.s.enqueue_review("原稿甲", [], "test", {}, force=True))
        self.cfg.update(idle_learning_enabled=True, style_learning_enabled=False)
        self.assertTrue(self.s.enqueue_meme("乙梗", [], "test"))
        self.assertFalse(self.s.enqueue_review("原稿乙", [], "test", {}, force=True))
        self.s.style_note("普通发言")
        self.s.snapshot()
        self.generate.assert_not_awaited()
        self.reviewer.review.assert_not_awaited()

    async def test_master_off_pauses_pending_and_inflight_learning_without_losing_jobs(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        original_detect = self.memory._detect

        async def slow_detect(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.memory._detect = AsyncMock(side_effect=slow_detect)
        self.assertTrue(self.s.enqueue_meme("甲梗", [], "test"))
        self.cfg["latency_optimization_enabled"] = False
        self.assertFalse(self.s.enqueue_review("待抽查回复", [], "test", {}, force=True))
        await self.s.start()
        await asyncio.sleep(idle.IDLE_SECONDS * 3)
        self.memory._detect.assert_not_awaited()
        self.assertEqual(self.s.snapshot()["pending"], 1)
        self.assertFalse(self.s.snapshot()["idle_learning_enabled"])
        self.cfg["latency_optimization_enabled"] = True
        await asyncio.wait_for(entered.wait(), 1)
        self.cfg["latency_optimization_enabled"] = False
        await asyncio.wait_for(cancelled.wait(), 1)
        await self.until(lambda: self.s.snapshot()["running"] == 0)
        await asyncio.sleep(idle.IDLE_SECONDS * 3)
        self.memory._detect.assert_awaited_once()
        self.assertEqual(self.s.snapshot()["pending"], 1)
        self.assertEqual(self.s.snapshot()["stats"]["failures"], 0)
        self.assertNotIn("甲梗", self.memory.entries)
        self.memory._detect = original_detect
        self.cfg["latency_optimization_enabled"] = True
        await self.drain()
        self.assertIn("甲梗", self.memory.entries)
        self.assertEqual(self.s.snapshot()["stats"]["completed"], 1)

    async def test_sampling_only_without_force_or_corrected_and_defaults_ten_percent(self):
        with patch.object(idle.random, "random", side_effect=[.0999, .10]):
            self.assertTrue(self.s.enqueue_review("抽样甲", [], "test", {}))
            self.assertFalse(self.s.enqueue_review("抽样乙", [], "test", {}))
        with patch.object(idle.random, "random", side_effect=AssertionError("must not sample")):
            self.assertTrue(self.s.enqueue_review("强制甲", [], "test", {}, force=True))
            self.assertTrue(self.s.enqueue_review("改写甲", [], "test", {}, corrected="改写乙"))
            self.assertTrue(self.s.enqueue_review("通过甲", [], "test", {}, corrected="通过甲"))
        self.cfg["reply_audit_sample_percent"] = 0
        self.assertFalse(self.s.enqueue_review("不抽甲", [], "test", {}))

    async def test_durable_bounded_queue_and_dedup(self):
        self.assertEqual(self.s._limit, 500)
        for i in range(500):
            self.assertTrue(self.s.enqueue_meme(f"聊天片段{i}", [], "test"))
        self.assertFalse(self.s.enqueue_meme("超过上限", [], "test"))
        self.assertFalse(self.s.enqueue_meme("聊天片段0", [], "test"))
        self.assertEqual(self.s.snapshot()["pending"], 500)
        await self.s.close()
        self.s = self.make_service()
        self.assertEqual(self.s.snapshot()["pending"], 500)
        self.cfg["idle_learning_queue_limit"] = 3
        with self.s._db:
            self.s._db.execute("DELETE FROM jobs")
        for i in range(12):
            self.s.enqueue_meme(f"轮次{i}", [], "test")
            with self.s._db:
                self.s._db.execute("DELETE FROM jobs")
        self.assertLessEqual(self.s._db.execute("SELECT count(*) FROM dedup").fetchone()[0], 6)

    async def test_five_uninterrupted_idle_seconds_and_busy_reset(self):
        clock = [100.0]
        self.s.enqueue_meme("甲梗", [], "test")
        with patch.object(idle, "IDLE_SECONDS", 5), patch.object(idle, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0])):
            await self.s.start()
            await self.until(lambda: self.s._idle_since == 100)
            clock[0] = 104.99
            await asyncio.sleep(.02)
            self.generate.assert_not_awaited()
            self.busy = True
            await self.until(lambda: self.s._idle_since is None)
            self.busy = False
            clock[0] = 200
            await self.until(lambda: self.s._idle_since == 200)
            clock[0] = 204.99
            await asyncio.sleep(.02)
            self.generate.assert_not_awaited()
            clock[0] = 205
            await self.until(lambda: "甲梗" in self.memory.entries)

    async def test_busy_preempts_and_requeues_one_task_without_failures(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        concurrent, maximum = 0, 0

        async def slow(*args):
            nonlocal concurrent, maximum
            concurrent += 1
            maximum = max(maximum, concurrent)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                concurrent -= 1

        self.memory._detect = AsyncMock(side_effect=slow)
        self.s.enqueue_meme("甲梗", [], "a")
        self.s.enqueue_meme("乙梗", [], "b")
        await self.s.start()
        await asyncio.wait_for(entered.wait(), 1)
        self.busy = True
        await asyncio.wait_for(cancelled.wait(), 1)
        await self.until(lambda: self.s.snapshot()["running"] == 0)
        self.assertEqual(maximum, 1)
        self.assertEqual(self.s.snapshot()["pending"], 2)
        self.assertEqual(self.s.snapshot()["stats"]["failures"], 0)
        self.assertTrue(all(r["attempts"] == 0 for r in self.rows()))
        self.memory._detect.side_effect = None
        self.memory._detect.return_value = []
        self.busy = False
        await self.drain()

    async def test_scheduler_pause_by_class_name_and_wrapped_review_pause_requeue(self):
        self.memory._detect = AsyncMock(side_effect=[BackgroundPaused(), []])
        self.s.enqueue_meme("甲梗", [], "test")
        await self.drain()
        self.assertEqual(self.memory._detect.await_count, 2)
        self.assertEqual(self.s.snapshot()["stats"]["failures"], 0)
        wrapper = ReviewUnavailable("not a defect")
        wrapper.__cause__ = BackgroundPaused()
        self.reviewer.review.side_effect = [wrapper, "根据你的需求，窗口吹来风"]
        self.s.enqueue_review("根据你的需求，窗口吹来风", [], "test", {}, force=True)
        await self.drain()
        self.assertEqual(self.reviewer.review.await_count, 2)
        self.assertEqual(self.s.snapshot()["stats"]["paused"], 2)
        self.assertEqual(self.s.snapshot()["stats"]["failures"], 0)

    async def test_close_cancels_inflight_and_restart_resumes_saved_detection(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        original_lookup = self.memory.lookup

        async def lookup(phrase, origin):
            if phrase == "乙梗":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return await original_lookup(phrase, origin)

        self.memory.lookup = AsyncMock(side_effect=lookup)
        self.s.enqueue_meme("甲梗与乙梗", [], "test")
        await self.s.start()
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(json.loads(self.rows()[0]["progress"])["index"], 1)
        await self.s.close()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.s.snapshot()["worker_running"])
        self.memory.lookup = AsyncMock(side_effect=original_lookup)
        self.s = self.make_service()
        await self.drain()
        self.assertEqual(len(self.calls_for(DETECT_SYSTEM)), 1)
        self.memory.lookup.assert_awaited_once_with("乙梗", "test")
        self.assertEqual(set(self.memory.entries), {"甲梗", "乙梗"})

    async def test_normal_cancelled_error_is_not_a_failure_and_worker_cancel_propagates(self):
        self.memory._detect = AsyncMock(side_effect=[asyncio.CancelledError(), []])
        self.s.enqueue_meme("甲梗", [], "test")
        await self.drain()
        self.assertEqual(self.s.snapshot()["stats"]["failures"], 0)
        self.s._runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.s._runner
        self.assertFalse(self.s.snapshot()["worker_running"])

    async def test_real_errors_report_bounded_retries_without_private_error_messages(self):
        self.memory._detect = AsyncMock(side_effect=TypeError("PRIVATE_CONTENT_NOT_FOR_AUDIT"))
        self.s.enqueue_meme("甲梗", [], "test")
        await self.s.start()
        for attempt in (1, 2, 3):
            await self.until(lambda: self.s.snapshot()["stats"]["failures"] == attempt)
            with self.s._db:
                self.s._db.execute("UPDATE jobs SET ready=0")
            self.s._wake.set()
        self.assertEqual(self.s.snapshot()["pending"], 0)
        self.assertEqual(self.s.snapshot()["last_error"], "TypeError")
        self.assertNotIn("PRIVATE_CONTENT", encoded(self.events))

    async def test_meme_source_validation_and_private_sentence_never_become_facts(self):
        async def invalid(system, payload, origin):
            if system == DETECT_SYSTEM:
                return encoded({"phrases": ["我家住在小巷", "凭空发明的梗"]})
            if system == LEARN_SYSTEM:
                return encoded({"verified": True, "meaning": "private", "source_ids": [999]})

        self.hook = invalid
        self.s.enqueue_meme("我家住在小巷", ["私聊信息"], "private:test")
        await self.drain()
        self.assertEqual(self.memory.entries, {})
        self.assertFalse(self.memory.path.exists())
        self.search.assert_awaited_once()
        self.assertNotIn("凭空", self.search.call_args.args[0])

    async def test_no_usable_sources_or_false_verification_never_persist(self):
        self.search.return_value = {"results": [{"url": "file:///private"}]}
        self.s.enqueue_meme("甲梗", [], "test")
        await self.drain()
        self.assertEqual(self.memory.entries, {})
        self.search.return_value = {"results": [{"url": "https://example.invalid/meme"}]}

        async def refuse(system, *_):
            return encoded({"verified": False, "meaning": "unknown", "source_ids": [1]}) if system == LEARN_SYSTEM else None

        self.hook = refuse
        self.s.enqueue_meme("乙梗", [], "test")
        await self.drain()
        self.assertEqual(self.memory.entries, {})

    async def test_public_meme_uses_existing_memory_persistence(self):
        self.s.enqueue_meme("啊对对对，全靠你", ["私有上文"], "private:test")
        await self.drain()
        restored = MemeMemory(self.memory.path, self.generate, self.search, self.audit)
        self.assertEqual(restored.match("啊对对对")[0]["sources"][0]["url"], "https://example.invalid/public-meme")
        self.assertNotIn("私有上文", self.memory.path.read_text(encoding="utf-8"))
        self.assertNotIn("private:test", self.memory.path.read_text(encoding="utf-8"))

    async def test_three_distinct_training_samples_plus_separate_holdout(self):
        texts = self.examples(3)
        self.assertFalse(self.s.enqueue_review(texts[0], ["不同上下文"], "different:origin", {}, force=True))
        self.assertFalse(self.s.enqueue_review(texts[0] + "！", [], "different:origin", {}, force=True))
        await self.drain()
        self.assertEqual(self.s.snapshot()["cases"], 3)
        self.assertFalse(self.calls_for(idle.PROPOSE_SYSTEM))
        fourth = "根据你的需求，树叶沙沙作响"
        self.s.enqueue_review(fourth, ["今天的树"], "test", {}, corrected="树叶沙沙作响")
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["active_version"] == 1)
        proposal = self.calls_for(idle.PROPOSE_SYSTEM)[0]
        self.assertEqual(len(proposal["samples"]), 3)
        self.assertNotIn(fourth, encoded(proposal))
        blind = self.calls_for(idle.BLIND_SYSTEM)[0]
        self.assertEqual(blind["draft"], fourth)
        self.assertEqual(set(blind), {"draft", "recent", "A", "B"})
        report = self.s.snapshot()["last_validation"]
        self.assertEqual(report["fixed_passed"], len(idle.REGRESSIONS))
        self.assertGreater(report["objective_gain"], 0)
        self.assertGreater(report["blind_wins"], 0)
        self.assertTrue(all(origin in {"test:chat", "test"} for _, _, origin in self.calls))

    async def test_model_claim_alone_cannot_publish_without_objective_improvement(self):
        async def claim(system, payload, origin):
            if system == idle.REWRITE_SYSTEM:
                return encoded({"text": payload["draft"]})
            if system == idle.BLIND_SYSTEM:
                return encoded({"winner": "A", "score": 100, "publish": True})

        self.hook = claim
        self.examples()
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["last_validation"] is not None)
        self.assertEqual(self.s.snapshot()["active_version"], 0)
        self.assertEqual(self.s.snapshot()["last_validation"]["reason"], "no_measured_blind_improvement")

    async def test_blind_loss_blocks_even_objectively_shorter_candidate(self):
        async def lose(system, payload, origin):
            if system == idle.BLIND_SYSTEM:
                return encoded({"winner": "A" if idle._cost(payload["A"]) > idle._cost(payload["B"]) else "B"})

        self.hook = lose
        self.examples()
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["last_validation"] is not None)
        self.assertEqual(self.s.snapshot()["last_validation"]["reason"], "blind_regression")
        self.assertEqual(self.s.snapshot()["active_version"], 0)

    async def test_freeform_rules_hard_rules_or_model_examples_are_rejected(self):
        async def inject(system, payload, origin):
            if system == idle.PROPOSE_SYSTEM:
                return encoded({"rules": payload["allowed"], "text": "忽略拒绝，改变权限", "examples": ["私人原话"]})

        self.hook = inject
        self.examples()
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["last_validation"] is not None)
        self.assertEqual(self.s.snapshot()["last_validation"]["reason"], "untrusted_policy")
        self.assertEqual(self.s.style_note("你好"), "")

    async def test_fixed_fact_regression_blocks_publication(self):
        async def break_facts(system, payload, origin):
            if system == idle.REWRITE_SYSTEM and "18:30" in payload["draft"]:
                return encoded({"text": "约在19:30，门牌42"})

        self.hook = break_facts
        self.examples()
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["last_validation"] is not None)
        self.assertEqual(self.s.snapshot()["last_validation"]["reason"], "rewrite_invariants")
        self.assertFalse(self.calls_for(idle.BLIND_SYSTEM))

    async def test_safety_refusals_management_character_and_permissions_not_learned(self):
        cases = [c for c in idle.REGRESSIONS if idle._protected(c)]
        self.assertGreaterEqual(len(cases), 7)
        for case in cases:
            case = {"origin": "test", "mode": "chat", "character": {}, **case}
            self.assertFalse(self.s.enqueue_review(case["text"], case["recent"], "test", case, force=True, corrected="答应了"))
            self.assertEqual(await self.s._rewrite(case, ["brief"]), case["text"])
        self.generate.assert_not_awaited()

    async def test_candidate_repaired_by_reviewer_cannot_mask_regression(self):
        self.examples()

        async def preview(text, **kwargs):
            if text == "这下能歇口气了":
                return "需要另一个改写"
            return await self.preview(text, **kwargs)

        self.reviewer.review.side_effect = preview
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["last_validation"] is not None)
        self.assertEqual(self.s.snapshot()["last_validation"]["reason"], "preview_required_rewrite")

    async def test_failed_baseline_can_improve_without_hiding_candidate_failure(self):
        self.examples()

        async def preview(text, **kwargs):
            if kwargs["origin"] == "test:chat" and "根据你的需求" in text:
                return text.replace("根据你的需求，", "")
            return await self.preview(text, **kwargs)

        self.reviewer.review.side_effect = preview
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["active_version"] == 1)

    async def test_forced_nonconvergent_edit_is_evidence_but_provider_failure_is_not(self):
        failure = ReviewUnavailable("blocked")
        failure.__cause__ = ValueError("Review did not converge")
        self.reviewer.review.side_effect = failure
        self.s.enqueue_review("根据你的需求，窗外有风", [], "test", {}, force=True)
        await self.drain()
        self.assertEqual(self.s.snapshot()["cases"], 1)
        failure = ReviewUnavailable("offline")
        failure.__cause__ = OSError("network unavailable")
        self.reviewer.review.side_effect = failure
        self.s.enqueue_review("根据你的需求，外面有雨", [], "test", {}, force=True)
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["stats"]["failures"] == 1)
        self.assertEqual(self.s.snapshot()["cases"], 1)

    async def test_validation_progress_survives_pause_close_and_restart(self):
        paused = False

        async def pause_once(system, payload, origin):
            nonlocal paused
            if system == idle.BLIND_SYSTEM and not paused:
                paused = True
                raise BackgroundPaused()

        self.hook = pause_once
        self.examples()
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["stats"]["paused"] == 1)
        self.busy = True
        fixed_calls = len(self.calls_for(idle.REWRITE_SYSTEM))
        await self.s.close()
        self.s = self.make_service()
        self.busy = False
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["active_version"] == 1)
        self.assertEqual(len(self.calls_for(idle.PROPOSE_SYSTEM)), 1)
        self.assertEqual(len(self.calls_for(idle.REWRITE_SYSTEM)), fixed_calls)
        self.assertEqual(self.s.snapshot()["stats"]["failures"], 0)

    async def test_daily_publication_limit_five_versions_and_persistent_rollback(self):
        clock = [time.time()]
        with patch.object(idle, "time", SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic)):
            for number, category in enumerate(PREFIXES, 1):
                self.category = category
                self.examples(category=category, suffix=category)
                await self.s.start()
                if number > 1:
                    await self.until(lambda: self.s.snapshot()["cases"] == 4 * number)
                    self.assertEqual(self.s.snapshot()["active_version"], number - 1)
                    clock[0] += idle.DAY + 1
                await self.until(lambda: self.s.snapshot()["active_version"] == number)
            self.assertEqual(len(self.s.snapshot()["versions"]), 5)
            self.assertEqual([v["version"] for v in self.s.snapshot()["versions"]], [6, 5, 4, 3, 2])
            with self.assertRaises(ValueError):
                await self.s.rollback(1)
            last_publish = self.s.snapshot()["last_publish"]
            self.assertEqual((await self.s.rollback())["active_version"], 5)
            self.assertEqual((await self.s.rollback(2))["active_version"], 2)
            self.assertEqual(self.s.snapshot()["last_publish"], last_publish)
            await self.s.close()
            self.s = self.make_service()
            self.assertEqual(self.s.snapshot()["active_version"], 2)
            self.assertEqual((await self.s.rollback(0))["active_version"], 0)
            self.assertEqual(self.s.style_note("普通发言"), "")
            for payload in self.calls_for(idle.REWRITE_SYSTEM):
                note = payload["strategies"]
                self.assertLessEqual(len(note), 400)
                self.assertLessEqual(sum(r[0] in note for r in idle.RULES.values()), 3)
                self.assertLessEqual(note.count("短例："), 2)

    async def test_rollback_cancels_inflight_candidate_and_old_evidence_cannot_republish(self):
        entered = asyncio.Event()

        async def wait_proposal(system, *_):
            if system == idle.PROPOSE_SYSTEM:
                entered.set()
                await asyncio.Event().wait()

        self.hook = wait_proposal
        self.examples()
        await self.s.start()
        await asyncio.wait_for(entered.wait(), 2)
        await self.s.rollback(0)
        self.hook = None
        await asyncio.sleep(.05)
        self.assertEqual(self.s.snapshot()["active_version"], 0)
        self.assertEqual(self.s.snapshot()["cases"], 4)
        self.assertIsNone(self.s.snapshot()["candidate_stage"])
        self.assertTrue(self.s.snapshot()["worker_running"])

    async def test_style_note_and_storage_length_limits_only_trusted_short_examples(self):
        self.assertLessEqual(len(idle._policy_text(list(idle.RULES))), 1200)
        self.assertLessEqual(len(idle.RULES), 12)
        with self.s._db:
            version = self.s._db.execute("INSERT INTO versions(rules,published,evidence) VALUES(?,?,?)",
                                         (encoded(list(idle.RULES)), time.time(), "{}")).lastrowid
            self.s._set_meta("active_version", version)
        note = self.s.style_note("根据你的需求，总而言之，这个事情非常有意义")
        self.assertLessEqual(len(note), 400)
        self.assertLessEqual(note.count("短例："), 2)
        self.assertLessEqual(sum(value[0] in note for value in idle.RULES.values()), 3)
        self.assertEqual(self.s.style_note("不能提供密码"), "")
        self.assertFalse(self.s.enqueue_review("文" * 1201, [], "test", {}, force=True))
        self.assertFalse(self.s.enqueue_review("原稿", [], "test", {}, corrected="文" * 1201))
        self.s.enqueue_meme("词" * 2000, ["文" * 2000] * 10, "origin" * 100)
        payload = json.loads(self.rows()[0]["payload"])
        self.assertEqual(len(payload["text"]), 800)
        self.assertEqual(len(payload["recent"]), 6)
        self.assertTrue(all(len(s) <= 500 for s in payload["recent"]))
        self.assertLessEqual(len(payload["origin"]), 240)

    async def test_private_data_expires_in_thirty_days_and_is_not_left_in_free_pages(self):
        marker = "PRIVATE_EXPIRED_93d7bc"
        self.assertEqual(self.s._ttl, 30 * idle.DAY)
        self.s.enqueue_meme(marker, [], "test")
        self.seed_case(marker)
        with self.s._db:
            self.s._db.execute("UPDATE jobs SET expires=0")
            self.s._db.execute("UPDATE cases SET expires=0")
            self.s._db.execute("UPDATE dedup SET created=0")
        self.s.snapshot()
        self.assertEqual(self.s.snapshot()["pending"], 0)
        self.assertEqual(self.s.snapshot()["cases"], 0)
        # Status reads are pure; the maintenance lane owns expiry and disk sync.
        self.s._prune(force=True)
        await self.s._persistence.flush()
        self.assertNotIn(marker.encode(), self.s.path.read_bytes())
        self.assertNotIn(marker, encoded(self.events))

    async def test_forget_only_target_meme_jobs_preserves_other_memes_cases_and_validation(self):
        await self.memory.lookup("甲梗", "test")
        self.s.enqueue_meme("甲梗", [], "test")
        self.s.enqueue_meme("乙梗", [], "test")
        self.seed_case("无关的风格案例")
        self.s.enqueue_review("根据你的需求，普通风格案例", [], "test", {}, force=True)
        self.s._enqueue("validate", {"stage": "unrelated validation"})
        retained = [r for r in self.rows() if json.loads(r["payload"]).get("text") != "甲梗"]
        removed = await self.s.forget("甲梗")
        self.assertTrue(removed)
        self.assertEqual(self.rows(), retained)
        self.assertEqual(self.s.snapshot()["cases"], 1)
        self.assertNotIn("甲梗", self.memory.entries)
        self.assertEqual(self.s.snapshot()["pending_forgets"], 0)
        self.assertFalse(await self.s.forget("甲梗"))

    async def test_forget_matches_recent_and_progress_with_normalized_keys(self):
        self.s.enqueue_meme("其他文本", ["提到ＡＢ梗"], "test")
        self.s.enqueue_meme("待查文本", [], "test")
        self.s.enqueue_meme("乙梗", [], "test")
        with self.s._db:
            self.s._db.execute("UPDATE jobs SET progress=? WHERE id=?", (encoded({"phrases": ["ab梗"], "index": 0}), self.rows()[1]["id"]))
        await self.s.forget("Ab梗")
        self.assertEqual([json.loads(r["payload"])["text"] for r in self.rows()], ["乙梗"])

    async def test_forget_cancels_inflight_lookup_and_cannot_resurrect_after_restart(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked_search(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        self.search.side_effect = blocked_search
        self.s.enqueue_meme("甲梗", [], "test")
        self.s.enqueue_meme("乙梗", [], "test")
        self.seed_case()
        await self.s.start()
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(await self.s.forget("甲梗"))
        self.assertTrue(cancelled.is_set())
        self.busy = True
        self.assertEqual(self.s.snapshot()["cases"], 1)
        await self.s.close()
        self.search.side_effect = None
        self.s = self.make_service()
        self.busy = False
        await self.drain()
        self.assertNotIn("甲梗", self.memory.entries)
        self.assertIn("乙梗", self.memory.entries)

    async def test_forget_does_not_cancel_unrelated_active_style_review(self):
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def blocked_preview(text, **kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return await self.preview(text, **kwargs)

        self.reviewer.review.side_effect = blocked_preview
        self.s.enqueue_review("根据你的需求，窗外有风", [], "test", {}, force=True)
        self.s.enqueue_meme("甲梗", [], "test")
        await self.s.start()
        await asyncio.wait_for(entered.wait(), 1)
        await self.s.forget("甲梗")
        self.assertFalse(cancelled.is_set())
        self.assertEqual(self.s.snapshot()["running"], 1)
        release.set()
        await self.drain()
        self.assertEqual(self.s.snapshot()["cases"], 1)

    async def test_forget_of_target_cancels_detection_even_when_it_swallows_cancel(self):
        entered = asyncio.Event()

        async def uncooperative_detection(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return ["甲梗"]

        self.memory._detect = AsyncMock(side_effect=uncooperative_detection)
        self.s.enqueue_meme("刚听说甲梗", [], "test")
        await self.s.start()
        await asyncio.wait_for(entered.wait(), 1)
        await self.s.forget("甲梗")
        await asyncio.sleep(.03)
        self.search.assert_not_awaited()
        self.assertEqual(self.s.snapshot()["pending"], 0)
        self.assertNotIn("甲梗", self.memory.entries)

    async def test_forget_does_not_cancel_unrelated_active_meme(self):
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = self.memory._detect

        async def detection(*args):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return await original(*args)

        self.memory._detect = AsyncMock(side_effect=detection)
        self.s.enqueue_meme("乙梗", [], "test")
        await self.s.start()
        await asyncio.wait_for(entered.wait(), 1)
        await self.s.forget("甲梗")
        self.assertFalse(cancelled.is_set())
        release.set()
        await self.drain()
        self.assertIn("乙梗", self.memory.entries)

    async def test_disabling_midflight_keeps_queue_and_existing_memes(self):
        await self.memory.lookup("乙梗", "test")
        before = self.memory.path.read_bytes()
        entered = asyncio.Event()

        async def detect(*_):
            entered.set()
            await asyncio.Event().wait()

        self.memory._detect = AsyncMock(side_effect=detect)
        self.s.enqueue_meme("甲梗", [], "test")
        await self.s.start()
        await asyncio.wait_for(entered.wait(), 1)
        self.cfg["idle_learning_enabled"] = False
        await self.until(lambda: self.s.snapshot()["running"] == 0)
        self.assertEqual(self.s.snapshot()["pending"], 1)
        self.assertTrue(self.s.snapshot()["worker_running"])
        self.assertEqual(before, self.memory.path.read_bytes())
        self.memory._detect.side_effect = None
        self.memory._detect.return_value = []
        self.cfg["idle_learning_enabled"] = True
        await self.drain()

    async def test_real_memory_pause_clears_backoff_and_retries_saved_phrase(self):
        paused = False

        async def pause(system, payload, origin):
            nonlocal paused
            if system == LEARN_SYSTEM and not paused:
                paused = True
                raise BackgroundPaused()

        self.hook = pause
        self.s.enqueue_meme("甲梗", [], "test")
        await self.drain()
        self.assertIn("甲梗", self.memory.entries)
        self.assertNotIn("甲梗", self.memory._retry_after)
        self.assertEqual(self.s.snapshot()["stats"]["failures"], 0)
        self.assertEqual(self.s.snapshot()["stats"]["paused"], 1)
        self.assertEqual(len(self.calls_for(DETECT_SYSTEM)), 1)

    async def test_cancelled_close_propagates_and_leaves_recoverable_queue(self):
        entered, cleaning = asyncio.Event(), asyncio.Event()

        async def detect(*_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await asyncio.sleep(.2)

        self.memory._detect = AsyncMock(side_effect=detect)
        self.s.enqueue_meme("甲梗", [], "test")
        await self.s.start()
        await asyncio.wait_for(entered.wait(), 1)
        closing = asyncio.create_task(self.s.close())
        await asyncio.wait_for(cleaning.wait(), 1)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertFalse(self.s.snapshot()["worker_running"])
        self.assertTrue(self.s.snapshot()["closed"])
        self.memory._detect.side_effect = None
        self.memory._detect.return_value = []
        self.s = self.make_service()
        await self.drain()
        self.assertEqual(self.s.snapshot()["stats"]["failures"], 0)

    async def test_forget_persistent_tombstone_retries_after_disk_failure_and_restart(self):
        await self.memory.lookup("甲梗", "test")
        original = self.memory.forget
        failure = True

        async def forget(phrase):
            nonlocal failure
            if failure:
                failure = False
                raise OSError("disk busy")
            return await original(phrase)

        self.memory.forget = AsyncMock(side_effect=forget)
        self.s.enqueue_meme("甲梗", [], "test")
        with self.assertRaises(OSError):
            await self.s.forget("甲梗")
        self.assertEqual(self.s.snapshot()["pending_forgets"], 1)
        self.assertFalse(self.s.enqueue_meme("甲梗", [], "new"))
        self.assertTrue(self.s.enqueue_meme("乙梗", [], "new"))
        self.assertTrue(self.s.enqueue_review("普通的案例", [], "new", {}, force=True))
        await self.s.close()
        self.s = self.make_service()
        await self.s.start()
        self.assertEqual(self.s.snapshot()["pending_forgets"], 0)
        self.assertNotIn("甲梗", self.memory.entries)
        self.assertEqual(self.memory.forget.await_count, 2)

    async def test_real_reply_reviewer_is_preview_only_and_never_calls_transport(self):
        from aiocqhttp import CQHttp
        judge = AsyncMock(return_value=encoded({"pass": True, "natural": True, "chat_fit": True}))
        self.s.reviewer = ReplyReviewer(judge, self.audit)
        self.s.enqueue_review("今天能歇口气了", [], "test", {}, corrected="今天能歇口气了")
        with patch.object(CQHttp, "call_action", new_callable=AsyncMock) as transport:
            await self.drain()
            judge.assert_awaited_once()
            transport.assert_not_awaited()

    async def test_missing_scheduler_falls_back_but_broken_scheduler_import_is_visible(self):
        original_import = builtins.__import__

        def load(error):
            def importing(name, globals=None, locals=None, fromlist=(), level=0):
                if name == "work_scheduler" and level == 1:
                    raise error
                return original_import(name, globals, locals, fromlist, level)
            spec = importlib.util.spec_from_file_location(idle.__package__ + ".idle_import_probe", idle.__file__)
            module = importlib.util.module_from_spec(spec)
            with patch("builtins.__import__", side_effect=importing):
                spec.loader.exec_module(module)
            return module

        absent = ModuleNotFoundError("missing optional module", name=idle.__package__ + ".work_scheduler")
        self.assertIsNone(load(absent).model_work)
        with self.assertRaises(ModuleNotFoundError):
            load(ModuleNotFoundError("broken dependency", name="missing_dependency"))
        with self.assertRaises(ImportError):
            load(ImportError("missing exported model_work", name=idle.__package__ + ".work_scheduler"))
        with patch.object(idle, "model_work", None):
            self.s.enqueue_meme("甲梗", [], "test")
            await self.drain()
            self.assertIn("甲梗", self.memory.entries)

    async def test_all_worker_model_calls_use_background_context(self):
        if idle.model_work is None:
            self.skipTest("scheduler is an optional separately delivered module")
        from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import _kind

        async def check(*_):
            self.assertEqual(_kind.get(), "background")

        self.hook = check
        self.s.enqueue_meme("甲梗", [], "test")
        self.examples()
        await self.s.start()
        await self.until(lambda: self.s.snapshot()["active_version"] == 1)
        self.assertIsNone(_kind.get())

    async def test_unrelated_or_corrupt_database_is_not_modified(self):
        other = self.root / "other.sqlite"
        with sqlite3.connect(other) as db:
            db.execute("CREATE TABLE unrelated(value TEXT)")
        db.close()
        before = other.read_bytes()
        with self.assertRaises(ValueError):
            self.make_service(other)
        self.assertEqual(before, other.read_bytes())
        broken = self.root / "broken.sqlite"
        broken.write_bytes(b"broken-database-do-not-overwrite")
        with self.assertRaises(sqlite3.DatabaseError):
            self.make_service(broken)
        self.assertEqual(broken.read_bytes(), b"broken-database-do-not-overwrite")
        with self.assertRaises(ValueError):
            self.make_service(self.memory.path)


if __name__ == "__main__":
    unittest.main()
