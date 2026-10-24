import asyncio
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.fixed_schedule import FixedScheduler, STATE_KEY


class FixedScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 10000
        self.db = {}
        self.p = object.__new__(Main)
        self.p.config = dict(enable=True, allow_group=True, allow_private=True, allow_qzone=True,
            fixed_messages_enabled=True, fixed_group_ids="101,102", fixed_private_ids="201,202",
            fixed_message_interval_minutes=30, fixed_qzone_enabled=True, fixed_qzone_interval_minutes=60)
        self.p._quiet_now = lambda: False
        self.p._recent_messages = {}
        async def get(key, default=None): return copy.deepcopy(self.db.get(key, default))
        async def put(key, value): self.db[key] = copy.deepcopy(value)
        self.p.get_kv_data, self.p.put_kv_data = get, put
        self.p._record = AsyncMock()
        self.s = FixedScheduler(self.p, lambda: self.now)
        self.s.generate_and_send = AsyncMock(return_value="hello")

    async def test_every_recipient_gets_own_slot_qzone_separate(self):
        await self.s.tick()
        self.s.generate_and_send.assert_not_awaited()
        self.now += 1800
        await self.s.tick()
        self.assertEqual({c.args[0] for c in self.s.generate_and_send.await_args_list},
                         {"group:101", "group:102", "private:201", "private:202"})
        await self.s.tick()
        self.assertEqual(self.s.generate_and_send.await_count, 4)
        self.now += 1800
        await self.s.tick()
        self.assertEqual(self.s.generate_and_send.await_count, 9)
        self.assertEqual(self.db[STATE_KEY]["qzone:"]["last_success"], self.now)

    async def test_one_failure_does_not_starve_other_targets(self):
        async def send(key, interval):
            if key == "group:101": raise RuntimeError("model failure")
            return "ok"
        self.s.generate_and_send.side_effect = send
        await self.s.tick(); self.now += 1800; await self.s.tick()
        self.assertEqual(self.s.generate_and_send.await_count, 4)
        self.assertEqual(self.db[STATE_KEY]["private:202"]["status"], "sent")
        self.now += 60
        await self.s.tick()
        self.assertEqual(self.s.generate_and_send.await_count, 5)

    async def test_restart_keeps_due_quiet_coalesces_missed_slots(self):
        await self.s.tick()
        self.now += 7200
        self.p._quiet_now = lambda: True
        await self.s.tick(); self.s.generate_and_send.assert_not_awaited()
        self.p._quiet_now = lambda: False
        second = FixedScheduler(self.p, lambda: self.now)
        second.generate_and_send = self.s.generate_and_send
        await second.tick(); await second.tick()
        self.assertEqual(second.generate_and_send.await_count, 5)
        self.assertTrue(all(v['due'] > self.now for v in self.db[STATE_KEY].values()))

    async def test_removal_and_interval_change_restart_timers(self):
        await self.s.tick(); self.now += 1700
        self.p.config.update(fixed_group_ids="102", fixed_message_interval_minutes=10)
        await self.s.tick()
        self.assertNotIn("group:101", self.db[STATE_KEY])
        self.assertEqual(self.db[STATE_KEY]["group:102"]["due"], self.now + 600)
        self.s.generate_and_send.assert_not_awaited()

    async def test_overlapping_ticks_and_disable_during_generation(self):
        await self.s.tick(); self.now += 1800
        gate = asyncio.Event()
        async def send(key, interval): await gate.wait(); return 'ok'
        self.s.generate_and_send.side_effect = send
        task = asyncio.create_task(self.s.tick())
        await asyncio.sleep(0.01)
        await self.s.tick()
        self.p.config['enable'] = False
        gate.set(); await task
        self.assertEqual(self.s.generate_and_send.await_count, 2)
        self.assertFalse(self.s.allowed('group:101',1800))

    async def test_real_generation_routing_and_final_permission_check(self):
        await self.s.tick()
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text='hello。')))
        self.p.context = SimpleNamespace(get_using_provider_async=AsyncMock(return_value=provider), send_message=AsyncMock(return_value=True))
        self.p._post_qzone = AsyncMock()
        real = FixedScheduler(self.p, lambda: self.now)
        await real.generate_and_send('private:201',1800)
        self.assertEqual(self.p.context.send_message.await_args.args[0], 'aiocqhttp:FriendMessage:201')
        provider.text_chat.return_value = SimpleNamespace(completion_text='游戏存档名后面的最终版到底能排到几代')
        await real.generate_and_send('qzone:',3600)
        self.p._post_qzone.assert_awaited_once()
        self.assertEqual(__import__('json').loads(provider.text_chat.await_args.kwargs['prompt'])['recent_conversation'], [])
        async def close(**kwargs):
            self.p.config['enable'] = False
            return SimpleNamespace(completion_text='hello')
        provider.text_chat.side_effect=close
        with self.assertRaises(RuntimeError): await real.generate_and_send('private:202',1800)
        self.assertEqual(self.p.context.send_message.await_count,1)

    async def test_optional_review_does_not_invoke_model(self):
        self.p.config['reply_review_enabled']=False
        self.p._reviewer=SimpleNamespace(review=AsyncMock())
        await self.p.review_agent_reply(None,None,SimpleNamespace())
        self.p._reviewer.review.assert_not_awaited()
