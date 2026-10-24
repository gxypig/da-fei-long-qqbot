"""Offline admission/cancellation tests; no real APIs, sends or history writes."""
import asyncio
import unittest

import test_work_scheduler as fixtures
from test_work_scheduler import FakeResponse, provider, until
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import (
    HedgePreempted, ModelScheduler, WorkClosed, _HedgeControl,
)


class HedgePreemptionTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the isolated fake-provider fixture without re-running inherited tests.
    asyncSetUp = fixtures.HedgeTests.asyncSetUp
    asyncTearDown = fixtures.HedgeTests.asyncTearDown
    start = fixtures.HedgeTests.start
    trigger = fixtures.HedgeTests.trigger
    stats = fixtures.HedgeTests.stats

    def actual(self, operation, *, source=None, kind='interactive'):
        task = asyncio.create_task(self.runtime.scheduler.run(source or self.provider, operation, kind=kind))
        self.parents.append(task)
        return task

    async def start_race(self):
        task = self.start()
        await self.trigger()
        await until(lambda: len(self.calls) == 2)
        return task

    async def test_interactive_starts_before_primary_finishes_and_signal_is_not_set(self):
        primary_gate, cleanup = asyncio.Event(), asyncio.Event()
        abort = asyncio.Event()
        async def operation(index, p, params):
            self.assertIs(params['abort_signal'], abort)
            if index == 0:
                await primary_gate.wait()
                return FakeResponse('primary answer')
            try:
                await asyncio.Future()
            finally:
                cleanup.set()
        self.operation = operation
        race = self.start(abort_signal=abort)
        await self.trigger()
        await until(lambda: len(self.calls) == 2)
        entered = asyncio.Event()
        async def actual():
            self.assertTrue(cleanup.is_set())
            entered.set()
            return 'explicit answer'
        request = self.actual(actual)
        await entered.wait()
        self.assertEqual(await request, 'explicit answer')
        self.assertFalse(race.done())
        self.assertFalse(abort.is_set())
        self.assertEqual(len(self.calls), 2)  # no speculative restart
        primary_gate.set()
        self.assertEqual((await race).completion_text, 'primary answer')
        self.assertEqual(self.stats(), {'hedged': 1, 'won': 0, 'failed': 0})
        self.assertTrue(any(name == 'model_work' and row['status'] == 'preempted' for name, row in self.rows))
        self.assertEqual(self.runtime.scheduler.status()['running'], 0)

    async def test_cleanup_completes_before_releasing_slot_no_oversubscription(self):
        primary_gate, cancelled, cleanup_gate, entered = (asyncio.Event() for _ in range(4))
        async def operation(index, *args):
            if index == 0:
                await primary_gate.wait()
                return FakeResponse()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
                await cleanup_gate.wait()
        self.operation = operation
        race = await self.start_race()
        request = self.actual(self._mark(entered))
        await cancelled.wait()
        self.runtime.scheduler.wake()  # must not cancel cleanup a second time
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(entered.is_set())
        self.assertEqual(self.runtime.scheduler.status()['running'], 2)
        self.assertEqual(self.runtime.scheduler.status()['waiting'], 1)
        cleanup_gate.set()
        await entered.wait()
        await request
        self.assertFalse(race.done())
        primary_gate.set()
        await race

    @staticmethod
    def _mark(event):
        async def operation():
            event.set()
        return operation

    async def test_primary_failure_after_preemption_gets_one_frozen_recovery(self):
        primary_gate, actual_gate, actual_entered = (asyncio.Event() for _ in range(3))
        original_contexts = [{'role': 'user', 'content': 'frozen'}]
        abort = asyncio.Event()
        async def operation(index, p, params):
            self.assertIs(params['abort_signal'], abort)
            if index == 0:
                await primary_gate.wait()
                raise OSError('primary failed')
            if index == 1:
                params['contexts'][0]['content'] = 'mutated by duplicate'
                await asyncio.Future()
            self.assertEqual(params['contexts'][0]['content'], 'frozen')
            self.assertEqual(p.provider_config['custom_extra_body'], {'reasoning_effort': 'high'})
            return FakeResponse('recovered')
        self.operation = operation
        race = self.start(contexts=original_contexts, abort_signal=abort)
        await self.trigger()
        await until(lambda: len(self.calls) == 2)
        original_contexts[0]['content'] = 'mutated by caller'
        async def actual():
            actual_entered.set()
            await actual_gate.wait()
        request = self.actual(actual)
        await actual_entered.wait()
        self.assertEqual(len(self.calls), 2)
        primary_gate.set()
        self.assertEqual((await race).completion_text, 'recovered')
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(abort.is_set())
        self.assertFalse(request.done())
        actual_gate.set()
        await request
        recovery, = [row for name, row in self.rows if name == 'model_provider_call' and row['call_role'] == 'hedge_recovery']
        self.assertEqual((recovery['work_kind'], recovery['status']), ('interactive', 'ok'))

    async def test_already_failed_primary_protects_its_running_backup(self):
        fail, backup_gate, backup_cancelled = (asyncio.Event() for _ in range(3))
        error = OSError('primary failed')
        async def operation(index, *args):
            if index == 0:
                await fail.wait()
                raise error
            try:
                await backup_gate.wait()
                return FakeResponse('backup is now required')
            except asyncio.CancelledError:
                backup_cancelled.set()
                raise
        self.operation = operation
        race = await self.start_race()
        fail.set()
        await until(lambda: self.runtime.scheduler.status()['running'] == 1)
        gate, blocker_entered, next_entered = (asyncio.Event() for _ in range(3))
        async def blocker():
            blocker_entered.set()
            await gate.wait()
        first = self.actual(blocker)
        await blocker_entered.wait()
        second = self.actual(self._mark(next_entered))
        await until(lambda: self.runtime.scheduler.status()['waiting'] == 1)
        self.assertFalse(backup_cancelled.is_set())
        self.assertFalse(next_entered.is_set())
        backup_gate.set()
        self.assertEqual((await race).completion_text, 'backup is now required')
        await second
        gate.set()
        await first
        self.assertEqual(len(self.calls), 2)

    async def test_recovery_cannot_be_preempted_and_preserves_tool_response(self):
        fail, actual_gate, actual_entered, recovery_gate, recovery_entered = (asyncio.Event() for _ in range(5))
        response = FakeResponse('', role='tool', tools_call_name=['tool'], tools_call_args=[{'q': 'test'}], tools_call_ids=['call-test'])
        recovery_cancelled = []
        async def operation(index, *args):
            if index == 0:
                await fail.wait()
                raise OSError('primary failed')
            if index == 1:
                await asyncio.Future()
            recovery_entered.set()
            try:
                await recovery_gate.wait()
                return response
            except asyncio.CancelledError:
                recovery_cancelled.append(True)
                raise
        self.operation = operation
        race = await self.start_race()
        async def actual():
            actual_entered.set()
            await actual_gate.wait()
        request = self.actual(actual)
        await actual_entered.wait()
        fail.set()
        await recovery_entered.wait()
        next_entered = asyncio.Event()
        following = self.actual(self._mark(next_entered))
        await until(lambda: self.runtime.scheduler.status()['waiting'] == 1)
        self.assertFalse(next_entered.is_set())
        self.assertFalse(recovery_cancelled)
        recovery_gate.set()
        self.assertIs(await race, response)
        await following
        actual_gate.set()
        await request
        self.assertEqual(len(self.calls), 3)

    async def test_failed_recovery_preserves_original_primary_exception_and_is_bounded(self):
        fail = asyncio.Event()
        error = OSError('original primary error')
        async def operation(index, *args):
            if index == 0:
                await fail.wait()
                raise error
            if index == 1:
                await asyncio.Future()
            raise ValueError('recovery also failed')
        self.operation = operation
        race = await self.start_race()
        await self.actual(self._mark(asyncio.Event()))
        fail.set()
        with self.assertRaises(OSError) as caught:
            await race
        self.assertIs(caught.exception, error)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.stats(), {'hedged': 2, 'won': 0, 'failed': 1})
        self.assertEqual(self.runtime.scheduler.status()['running'], 0)
        self.assertFalse(self.runtime._hedge_tasks)

    async def test_translated_cancel_is_not_backup_failure_and_recovery_tools_commit_once(self):
        fail = asyncio.Event()
        response = FakeResponse('', role='tool', tools_call_name=['tool'], tools_call_args=[{'q': 'test'}], tools_call_ids=['call-test'])
        executed = []
        async def operation(index, *args):
            if index == 0:
                await fail.wait()
                raise OSError('primary failed after hedge yielded')
            if index == 1:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    raise RuntimeError('adapter translated cancellation during cleanup') from None
            return response
        self.operation = operation
        async def consumer():
            result = await self.start()
            # Business tools live outside scheduler/provider factories. Only
            # the one selected response is returned for one execution.
            executed.extend(zip(result.tools_call_name, result.tools_call_ids))
            return result
        owner = asyncio.create_task(consumer())
        self.parents.append(owner)
        await self.trigger()
        await until(lambda: len(self.calls) == 2)
        await self.actual(self._mark(asyncio.Event()))
        self.assertFalse(owner.done())
        self.assertEqual(self.stats()['failed'], 0)
        self.assertEqual(executed, [])
        fail.set()
        self.assertIs(await owner, response)
        self.assertEqual(executed, [('tool', 'call-test')])
        self.assertEqual(self.stats(), {'hedged': 2, 'won': 1, 'failed': 0})
        rows = [row for name, row in self.rows if name == 'model_provider_call' and row['call_role'] == 'hedge']
        self.assertEqual([row['status'] for row in rows], ['preempted'])
        self.assertEqual(self.runtime.scheduler.status()['running'], 0)

    async def test_parent_cancel_while_preempted_waits_does_not_restart_or_cancel_other_request(self):
        self.operation = lambda *args: asyncio.Future()
        race = await self.start_race()
        gate, entered = asyncio.Event(), asyncio.Event()
        async def actual():
            entered.set()
            await gate.wait()
            return 'actual'
        request = self.actual(actual)
        await entered.wait()
        race.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await race
        self.assertFalse(request.done())
        self.assertEqual(len(self.calls), 2)
        gate.set()
        self.assertEqual(await request, 'actual')
        self.assertFalse(self.runtime._hedge_tasks)
        self.assertEqual(self.runtime.scheduler.status()['running'], 0)

    async def test_shutdown_after_preemption_does_not_deadlock_or_recover(self):
        self.operation = lambda *args: asyncio.Future()
        race = await self.start_race()
        entered = asyncio.Event()
        async def actual():
            entered.set()
            await asyncio.Future()
        request = self.actual(actual)
        await entered.wait()
        await self.runtime.close()
        for task in (race, request):
            with self.assertRaises(WorkClosed):
                await task
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.runtime._hedge_tasks)

    async def test_other_source_and_spare_capacity_do_not_cancel_hedge(self):
        self.cfg['model_concurrency'] = 3
        gate, cancelled = asyncio.Event(), asyncio.Event()
        async def operation(index, *args):
            try:
                await gate.wait()
                return FakeResponse()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        self.operation = operation
        race = await self.start_race()
        await self.actual(self._mark(asyncio.Event()), source=provider('another-source'))
        await self.actual(self._mark(asyncio.Event()))
        self.assertFalse(cancelled.is_set())
        self.assertFalse(any(name == 'reply_hedge' and row['status'] == 'preempted' for name, row in self.rows))
        gate.set()
        await race


class PreemptionAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler = ModelScheduler({})
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.scheduler.close()

    def submit(self, operation, **kwargs):
        task = asyncio.create_task(self.scheduler.run(provider(), operation, **kwargs))
        self.tasks.append(task)
        return task

    async def test_prediction_auxiliary_is_not_a_speculative_hedge(self):
        gate, started = asyncio.Event(), []
        async def operation(label):
            started.append(label)
            await gate.wait()
            return label
        tasks = [self.submit(lambda: operation('primary')), self.submit(lambda: operation('prediction'), kind='auxiliary')]
        await until(lambda: len(started) == 2)
        tasks.append(self.submit(lambda: operation('next real')))
        await until(lambda: self.scheduler.status()['waiting'] == 1)
        self.assertEqual(started, ['primary', 'prediction'])
        gate.set()
        self.assertEqual(await asyncio.gather(*tasks), ['primary', 'prediction', 'next real'])

    async def test_admitted_but_not_started_hedge_is_preempted_without_model_io(self):
        gate, primary_gate, entered = asyncio.Event(), asyncio.Event(), []
        primary = self.submit(primary_gate.wait)
        async def blocker():
            entered.append('blocker')
            await gate.wait()
        blocking = self.submit(blocker)
        await until(lambda: entered == ['blocker'])
        async def duplicate():
            entered.append('duplicate must never start')
        hedge = self.submit(duplicate, kind='auxiliary', hedge=_HedgeControl())
        await until(lambda: self.scheduler.status()['waiting'] == 1)
        async def arrive():
            await gate.wait()
            async def actual():
                entered.append('actual')
            await self.scheduler.run(provider(), actual)
        request = asyncio.create_task(arrive())
        self.tasks.append(request)
        await asyncio.sleep(0)
        # Blocker releases a slot first, marking hedge ready. The actual request
        # is already runnable and arrives before that hedge resumes _acquire.
        gate.set()
        await request
        with self.assertRaises(HedgePreempted):
            await hedge
        self.assertEqual(entered, ['blocker', 'actual'])
        primary_gate.set()
        await asyncio.gather(primary, blocking)

    async def test_preempted_worker_that_suppresses_cancel_cannot_hold_slot_or_win(self):
        gate, started, actual = asyncio.Event(), asyncio.Event(), asyncio.Event()
        primary = self.submit(gate.wait)
        async def duplicate():
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return 'late duplicate result'
        hedge = self.submit(duplicate, kind='auxiliary', hedge=_HedgeControl())
        await started.wait()
        async def real():
            actual.set()
        request = self.submit(real)
        await actual.wait()
        with self.assertRaises(HedgePreempted):
            await hedge
        await request
        gate.set()
        await primary

    async def test_two_actual_generations_start_concurrently_and_both_finish(self):
        gate, started = asyncio.Event(), []
        async def operation(label):
            started.append(label)
            await gate.wait()
            return label
        first = self.submit(lambda: operation('first'))
        second = self.submit(lambda: operation('second'))
        await until(lambda: len(started) == 2)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        gate.set()
        self.assertEqual(await asyncio.gather(first, second), ['first', 'second'])


if __name__ == '__main__':
    unittest.main()
