"""Offline scheduler/transport tests; no AstrBot startup, network or QQ calls."""
import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar
import inspect
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'AstrBot'))
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import (
    BackgroundPaused, InvalidModelResponse, ModelScheduler, WorkClosed, WorkRuntime, _isolated_provider,
    TAIL_GUARD_MARKER, _valid_model_response, install_work_runtime, model_work, provider_key,
)


class Clock:
    def __init__(self, value=100.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class PredictionPriority(unittest.IsolatedAsyncioTestCase):
    async def test_other_reply_preempts_optional_prediction_but_waits_for_cleanup(self):
        runtime=WorkRuntime({'latency_optimization_enabled':True,'model_concurrency':2}.get,lambda *_:None)
        self.addAsyncCleanup(runtime.close)
        primary_entered=asyncio.Event();primary_release=asyncio.Event()
        predicted=asyncio.Event();cancelled=asyncio.Event();cleanup=asyncio.Event();other_entered=asyncio.Event()
        self.addCleanup(primary_release.set)
        self.addCleanup(cleanup.set)
        async def primary():primary_entered.set();await primary_release.wait();return 'primary'
        async def predict():
            predicted.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:cancelled.set();await cleanup.wait();raise
        async def other():other_entered.set();return 'other'
        event=Event(message_id='first')
        with runtime.reply_work(event):
            first=asyncio.create_task(runtime.scheduler.run(provider(),primary,kind='interactive'))
            await primary_entered.wait()
            with runtime.prediction_work(event):
                prediction=asyncio.create_task(runtime.scheduler.run(provider(),predict,kind='auxiliary'))
            await asyncio.wait_for(predicted.wait(),1)
            with runtime.reply_work(Event(target='aiocqhttp:GroupMessage:second',message_id='second')):
                second=asyncio.create_task(runtime.scheduler.run(provider(),other,kind='interactive'))
                await asyncio.wait_for(cancelled.wait(),1)
                self.assertFalse(other_entered.is_set());self.assertFalse(first.done())
                cleanup.set()
                with self.assertRaises(BackgroundPaused):await prediction
                self.assertEqual(await second,'other')
            primary_release.set();self.assertEqual(await first,'primary')


def provider(source='one', **config):
    return SimpleNamespace(provider_config={'provider_source_id': source, **config})


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


class Event:
    def __init__(self, target='aiocqhttp:GroupMessage:alice_100', received=100, message_id='in-1'):
        self.unified_msg_origin = target
        self.message_obj = SimpleNamespace(message_id=message_id)
        self.extras = {'qqbot_received_at': received}
        self.result = None
        self.stopped = False

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_result(self):
        return self.result

    def is_stopped(self):
        return self.stopped


def text(body):
    return [{'type': 'text', 'data': {'text': body}}]


class FakeResponse:
    def __init__(self, body='complete reply', *, role='assistant', **fields):
        self.role, self.completion_text = role, body
        self.is_chunk, self.result_chain = False, None
        self.tools_call_name, self.tools_call_args, self.tools_call_ids = [], [], []
        self.__dict__.update(fields)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.rows = []
        self.scheduler = ModelScheduler({}, lambda *row: self.rows.append(row), clock=self.clock)
        self.addAsyncCleanup(self.scheduler.close)

    async def test_source_identity_facades_and_independent_clients_share_budget(self):
        p = provider('shared', api_base='https://example.invalid', model='m', id='a', key=['do-not-log'])
        clones = [p, copy.copy(p), copy.deepcopy(p)]
        clones[-1].provider_config['id'] = 'separate-prediction-client'
        clones[-1].provider_config['model'] = 'another-model'
        active, peak = [], []
        gate = asyncio.Event()
        async def operation(label):
            active.append(label)
            peak.append(len(active))
            await gate.wait()
            active.remove(label)
            return label
        tasks = [asyncio.create_task(self.scheduler.run(p, lambda i=i: operation(i))) for i, p in enumerate(clones)]
        await until(lambda: len(active) == 2)
        self.assertEqual(self.scheduler.status()['waiting'], 1)
        other = asyncio.create_task(self.scheduler.run(provider('other'), lambda: operation(9)))
        await until(lambda: 9 in active)
        self.assertEqual(len(self.scheduler.status()['providers']), 2)
        self.assertNotIn('do-not-log', repr(self.scheduler.status()))
        gate.set()
        self.assertEqual(await asyncio.gather(*tasks), [0, 1, 2])
        self.assertEqual(await other, 9)
        self.assertEqual(self.scheduler.status()['running'], 0)

    async def test_fallback_config_identity_does_not_use_addresses_or_credentials(self):
        p = SimpleNamespace(provider_config={'id': 'p', 'api_base': 'https://user:secret@EXAMPLE.invalid/v1/?key=secret',
                                            'model': 'm', 'key': ['a']})
        q = copy.deepcopy(p)
        q.client = object()
        q.provider_config.update(key=['b'], api_base='https://example.invalid/v1')
        self.assertEqual(provider_key(p), provider_key(q))
        q.provider_config['id'] = 'different'
        self.assertNotEqual(provider_key(p), provider_key(q))
        q.provider_source_id = 'source'
        p.provider_source_id = 'source'
        self.assertEqual(provider_key(p), provider_key(q))
        self.assertNotIn('secret', provider_key(p))

    async def test_auxiliary_cap_reserves_capacity_and_foreground_precedes_auxiliary(self):
        p = provider()
        gates = {name: asyncio.Event() for name in ('aux1', 'aux2', 'reply', 'review')}
        entered = []
        async def run(name, kind):
            with model_work(kind):
                async def operation():
                    entered.append(name)
                    await gates[name].wait()
                await self.scheduler.run(p, operation)
        a = asyncio.create_task(run('aux1', 'auxiliary'))
        b = asyncio.create_task(run('aux2', 'auxiliary'))
        await until(lambda: entered == ['aux1'])
        c = asyncio.create_task(run('reply', 'interactive'))
        await until(lambda: 'reply' in entered)
        d = asyncio.create_task(run('review', 'interactive'))
        await until(lambda: self.scheduler.status()['waiting'] == 2)
        gates['aux1'].set()
        await until(lambda: 'review' in entered)
        self.assertNotIn('aux2', entered)
        gates['reply'].set()
        await until(lambda: 'aux2' in entered)
        gates['aux2'].set()
        gates['review'].set()
        await asyncio.gather(a, b, c, d)

    async def test_scheduled_promotes_at_sixty_seconds_even_with_interactive_waiters(self):
        self.scheduler.cfg = {'model_concurrency': 1}
        p, gate, entered = provider(), asyncio.Event(), []
        async def first():
            await gate.wait()
        blocker = asyncio.create_task(self.scheduler.run(p, first))
        await until(lambda: self.scheduler.status()['running'] == 1)
        async def record(name):
            entered.append(name)
        scheduled = asyncio.create_task(self.scheduler.run(p, lambda: record('timer'), kind='scheduled'))
        await until(lambda: self.scheduler.status()['waiting'] == 1)
        self.clock.advance(60)
        replies = [asyncio.create_task(self.scheduler.run(p, lambda: record('reply'))) for _ in range(4)]
        await until(lambda: self.scheduler.status()['waiting'] == 5)
        gate.set()
        await asyncio.gather(blocker, scheduled, *replies)
        self.assertEqual(entered, ['timer', 'reply', 'reply', 'reply', 'reply'])

    async def test_unaged_scheduled_waits_after_interactive_before_auxiliary(self):
        self.scheduler.cfg = {'model_concurrency': 1}
        gate, entered = asyncio.Event(), []
        blocker = asyncio.create_task(self.scheduler.run(provider(), gate.wait))
        await until(lambda: self.scheduler.status()['running'] == 1)
        async def record(kind):
            entered.append(kind)
        tasks = [asyncio.create_task(self.scheduler.run(provider(), lambda k=k: record(k), kind=k))
                 for k in ('auxiliary', 'scheduled', 'interactive')]
        await until(lambda: self.scheduler.status()['waiting'] == 3)
        gate.set()
        await asyncio.gather(blocker, *tasks)
        self.assertEqual(entered, ['interactive', 'scheduled', 'auxiliary'])

    async def test_background_needs_continuous_idle_including_group_lock_wait(self):
        gate, entered = asyncio.Event(), asyncio.Event()
        async def operation():
            entered.set()
            await gate.wait()
        bg = asyncio.create_task(self.scheduler.run(provider(), operation, kind='background'))
        await until(lambda: self.scheduler.status()['waiting'] == 1)
        self.clock.advance(4.9)
        self.scheduler.wake()
        self.assertFalse(entered.is_set())
        with self.scheduler.foreground():
            self.clock.advance(20)
            self.scheduler.wake()
            self.assertTrue(self.scheduler.busy())
            self.assertFalse(entered.is_set())
        self.clock.advance(4.99)
        self.scheduler.wake()
        self.assertFalse(entered.is_set())
        self.clock.advance(.01)
        self.scheduler.wake()
        await until(entered.is_set)
        self.assertFalse(self.scheduler.busy())
        gate.set()
        await bg

    async def test_idle_timer_wakes_without_polling(self):
        scheduler = ModelScheduler({}, idle_seconds=.01)
        self.addAsyncCleanup(scheduler.close)
        self.assertEqual(await asyncio.wait_for(scheduler.run(provider(), lambda: asyncio.sleep(0, result=7), kind='background'), 1), 7)
        self.assertIsNone(scheduler._timer)

    async def test_preempt_background_model_only_and_raise_reschedulable_exception(self):
        self.clock.advance(5)
        started, cancelled = asyncio.Event(), asyncio.Event()
        business = []
        async def model():
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        async def business_task():
            try:
                await self.scheduler.run(provider(), model, kind='background')
                business.append('side-effect')
            except BackgroundPaused:
                business.append('requeued')
            business.append('caller-alive')
        bg = asyncio.create_task(business_task())
        await started.wait()
        with self.scheduler.foreground():
            await asyncio.wait_for(bg, 1)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(business, ['requeued', 'caller-alive'])

    async def test_completed_model_does_not_let_preemption_cancel_committing_tool(self):
        self.clock.advance(5)
        committing, finish = asyncio.Event(), asyncio.Event()
        records = []
        async def business():
            with model_work('background'):
                await self.scheduler.run(provider(), lambda: asyncio.sleep(0, result='draft'))
                committing.set()
                await finish.wait()
                records.append('sent-once')
        task = asyncio.create_task(business())
        await committing.wait()
        with self.scheduler.foreground():
            await asyncio.sleep(0)
            self.assertFalse(task.cancelled())
            finish.set()
            await task
        self.assertEqual(records, ['sent-once'])

    async def test_cancellation_suppression_cannot_commit_preempted_model_result(self):
        self.clock.advance(5)
        started = asyncio.Event()
        async def stubborn():
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return 'discard this result'
        bg = asyncio.create_task(self.scheduler.run(provider(), stubborn, kind='background'))
        await started.wait()
        with self.scheduler.foreground():
            with self.assertRaises(BackgroundPaused):
                await bg

    async def test_waiter_cancel_active_cancel_exception_and_retry_release_all_slots(self):
        self.scheduler.cfg = {'model_concurrency': 1}
        entered = asyncio.Event()
        async def model():
            entered.set()
            await asyncio.Future()
        active = asyncio.create_task(self.scheduler.run(provider(), model))
        await entered.wait()
        waiter = asyncio.create_task(self.scheduler.run(provider(), model))
        await until(lambda: self.scheduler.status()['waiting'] == 1)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(self.scheduler.status()['running'], 1)
        active.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await active
        async def bad():
            raise LookupError('fake failure')
        with self.assertRaisesRegex(LookupError, 'fake failure'):
            await self.scheduler.run(provider(), bad)
        self.assertEqual(await self.scheduler.run(provider(), lambda: asyncio.sleep(0, result=23)), 23)
        self.assertFalse(self.scheduler.busy())
        self.assertEqual(self.scheduler.status()['waiting'], 0)

    async def test_nested_same_task_reuses_slot_but_inherited_context_does_not_bypass(self):
        self.scheduler.cfg = {'model_concurrency': 1}
        async def nested():
            return await self.scheduler.run(copy.copy(provider()), lambda: asyncio.sleep(0, result=11))
        self.assertEqual(await asyncio.wait_for(self.scheduler.run(provider(), nested), 1), 11)
        first, release = asyncio.Event(), asyncio.Event()
        children = []
        async def outer():
            children.append(asyncio.create_task(self.scheduler.run(provider(), lambda: asyncio.sleep(0, result=12))))
            first.set()
            await release.wait()
        task = asyncio.create_task(self.scheduler.run(provider(), outer))
        await first.wait()
        await until(lambda: self.scheduler.status()['waiting'] == 1)
        self.assertFalse(children[0].done())
        release.set()
        await task
        self.assertEqual(await children[0], 12)

    async def test_stream_backpressure_partial_close_and_model_exception(self):
        pulled, closed = [], []
        async def stream():
            try:
                for i in range(3):
                    pulled.append(i)
                    yield i
            finally:
                closed.append(True)
        iterator = self.scheduler.stream(provider(), stream)
        self.assertEqual(await anext(iterator), 0)
        await asyncio.sleep(0)
        self.assertEqual(pulled, [0])
        self.assertEqual(self.scheduler.status()['running'], 1)
        await iterator.aclose()
        self.assertEqual(closed, [True])
        self.assertEqual(self.scheduler.status()['running'], 0)
        async def bad_stream():
            yield 'first'
            raise ValueError('stream failure')
        iterator = self.scheduler.stream(provider(), bad_stream)
        self.assertEqual(await anext(iterator), 'first')
        with self.assertRaisesRegex(ValueError, 'stream failure'):
            await anext(iterator)
        self.assertFalse(self.scheduler.busy())

    async def test_background_stream_preempts_during_pull_and_between_yields(self):
        for suspended in (False, True):
            self.clock.advance(5)
            entered, closed = asyncio.Event(), asyncio.Event()
            async def stream():
                try:
                    entered.set()
                    if not suspended:
                        await asyncio.Future()
                    yield 'chunk'
                    await asyncio.Future()
                finally:
                    closed.set()
            iterator = self.scheduler.stream(provider(), stream, kind='background')
            if suspended:
                self.assertEqual(await anext(iterator), 'chunk')
                pull = None
            else:
                pull = asyncio.create_task(anext(iterator))
                await entered.wait()
            with self.scheduler.foreground():
                await until(closed.is_set)
                self.assertEqual(self.scheduler.status()['running'], 0)
                with self.assertRaises(BackgroundPaused):
                    await (pull if pull is not None else anext(iterator))
            await iterator.aclose()

    async def test_close_cancels_only_workers_wakes_waiters_and_is_idempotent(self):
        self.scheduler.cfg = {'model_concurrency': 1}
        entered, closed = asyncio.Event(), asyncio.Event()
        async def operation():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()
        active = asyncio.create_task(self.scheduler.run(provider(), operation))
        await entered.wait()
        waiter = asyncio.create_task(self.scheduler.run(provider(), operation))
        await until(lambda: self.scheduler.status()['waiting'] == 1)
        await self.scheduler.close()
        result = await asyncio.gather(active, waiter, return_exceptions=True)
        self.assertTrue(all(isinstance(item, WorkClosed) for item in result))
        self.assertTrue(closed.is_set())
        self.assertEqual(self.scheduler.status()['running'], 0)
        self.assertEqual(self.scheduler.status()['waiting'], 0)
        with self.assertRaises(WorkClosed):
            await self.scheduler.run(provider(), operation)
        await self.scheduler.close()

    async def test_cancel_stream_consumer_closes_upstream_and_releases_capacity(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        async def stream():
            try:
                entered.set()
                await asyncio.Future()
                yield 'never reached'
            finally:
                closed.set()
        iterator = self.scheduler.stream(provider(), stream)
        consumer = asyncio.create_task(anext(iterator))
        await entered.wait()
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer
        self.assertTrue(closed.is_set())
        self.assertEqual(self.scheduler.status()['running'], 0)
        self.assertEqual(await self.scheduler.run(provider(), lambda: asyncio.sleep(0, result='next')), 'next')

    async def test_close_stream_between_yields_and_idle_waiter_does_not_cancel_consumer(self):
        closed = asyncio.Event()
        async def stream():
            try:
                yield 'one'
                yield 'two'
            finally:
                closed.set()
        iterator = self.scheduler.stream(provider(), stream)
        self.assertEqual(await anext(iterator), 'one')
        background = asyncio.create_task(self.scheduler.run(provider('idle'), lambda: asyncio.sleep(0), kind='background'))
        await until(lambda: self.scheduler.status()['waiting'] == 1)
        await self.scheduler.close()
        self.assertTrue(closed.is_set())
        with self.assertRaises(WorkClosed):
            await anext(iterator)
        with self.assertRaises(WorkClosed):
            await background
        self.assertIsNone(self.scheduler._timer)
        self.assertEqual(self.scheduler.status()['running'], 0)
        await self.scheduler.close()

    async def test_kind_contexts_are_nested_and_task_local(self):
        async def run(kind):
            with model_work(kind):
                await self.scheduler.run(provider(kind), lambda: asyncio.sleep(0))
                with model_work('interactive'):
                    await self.scheduler.run(provider(kind), lambda: asyncio.sleep(0))
                await self.scheduler.run(provider(kind), lambda: asyncio.sleep(0))
        await asyncio.gather(run('auxiliary'), run('scheduled'))
        await self.scheduler.run(provider(), lambda: asyncio.sleep(0))
        kinds = [fields['kind'] for _, fields in self.rows]
        self.assertEqual(kinds.count('auxiliary'), 2)
        self.assertEqual(kinds.count('scheduled'), 2)
        self.assertEqual(kinds.count('interactive'), 3)
        with self.assertRaises(ValueError):
            with model_work('typo'):
                pass


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.rows = Clock(), []
        self.runtime = WorkRuntime({'latency_optimization_enabled': True}, lambda *row: self.rows.append(row), clock=self.clock)
        self.addAsyncCleanup(self.runtime.close)
        self.bot = object()
        self.calls = []

    async def transport(self, bot, action, **params):
        self.calls.append((action, params))
        return {'message_id': len(self.calls)}

    async def send(self, body='final', *, action='send_group_msg', target='100', transport=None, receipt=None, **params):
        target_key = 'user_id' if action == 'send_private_msg' else 'group_id'
        return await self.runtime.transport(transport or self.transport, self.bot, action,
            {target_key: target, 'message': body, **params}, receipt=receipt)

    def deliveries(self):
        return [fields for name, fields in self.rows if name == 'reply_text_delivery']

    async def test_received_to_first_confirmed_final_includes_queue_and_ignores_tail(self):
        event = Event()
        with self.runtime.reply_work(event):
            self.clock.advance(19)  # group/session queue
            self.runtime.mark(event, 'model_started')
            self.clock.advance(10)  # model/review
            self.runtime.mark(event, 'final_ready')
            async def slow_transport(*args, **kwargs):
                self.clock.advance(1)
                return await self.transport(*args, **kwargs)
            await self.send(transport=slow_transport)
            self.clock.advance(15)  # media/history/after-message hooks
            event.set_extra('qqbot_received_at', None)
            await self.send('second part')
        self.assertEqual(len(self.deliveries()), 1)
        self.assertEqual(self.deliveries()[0]['elapsed_ms'], 30000)
        self.assertTrue(self.deliveries()[0]['within_30s'])
        self.assertEqual(self.deliveries()[0]['trace_id'], event.get_extra('qqbot_work_trace_id'))
        self.assertFalse(self.runtime.busy())

    async def test_over_budget_is_reported_honestly(self):
        event = Event(received=69.999)
        self.runtime.mark(event, 'final_ready')
        await self.send()
        self.assertEqual(self.deliveries()[0]['elapsed_ms'], 30001)
        self.assertFalse(self.deliveries()[0]['within_30s'])

    async def test_actual_model_queue_is_included_in_thirty_second_metric(self):
        self.runtime.scheduler.cfg = {'model_concurrency': 1}
        gate = asyncio.Event()
        blocker = asyncio.create_task(self.runtime.scheduler.run(provider(), gate.wait))
        await until(lambda: self.runtime.scheduler.status()['running'] == 1)
        event = Event()
        async def reply():
            with self.runtime.reply_work(event):
                async def model():
                    self.clock.advance(9)
                await self.runtime.scheduler.run(provider(), model)
                self.runtime.mark(event, 'final_ready')
                self.clock.advance(1)
                await self.send()
        task = asyncio.create_task(reply())
        await until(lambda: self.runtime.scheduler.status()['waiting'] == 1)
        self.clock.advance(20)
        gate.set()
        await asyncio.gather(blocker, task)
        self.assertEqual(self.deliveries()[0]['elapsed_ms'], 30000)
        queued = [row for name, row in self.rows if name == 'model_work' and row['queue_ms'] == 20000]
        self.assertEqual(queued[0]['trace_id'], self.deliveries()[0]['trace_id'])

    async def test_unmarked_intermediate_other_target_empty_media_and_async_ack_do_not_qualify(self):
        event = Event()
        with self.runtime.reply_work(event):
            await self.send('intermediate tool send')
            self.assertFalse(self.deliveries())
            self.runtime.mark(event, 'final_ready')
            await self.send('wrong group', target='200')
            await self.send('wrong kind', action='send_private_msg')
            for body in (' ', '[CQ:image,file=x][CQ:at,qq=1]', [{'type': 'reply', 'data': {'id': '1'}}]):
                await self.send(body)
            await self.send('queued', action='send_group_msg_async')
            self.assertFalse(self.deliveries())
            self.clock.advance(1)
            await self.send('actual final')
        self.assertEqual(self.deliveries()[0]['elapsed_ms'], 1000)

    async def test_failures_never_count_and_success_can_retry_once(self):
        self.runtime.mark(Event(), 'final_ready')
        async def failure(*args, **kwargs):
            raise OSError('transport failed')
        with self.assertRaises(OSError):
            await self.send(transport=failure)
        for response in (None, False, {}, {'status': 'failed', 'retcode': 100, 'data': {'message_id': 1}},
                         {'status': 'async', 'retcode': 1}, {'status': 'ok', 'data': None}):
            async def returned(*args, **kwargs):
                return response
            await self.send(transport=returned)
        self.assertFalse(self.deliveries())
        await self.send()
        await self.send()
        self.assertEqual(len(self.deliveries()), 1)

    async def test_explicit_text_binding_rejects_another_send_in_final_phase(self):
        event = Event()
        event.result = SimpleNamespace(chain=text('expected final'))
        self.runtime.mark(event, 'final_ready')
        await self.send('unrelated notification')
        self.assertFalse(self.deliveries())
        await self.send(text('expected final'))
        self.assertEqual(len(self.deliveries()), 1)

    async def test_explicit_final_extra_wins_over_old_result_and_binds_complete_body(self):
        event = Event()
        event.result = SimpleNamespace(chain=text('old tool progress'))
        event.set_extra('qqbot_final_text', 'reviewed final\nsecond paragraph')
        self.runtime.mark(event, 'final_ready')
        for body in ('old tool progress', 'reviewed final', 'second paragraph', 'unrelated notification'):
            await self.send(body)
        self.assertFalse(self.deliveries())
        await self.send(text('reviewed final') + text('second paragraph'))
        self.assertEqual(len(self.deliveries()), 1)

    async def test_explicit_empty_final_extra_does_not_authorize_unrelated_prose(self):
        event = Event()
        event.result = SimpleNamespace(chain=text('stale'))
        event.set_extra('qqbot_final_text', '')
        self.runtime.mark(event, 'final_ready')
        await self.send('stale')
        await self.send('side channel')
        self.assertFalse(self.deliveries())

    async def test_helper_task_final_marker_authorizes_root_but_not_sibling(self):
        event = Event()
        event.set_extra('qqbot_final_text', 'final')
        with self.runtime.reply_work(event):
            async def hook():
                self.runtime.mark(event, 'final_ready')
            await asyncio.create_task(hook())
            await asyncio.create_task(self.send())
            self.assertFalse(self.deliveries())
            await self.send()
        self.assertEqual(len(self.deliveries()), 1)

    async def test_final_marker_during_inflight_progress_is_not_retroactive(self):
        event = Event()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'message_id': 1}
        with self.runtime.reply_work(event):
            async def mark_later():
                await entered.wait()
                self.runtime.mark(event, 'final_ready')
                release.set()
            marker = asyncio.create_task(mark_later())
            await self.send('progress', transport=delayed)
            await marker
            self.assertFalse(self.deliveries())
            await self.send('final')
        self.assertEqual(len(self.deliveries()), 1)

    async def test_fresh_review_receipt_tracks_rewritten_final_and_rejects_blank(self):
        receipt = ContextVar('fake_delivery_receipt', default=None)
        event = Event()
        event.result = SimpleNamespace(chain=text('original'))
        self.runtime.mark(event, 'final_ready')
        async def reviewed(*args, **kwargs):
            receipt.set({'origin': 'aiocqhttp:GroupMessage:100', 'original': 'original', 'text': ''})
            return {'message_id': 1}
        await self.send('original', transport=reviewed, receipt=receipt.get)
        self.assertFalse(self.deliveries())
        async def rewritten(*args, **kwargs):
            receipt.set({'origin': 'aiocqhttp:GroupMessage:100', 'original': 'original', 'text': 'checked'})
            return {'message_id': 2}
        await self.send('original', transport=rewritten, receipt=receipt.get)
        self.assertEqual(len(self.deliveries()), 1)

    async def test_cq_escaping_and_temporary_private_group_context_preserve_target(self):
        event = Event('aiocqhttp:FriendMessage:7')
        event.set_extra('qqbot_final_text', 'hello [literal] & comma,')
        self.runtime.mark(event, 'final_ready')
        await self.send('hello &#91;literal&#93; &amp; comma&#44;[CQ:image,file=x]',
                        action='send_private_msg', target='7', group_id='100')
        self.assertEqual(len(self.deliveries()), 1)

    async def test_fresh_mismatched_receipt_cannot_confirm_expected_text(self):
        receipt = ContextVar('mismatched_receipt', default=None)
        self.runtime.mark(Event(), 'final_ready')
        async def wrong(*args, **kwargs):
            receipt.set({'origin': 'aiocqhttp:GroupMessage:100', 'original': 'another request', 'text': 'different'})
            return {'message_id': 1}
        await self.send('final', transport=wrong, receipt=receipt.get)
        self.assertFalse(self.deliveries())

    async def test_child_tasks_do_not_inherit_final_send_authority_and_sessions_are_isolated(self):
        event = Event()
        with self.runtime.reply_work(event):
            self.runtime.mark(event, 'final_ready')
            await asyncio.create_task(self.send('child notification'))
            self.assertFalse(self.deliveries())
            await self.send('parent final')
        gate = asyncio.Event()
        async def reply(number):
            event = Event(f'aiocqhttp:FriendMessage:{number}', received=90 + number, message_id=str(number))
            with self.runtime.reply_work(event):
                self.runtime.mark(event, 'final_ready')
                await gate.wait()
                await self.send(str(number), action='send_private_msg', target=str(number))
            return event
        tasks = [asyncio.create_task(reply(i)) for i in (1, 2)]
        await until(lambda: self.runtime.status()['foreground'] == 2)
        gate.set()
        events = await asyncio.gather(*tasks)
        self.assertEqual({row['elapsed_ms'] for row in self.deliveries()}, {0, 9000, 8000})
        self.assertEqual(len({e.get_extra('qqbot_work_trace_id') for e in [event, *events]}), 3)
        await self.send('after scope')
        self.assertEqual(len(self.deliveries()), 3)

    async def test_event_identity_changed_under_same_target_cannot_claim_delivery(self):
        event = Event()
        with self.runtime.reply_work(event):
            self.runtime.mark(event, 'final_ready')
            event.message_obj.message_id = 'other-event'
            await self.send()
        self.assertFalse(self.deliveries())

    async def test_existing_tool_receipt_uses_actual_send_time_only_when_final(self):
        event = Event()
        with self.runtime.reply_work(event):
            self.clock.advance(3)
            await self.send('confirmed video')
            event.set_extra('qqbot_bili_sent', {'text': 'confirmed video', 'url': 'https://example.invalid'})
            self.clock.advance(20)
            self.runtime.mark(event, 'tools_finished')
            self.assertFalse(self.deliveries())  # composite request continues
            event.stopped = True
        self.assertEqual(self.deliveries()[0]['elapsed_ms'], 3000)
        self.assertEqual(self.deliveries()[0]['via'], 'tool_receipt')

    async def test_child_tool_receipt_can_confirm_delivery_without_inheriting_final_authority(self):
        event = Event()
        with self.runtime.reply_work(event):
            async def tool():
                self.clock.advance(2)
                await self.send('tool final')
                event.set_extra('qqbot_bili_sent', {'text': 'tool final'})
                event.stopped = True
            await asyncio.create_task(tool())
            self.assertFalse(self.deliveries())
            self.clock.advance(10)
        self.assertEqual(self.deliveries()[0]['elapsed_ms'], 2000)
        self.assertEqual(self.deliveries()[0]['via'], 'tool_receipt')

    async def test_unmatched_stale_receipt_or_missing_arrival_cannot_fake_complete_metric(self):
        event = Event()
        event.set_extra('qqbot_bili_sent', {'text': 'old'})
        with self.runtime.reply_work(event):
            await self.send('different')
            event.stopped = True
        self.assertFalse(self.deliveries())
        self.runtime.mark(Event(received=None), 'final_ready')
        await self.send()
        self.assertFalse(self.deliveries())
        self.assertEqual(self.runtime.status()['incomplete_deliveries'], 1)

    async def test_disabled_runtime_leaves_context_transport_and_audit_untouched(self):
        self.runtime.cfg['latency_optimization_enabled'] = False
        event = Event()
        with self.runtime.reply_work(event):
            self.runtime.mark(event, 'final_ready')
            self.assertFalse(self.runtime.busy())
            self.assertEqual(await self.send(), {'message_id': 1})
        self.assertEqual(event.extras, {'qqbot_received_at': 100})
        self.assertFalse(self.rows)

    async def test_audit_failure_does_not_retry_transport_or_lose_context_cleanup(self):
        def broken(*args):
            raise OSError('audit unavailable')
        self.runtime.audit = broken
        event = Event()
        with self.runtime.reply_work(event):
            self.runtime.mark(event, 'final_ready')
            await self.send()
        self.assertEqual(len(self.calls), 1)
        self.assertGreater(self.runtime.status()['audit_errors'], 0)
        self.assertFalse(self.runtime.busy())

    async def test_pipeline_envelope_retains_fallback_identity_but_passive_events_are_not_busy(self):
        event = Event(received=None)
        with self.runtime.pipeline_work(event):
            self.assertFalse(self.runtime.busy())
            event.set_extra('qqbot_received_at', 100)
            with self.runtime.reply_work(event):
                event.set_extra('qqbot_final_text', 'fallback final')
                self.runtime.mark(event, 'final_ready')
            self.assertTrue(self.runtime.busy())
            self.clock.advance(30)
            await self.send('fallback final')
            self.assertEqual(self.deliveries()[0]['elapsed_ms'], 30000)
        self.assertFalse(self.runtime.busy())
        with self.runtime.pipeline_work(Event()):
            self.assertFalse(self.runtime.busy())

    async def test_pipeline_exception_and_cancel_release_deferred_foreground(self):
        with self.assertRaises(ValueError):
            event = Event()
            with self.runtime.pipeline_work(event):
                with self.runtime.reply_work(event):
                    pass
                raise ValueError('decorate failed')
        self.assertFalse(self.runtime.busy())
        waiting = asyncio.Event()
        async def pipeline():
            event = Event()
            with self.runtime.pipeline_work(event):
                with self.runtime.reply_work(event):
                    pass
                waiting.set()
                await asyncio.Future()
        task = asyncio.create_task(pipeline())
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.runtime.busy())


@contextmanager
def fake_dependencies(stage, provider_class, transport_class):
    names = ['astrbot', 'astrbot.core', 'astrbot.core.pipeline', 'astrbot.core.pipeline.process_stage',
             'astrbot.core.pipeline.process_stage.method', 'astrbot.core.pipeline.process_stage.method.agent_sub_stages',
             'astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal',
             'astrbot.core.provider', 'astrbot.core.provider.sources', 'astrbot.core.provider.sources.openai_source',
             'astrbot.core.provider.entities',
             'astrbot.core.pipeline.scheduler',
             'aiocqhttp', 'data.plugins.astrbot_plugin_proactive_agent.conversation_review']
    modules = {name: ModuleType(name) for name in names}
    modules[names[6]].InternalAgentSubStage = stage
    modules[names[9]].ProviderOpenAIOfficial = provider_class
    modules['astrbot.core.provider.entities'].LLMResponse = FakeResponse
    class Pipeline:
        async def execute(self, event):
            return None
    modules['astrbot.core.pipeline.scheduler'].PipelineScheduler = Pipeline
    modules['aiocqhttp'].CQHttp = transport_class
    modules[names[-1]]._receipt = ContextVar('fake_installed_receipt', default=None)
    with patch.dict(sys.modules, modules):
        yield


class InstallationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.records = []
        class Stage:
            async def process(stage, event, provider_wake_prefix):
                yield event
        class Provider:
            def __init__(p):
                p.provider_config = {'provider_source_id': 'fake'}
            async def text_chat(p, prompt=None, session_id=None, **kwargs):
                return (prompt, session_id, kwargs)
            async def text_chat_stream(p, prompt=None, **kwargs):
                yield prompt
                yield kwargs
        class Transport:
            async def call_action(bot, action, **params):
                return {'message_id': 1}
        self.Stage, self.Provider, self.Transport = Stage, Provider, Transport
        self.bot = Transport()
        self.cfg = {'latency_optimization_enabled': True}
        self.plugin = SimpleNamespace(_cfg=self.cfg.get, _audit=lambda *row: self.records.append(row),
                                      _platform=lambda: SimpleNamespace(bot=self.bot))

    async def test_signatures_returns_streams_installed_order_and_restore(self):
        originals = self.Stage.process, self.Provider.text_chat, self.Provider.text_chat_stream, self.Transport.call_action
        with fake_dependencies(self.Stage, self.Provider, self.Transport):
            restore = install_work_runtime(self.plugin)
            self.addAsyncCleanup(self.plugin._work.close)
            try:
                wrapped = self.Stage.process, self.Provider.text_chat, self.Provider.text_chat_stream, self.Transport.call_action
                self.assertEqual([inspect.signature(f) for f in wrapped], [inspect.signature(f) for f in originals])
                self.assertTrue(inspect.isasyncgenfunction(self.Stage.process))
                self.assertTrue(inspect.isasyncgenfunction(self.Provider.text_chat_stream))
                p = self.Provider()
                self.assertEqual(await p.text_chat('hi', 'session', option=2), ('hi', 'session', {'option': 2}))
                self.assertEqual([x async for x in p.text_chat_stream('hi', extra=3)], ['hi', {'extra': 3}])
                e = Event(received=0)
                async for result in self.Stage().process(e, ''):
                    self.assertIs(result, e)
                    self.assertTrue(self.plugin._work.busy())
                    self.plugin._work.mark(e, 'final_ready')
                    await self.bot.call_action('send_msg', message_type='group', group_id='100', message='final')
                self.assertFalse(self.plugin._work.busy())
                self.assertEqual(sum(name == 'reply_text_delivery' for name, _ in self.records), 1)
            finally:
                restore()
                restore()
            self.assertEqual((self.Stage.process, self.Provider.text_chat, self.Provider.text_chat_stream, self.Transport.call_action), originals)

    async def test_group_and_session_locks_stay_held_across_yield_and_waiters_count(self):
        group_lock, session_lock = asyncio.Lock(), asyncio.Lock()
        entered, release, history = [], asyncio.Event(), []
        async def original(stage, event, provider_wake_prefix):
            async with group_lock:
                async with session_lock:
                    entered.append(event.message_obj.message_id)
                    yield event
                    history.append(event.message_obj.message_id)
        self.Stage.process = original
        with fake_dependencies(self.Stage, self.Provider, self.Transport):
            restore = install_work_runtime(self.plugin)
            self.addAsyncCleanup(self.plugin._work.close)
            async def run(event):
                async for _ in self.Stage().process(event, ''):
                    if event.message_obj.message_id == 'first':
                        await release.wait()
            try:
                first = asyncio.create_task(run(Event(message_id='first')))
                await until(lambda: entered == ['first'])
                second = asyncio.create_task(run(Event(message_id='second')))
                await until(lambda: self.plugin._work.status()['foreground'] == 2)
                self.assertTrue(group_lock.locked())
                self.assertTrue(session_lock.locked())
                self.assertEqual(entered, ['first'])
                self.assertEqual(history, [])
                release.set()
                await asyncio.gather(first, second)
                self.assertEqual(history, ['first', 'second'])
                self.assertFalse(group_lock.locked())
                self.assertFalse(session_lock.locked())
            finally:
                release.set()
                restore()

    async def test_process_cancel_early_close_and_exception_cleanup(self):
        lock = asyncio.Lock()
        async def original(stage, event, provider_wake_prefix):
            async with lock:
                yield event
                if provider_wake_prefix == 'bad':
                    raise ValueError('stage error')
        self.Stage.process = original
        with fake_dependencies(self.Stage, self.Provider, self.Transport):
            restore = install_work_runtime(self.plugin)
            self.addAsyncCleanup(self.plugin._work.close)
            try:
                iterator = self.Stage().process(Event(), '')
                await anext(iterator)
                await iterator.aclose()
                self.assertFalse(lock.locked())
                self.assertFalse(self.plugin._work.busy())
                iterator = self.Stage().process(Event(), 'bad')
                await anext(iterator)
                with self.assertRaisesRegex(ValueError, 'stage error'):
                    await anext(iterator)
                self.assertFalse(lock.locked())
                self.assertFalse(self.plugin._work.busy())
                await lock.acquire()
                task = asyncio.create_task(anext(self.Stage().process(Event(), '')))
                await until(lambda: self.plugin._work.busy())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(self.plugin._work.busy())
                lock.release()
            finally:
                restore()

    async def test_nested_same_class_chat_to_stream_does_not_deadlock(self):
        self.cfg['model_concurrency'] = 1
        async def nested(provider, prompt=None, **kwargs):
            return [part async for part in provider.text_chat_stream(prompt, **kwargs)]
        self.Provider.text_chat = nested
        with fake_dependencies(self.Stage, self.Provider, self.Transport):
            restore = install_work_runtime(self.plugin)
            self.addAsyncCleanup(self.plugin._work.close)
            try:
                self.assertEqual(await asyncio.wait_for(self.Provider().text_chat('nested', x=1), 1), ['nested', {'x': 1}])
            finally:
                restore()

    async def test_initial_false_can_enable_and_disable_without_reinstalling(self):
        self.cfg['latency_optimization_enabled'] = False
        originals = self.Stage.process, self.Provider.text_chat, self.Provider.text_chat_stream, self.Transport.call_action
        with fake_dependencies(self.Stage, self.Provider, self.Transport):
            restore = install_work_runtime(self.plugin)
            self.addAsyncCleanup(self.plugin._work.close)
            try:
                self.assertIsInstance(self.plugin._work, WorkRuntime)
                self.assertIsNot(self.Provider.text_chat, originals[1])
                p = self.Provider()
                p.client = object()  # Cannot be cloned; disabled calls must never touch it.
                self.assertEqual(await p.text_chat('old'), ('old', None, {}))
                self.assertEqual([row async for row in p.text_chat_stream('old')], ['old', {}])
                event = Event()
                async for _ in self.Stage().process(event, ''):
                    self.plugin._work.mark(event, 'final_ready')
                    self.assertFalse(self.plugin._work.busy())
                self.assertEqual(event.extras, {'qqbot_received_at': 100})
                self.assertFalse(self.records)
                platform = self.plugin._platform
                def no_lookup():
                    raise AssertionError('Disabled wrapper must pass straight through')
                self.plugin._platform = no_lookup
                self.assertEqual(await self.bot.call_action('send_group_msg', group_id='100', message='old'), {'message_id': 1})
                self.plugin._platform = platform
                self.cfg['latency_optimization_enabled'] = True
                self.assertEqual(await self.Provider().text_chat('new'), ('new', None, {}))
                self.assertEqual(len(self.plugin._work.status()['providers']), 1)
                self.assertTrue(self.records)
                event = Event(received=0)
                async for _ in self.Stage().process(event, ''):
                    event.set_extra('qqbot_final_text', 'new final')
                    self.plugin._work.mark(event, 'final_ready')
                    await self.bot.call_action('send_group_msg', group_id='100', message='new final')
                self.assertEqual(sum(name == 'reply_text_delivery' for name, _ in self.records), 1)
                count = len(self.records)
                self.cfg['latency_optimization_enabled'] = False
                self.assertEqual(await p.text_chat('old again'), ('old again', None, {}))
                self.assertEqual(len(self.records), count)
            finally:
                restore()
            self.assertEqual((self.Stage.process, self.Provider.text_chat, self.Provider.text_chat_stream, self.Transport.call_action), originals)

    async def test_same_source_cloned_sdk_clients_do_not_mutate_original_credentials(self):
        class Client:
            def __init__(self, credential):
                self.api_key = credential
            def with_options(self):
                return Client(self.api_key)
        gate = asyncio.Event()
        seen = []
        async def rotate(p, prompt=None, **kwargs):
            p.client.api_key = prompt  # mimic upstream rotation, never our scheduler
            seen.append(p.client)
            await gate.wait()
            return p.client.api_key
        self.Provider.text_chat = rotate
        p = self.Provider()
        p.client = Client('original')
        p.api_keys = ['original', 'alternate']
        with fake_dependencies(self.Stage, self.Provider, self.Transport):
            restore = install_work_runtime(self.plugin)
            self.addAsyncCleanup(self.plugin._work.close)
            try:
                tasks = [asyncio.create_task(q.text_chat(key)) for q, key in ((p, 'a'), (copy.copy(p), 'b'))]
                await until(lambda: len(seen) == 2)
                self.assertIsNot(seen[0], seen[1])
                self.assertEqual(p.client.api_key, 'original')
                self.assertEqual(p.api_keys, ['original', 'alternate'])
                gate.set()
                self.assertEqual(await asyncio.gather(*tasks), ['a', 'b'])
            finally:
                gate.set()
                restore()

    async def test_actual_openai_sdk_resources_bind_to_independent_client_without_network(self):
        try:
            from openai import AsyncOpenAI
            import httpx
        except ImportError:
            self.skipTest('Optional OpenAI SDK is not installed')
        async def no_network(request):
            raise AssertionError('No network requests permitted in scheduler tests')
        http = httpx.AsyncClient(transport=httpx.MockTransport(no_network))
        client = AsyncOpenAI(api_key='fake-offline', base_url='https://offline.invalid', http_client=http)
        try:
            _ = client.chat.completions  # exercise cached resource copying
            p = self.Provider()
            p.client = client
            clone = _isolated_provider(p)
            self.assertIsNot(clone.client, client)
            self.assertIs(clone.client._client, http)
            self.assertIs(clone.client.chat.completions._client, clone.client)
            clone.client.api_key = 'fake-rotated'
            self.assertEqual(client.api_key, 'fake-offline')
        finally:
            await client.close()


class HedgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls, self.stream_calls, self.timers, self.rows, self.parents = [], [], [], [], []
        self.cfg = {'latency_optimization_enabled': True}
        test = self
        class Client:
            def __init__(self):
                self.api_key = 'offline-only'
            def with_options(self):
                result = Client()
                result.api_key = self.api_key
                return result
        class Provider:
            def __init__(p):
                p.provider_config = {'provider_source_id': 'hedge-source', 'model': 'same-model',
                                     'custom_extra_body': {'reasoning_effort': 'high'}}
                p.client, p.api_keys = Client(), ['offline-only']
            async def text_chat(p, prompt=None, session_id=None, image_urls=None, audio_urls=None,
                                func_tool=None, contexts=None, system_prompt=None, tool_calls_result=None,
                                model=None, extra_user_content_parts=None, tool_choice='auto',
                                request_max_retries=None, **kwargs):
                params = dict(prompt=prompt, session_id=session_id, image_urls=image_urls, audio_urls=audio_urls,
                    func_tool=func_tool, contexts=contexts, system_prompt=system_prompt, tool_calls_result=tool_calls_result,
                    model=model, extra_user_content_parts=extra_user_content_parts, tool_choice=tool_choice,
                    request_max_retries=request_max_retries, **kwargs)
                index = len(test.calls)
                test.calls.append((p, params))
                return await test.operation(index, p, params)
            async def text_chat_stream(p, prompt=None, system_prompt=None, **kwargs):
                test.stream_calls.append((prompt, system_prompt, kwargs))
                yield FakeResponse('stream')
        class Stage:
            async def process(self, event, provider_wake_prefix):
                yield event
        class Transport:
            async def call_action(self, action, **params):
                raise AssertionError('Hedging must never send messages')
        self.Provider, self.provider = Provider, Provider()
        self.original = Provider.text_chat
        self.dependencies = fake_dependencies(Stage, Provider, Transport)
        self.dependencies.__enter__()
        self.plugin = SimpleNamespace(_cfg=self.cfg.get, _audit=lambda *row: self.rows.append(row), _platform=lambda: None)
        self.restore = install_work_runtime(self.plugin)
        self.runtime = self.plugin._work
        async def timer(seconds):
            gate = asyncio.Event()
            self.timers.append((seconds, gate))
            await gate.wait()
        self.runtime._hedge_sleep = timer
        async def default(*args):
            return FakeResponse()
        self.operation = default

    async def asyncTearDown(self):
        for task in self.parents:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.parents, return_exceptions=True)
        self.restore()
        await self.runtime.close()
        self.dependencies.__exit__(None, None, None)

    def start(self, **kwargs):
        async def turn():
            with self.runtime.reply_work(Event()):
                return await self.provider.text_chat(**{'prompt': 'hello', 'system_prompt': 'system\n' + TAIL_GUARD_MARKER, **kwargs})
        task = asyncio.create_task(turn())
        self.parents.append(task)
        return task

    async def trigger(self, index=0):
        await until(lambda: len(self.timers) > index)
        self.assertEqual(self.timers[index][0], self.cfg.get('reply_hedge_after_seconds', 15))
        self.timers[index][1].set()

    def stats(self):
        return self.runtime.status()['hedge']

    async def test_fast_primary_strips_marker_and_preserves_signature_and_response(self):
        response = FakeResponse('a full response', usage={'tokens': 77}, raw_completion=object())
        async def operation(*args):
            return response
        self.operation = operation
        self.assertIs(await self.start(), response)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]['system_prompt'], 'system\n')
        self.assertEqual(inspect.signature(self.Provider.text_chat), inspect.signature(self.original))
        self.assertEqual(self.stats(), {'hedged': 0, 'won': 0, 'failed': 0})
        self.assertFalse(self.runtime._hedge_tasks)

    async def test_backup_wins_identical_frozen_request_with_tools_and_independent_credentials(self):
        cancelled, first_ready = asyncio.Event(), asyncio.Event()
        response = FakeResponse('full answer', usage={'tokens': 1000})
        class Tools:
            def __deepcopy__(self, memo):
                raise AssertionError('Do not copy handlers bound to the application')
            def call(self):
                raise AssertionError('Do not execute tools')
        tool = Tools()
        contexts = [{'role': 'user', 'content': 'original'}]
        async def operation(index, p, params):
            p.client.api_key = str(index)  # Upstream rotation stays call-local.
            if index == 0:
                params['contexts'][0]['content'] = 'mutation in primary'
                params['extra_body']['reasoning_effort'] = 'mutation in primary'
                p.provider_config['model'] = 'mutation in primary'
                first_ready.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            self.assertEqual(params['contexts'], contexts)
            self.assertEqual(params['extra_body'], {'reasoning_effort': 'high'})
            self.assertEqual(p.provider_config['model'], 'same-model')
            self.assertEqual(p.provider_config['custom_extra_body'], {'reasoning_effort': 'high'})
            self.assertIs(params['func_tool'], tool)
            self.assertEqual(params['model'], 'same-override')
            self.assertEqual(params['tool_choice'], 'required')
            self.assertEqual(params['request_max_retries'], 2)
            self.assertEqual(params['session_id'], 'same-session')
            self.assertEqual(params['conversation_id'], 'same-conversation')
            return response
        self.operation = operation
        task = self.start(contexts=contexts, func_tool=tool, model='same-override', tool_choice='required',
                          request_max_retries=2, session_id='same-session', conversation_id='same-conversation',
                          extra_body={'reasoning_effort': 'high'})
        await first_ready.wait()
        await self.trigger()
        self.assertIs(await task, response)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.provider.client.api_key, 'offline-only')
        self.assertIsNot(self.calls[0][0].client, self.calls[1][0].client)
        self.assertEqual(self.stats(), {'hedged': 1, 'won': 1, 'failed': 0})
        self.assertEqual({r['kind'] for name, r in self.rows if name == 'model_work'}, {'interactive', 'auxiliary'})
        self.assertNotIn('full answer', repr(self.rows))
        self.assertNotIn('offline-only', repr(self.rows))

    async def test_primary_tool_result_wins_unchanged_and_loser_is_reaped(self):
        gate, cancelled = asyncio.Event(), asyncio.Event()
        response = FakeResponse('', role='tool', tools_call_name=['search'], tools_call_args=[{}], tools_call_ids=['call-1'])
        async def operation(index, *args):
            if index == 0:
                await gate.wait()
                return response
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        self.operation = operation
        task = self.start(func_tool=object())
        await self.trigger()
        await until(lambda: len(self.calls) == 2)
        gate.set()
        self.assertIs(await task, response)
        self.assertEqual(response.tools_call_name, ['search'])
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.stats(), {'hedged': 1, 'won': 0, 'failed': 0})

    async def test_primary_failure_waits_for_successful_backup(self):
        first, second = asyncio.Event(), asyncio.Event()
        response = FakeResponse('recovered')
        async def operation(index, *args):
            await (first if index == 0 else second).wait()
            if index == 0:
                raise OSError('primary failure')
            return response
        self.operation = operation
        task = self.start()
        await self.trigger()
        await until(lambda: len(self.calls) == 2)
        first.set()
        await until(lambda: self.runtime.scheduler.status()['running'] == 1)
        self.assertFalse(task.done())
        second.set()
        self.assertIs(await task, response)
        self.assertEqual(self.stats()['won'], 1)

    async def test_invalid_backup_waits_for_original_and_counts_failure_without_text(self):
        gate = asyncio.Event()
        response = FakeResponse('primary complete')
        async def operation(index, *args):
            if index:
                return FakeResponse('', reasoning_content='not a final answer')
            await gate.wait()
            return response
        self.operation = operation
        task = self.start()
        await self.trigger()
        await until(lambda: self.stats()['failed'] == 1)
        self.assertFalse(task.done())
        gate.set()
        self.assertIs(await task, response)
        self.assertEqual(self.stats(), {'hedged': 1, 'won': 0, 'failed': 1})
        self.assertNotIn('not a final answer', repr(self.rows))

    async def test_both_fail_preserves_primary_exception(self):
        gate = asyncio.Event()
        error = LookupError('original exception object')
        async def operation(index, *args):
            if index:
                raise OSError('backup failed')
            await gate.wait()
            raise error
        self.operation = operation
        task = self.start()
        await self.trigger()
        await until(lambda: self.stats()['failed'] == 1)
        gate.set()
        with self.assertRaises(LookupError) as caught:
            await task
        self.assertIs(caught.exception, error)
        self.assertEqual(self.stats(), {'hedged': 1, 'won': 0, 'failed': 1})
        self.assertFalse(self.runtime._hedge_tasks)

    async def test_error_response_is_not_a_winner_and_image_response_is_preserved(self):
        gate = asyncio.Event()
        Image = type('Image', (), {})
        image = Image()
        response = FakeResponse('', result_chain=SimpleNamespace(chain=[image]), qqbot_generated_image=True)
        async def operation(index, *args):
            if index == 0:
                await gate.wait()
                return FakeResponse('error prose', role='err')
            await gate.wait()
            return response
        self.operation = operation
        task = self.start()
        await self.trigger()
        await until(lambda: len(self.calls) == 2)
        gate.set()
        self.assertIs(await task, response)
        self.assertIs(response.result_chain.chain[0], image)
        self.assertTrue(response.qqbot_generated_image)

    async def test_response_validation_rejects_chunks_empty_and_malformed_tools(self):
        for response in (None, 'text', FakeResponse(''), FakeResponse('chunk', is_chunk=True),
                         FakeResponse('error', role='err'), FakeResponse('', role='tool'),
                         FakeResponse('', role='tool', tools_call_name=['a'], tools_call_args=[{}], tools_call_ids=[])):
            with self.subTest(response=type(response).__name__):
                self.assertFalse(_valid_model_response(response, FakeResponse))
        self.operation = lambda *args: asyncio.sleep(0, result=FakeResponse(''))
        with self.assertRaises(InvalidModelResponse):
            await self.start()
        self.assertEqual(len(self.calls), 1)  # A failure before the deadline is not retried by this feature.

    async def test_disabled_unmarked_and_stream_calls_keep_single_call_semantics(self):
        raw = object()
        self.operation = lambda *args: asyncio.sleep(0, result=raw)
        self.assertIs(await self.start(system_prompt='unmarked'), raw)
        for field in ('reply_hedge_after_seconds', 'latency_optimization_enabled'):
            self.cfg[field] = 0
            self.assertIs(await self.start(), raw)
            self.cfg.pop(field)
            self.cfg['latency_optimization_enabled'] = True
        for enabled in (True, False):
            self.cfg['latency_optimization_enabled'] = enabled
            with self.runtime.reply_work(Event()):
                chunks = [r async for r in self.provider.text_chat_stream('hello', system_prompt=TAIL_GUARD_MARKER)]
            self.assertEqual(len(chunks), 1)
            self.assertEqual(self.stream_calls[-1][1], '')
        self.assertTrue(all(TAIL_GUARD_MARKER not in params['system_prompt'] for _, params in self.calls))
        self.assertFalse(self.timers)
        self.assertEqual(self.stats()['hedged'], 0)

    async def test_guard_excludes_media_current_tools_and_noninteractive_work(self):
        raw = object()
        self.operation = lambda *args: asyncio.sleep(0, result=raw)
        cases = [dict(image_urls=['image']), dict(audio_urls=['audio']), dict(tool_calls_result=[object()]),
                 dict(contexts=[{'role': 'user'}, {'role': 'assistant', 'tool_calls': [{'id': 'x'}]}]),
                 dict(contexts=[{'role': 'user'}, {'role': 'tool', 'content': 'done'}]),
                 dict(contexts=[SimpleNamespace(role='user'), SimpleNamespace(role='tool')])]
        for params in cases:
            self.assertIs(await self.start(**params), raw)
        for kind in ('auxiliary', 'scheduled'):
            with model_work(kind):
                self.assertIs(await self.start(), raw)
        self.assertIs(await self.provider.text_chat(system_prompt=TAIL_GUARD_MARKER), raw)  # no bound reply turn
        self.assertFalse(self.timers)
        self.assertEqual(self.stats()['hedged'], 0)

    async def test_old_tools_before_last_user_do_not_block_and_positional_marker_is_removed(self):
        contexts = [dict(role='assistant', tool_calls=[{}]), dict(role='tool'), dict(role='user', content='new turn')]
        async def operation(index, *args):
            if not index:
                await asyncio.Future()
            return FakeResponse()
        self.operation = operation
        async def turn():
            with self.runtime.reply_work(Event()):
                return await self.provider.text_chat('hello', 's', None, None, None, contexts, 'prefix' + TAIL_GUARD_MARKER)
        task = asyncio.create_task(turn())
        self.parents.append(task)
        await self.trigger()
        await task
        self.assertEqual([params['system_prompt'] for _, params in self.calls], ['prefix', 'prefix'])

    async def test_real_runner_cancellation_event_with_waiter_is_shared_without_copying_future(self):
        abort = asyncio.Event()
        waiter = asyncio.create_task(abort.wait())
        self.parents.append(waiter)
        await until(lambda: bool(abort._waiters))
        async def operation(index, p, params):
            self.assertIs(params['abort_signal'], abort)
            if index == 0:
                await asyncio.Future()
            return FakeResponse('complete')
        self.operation = operation
        task = self.start(abort_signal=abort)
        await self.trigger()
        self.assertEqual((await task).completion_text, 'complete')
        self.assertEqual(self.stats()['hedged'], 1)
        self.assertFalse(abort.is_set())
        abort.set()
        await waiter

    async def test_runner_context_marker_is_cleaned_even_when_disabled_and_in_stream(self):
        contexts = [{'role': 'system', 'content': 'fixed\n' + TAIL_GUARD_MARKER},
                    {'role': 'user', 'content': 'current'}]
        for enabled in (False, True):
            self.cfg['latency_optimization_enabled'] = enabled
            with self.runtime.reply_work(Event()):
                await self.provider.text_chat(contexts=contexts)
                chunks = [item async for item in self.provider.text_chat_stream(contexts=contexts)]
            self.assertEqual(len(chunks), 1)
            self.assertEqual(self.calls[-1][1]['contexts'][0]['content'], 'fixed\n')
            self.assertEqual(self.stream_calls[-1][2]['contexts'][0]['content'], 'fixed\n')
        self.assertIn(TAIL_GUARD_MARKER, contexts[0]['content'])
        self.assertEqual(self.stats()['hedged'], 0)

    async def test_single_turn_uses_one_backup_across_later_calls_and_facades(self):
        later, release = asyncio.Event(), asyncio.Event()
        async def operation(index, *args):
            if index == 0:
                await asyncio.Future()
            if index == 2:
                later.set()
                await release.wait()
            return FakeResponse()
        self.operation = operation
        async def turn():
            with self.runtime.reply_work(Event()):
                await self.provider.text_chat(system_prompt=TAIL_GUARD_MARKER)
                await copy.copy(self.provider).text_chat(system_prompt=TAIL_GUARD_MARKER)
        task = asyncio.create_task(turn())
        self.parents.append(task)
        await self.trigger()
        await later.wait()
        self.assertEqual(len(self.timers), 1)
        release.set()
        await task
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.stats()['hedged'], 1)

    async def test_sibling_requests_share_atomic_turn_budget(self):
        self.cfg['model_concurrency'] = 3
        release = asyncio.Event()
        async def operation(index, *args):
            if index < 2:
                await release.wait()
            return FakeResponse()
        self.operation = operation
        async def turn():
            with self.runtime.reply_work(Event()):
                await asyncio.gather(*(self.provider.text_chat(system_prompt=TAIL_GUARD_MARKER) for _ in range(2)))
        task = asyncio.create_task(turn())
        self.parents.append(task)
        await self.trigger(0)
        await self.trigger(1)
        await until(lambda: self.stats()['won'] == 1)
        self.assertEqual(len(self.calls), 3)
        release.set()
        await task
        self.assertEqual(self.stats()['hedged'], 1)

    async def test_timer_starts_after_primary_admission_and_waiting_backup_is_skipped_on_success(self):
        self.cfg['model_concurrency'] = 1
        blocker_gate, primary_gate = asyncio.Event(), asyncio.Event()
        blocker = asyncio.create_task(self.runtime.scheduler.run(self.provider, blocker_gate.wait))
        self.parents.append(blocker)
        await until(lambda: self.runtime.scheduler.status()['running'] == 1)
        async def operation(*args):
            await primary_gate.wait()
            return FakeResponse()
        self.operation = operation
        task = self.start()
        await until(lambda: self.runtime.scheduler.status()['waiting'] == 1)
        self.assertFalse(self.timers)
        self.assertFalse(self.calls)
        blocker_gate.set()
        await blocker
        await self.trigger()
        await until(lambda: self.runtime.scheduler.status()['waiting'] == 1)
        primary_gate.set()
        await task
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.stats()['hedged'], 0)
        self.assertEqual(self.runtime.scheduler.status()['waiting'], 0)

    async def test_waiting_backup_still_runs_if_primary_failed(self):
        self.cfg['model_concurrency'] = 1
        gate = asyncio.Event()
        async def operation(index, *args):
            if index == 0:
                await gate.wait()
                raise OSError('failed after hedge deadline')
            return FakeResponse()
        self.operation = operation
        task = self.start()
        await self.trigger()
        await until(lambda: self.runtime.scheduler.status()['waiting'] == 1)
        gate.set()
        await task
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.stats()['won'], 1)

    async def test_queued_backup_cannot_jump_a_mandatory_review(self):
        blocker_gate, review_gate, primary_gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
        blocker = asyncio.create_task(self.runtime.scheduler.run(self.provider, blocker_gate.wait))
        self.parents.append(blocker)
        async def operation(*args):
            await primary_gate.wait()
            return FakeResponse()
        self.operation = operation
        task = self.start()
        await self.trigger()
        await until(lambda: self.runtime.scheduler.status()['waiting'] == 1)
        review_started = asyncio.Event()
        async def review():
            review_started.set()
            await review_gate.wait()
        reviewer = asyncio.create_task(self.runtime.scheduler.run(self.provider, review))
        self.parents.append(reviewer)
        await until(lambda: self.runtime.scheduler.status()['waiting'] == 2)
        blocker_gate.set()
        await review_started.wait()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.runtime.scheduler.status()['running'], 2)
        primary_gate.set()
        await task
        review_gate.set()
        await reviewer
        self.assertEqual(len(self.calls), 1)

    async def test_parent_cancellation_drains_both_requests_and_timer(self):
        cancelled = []
        async def operation(index, *args):
            try:
                await asyncio.Future()
            finally:
                cancelled.append(index)
        self.operation = operation
        task = self.start()
        await self.trigger()
        await until(lambda: len(self.calls) == 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(set(cancelled), {0, 1})
        self.assertFalse(self.runtime._hedge_tasks)
        self.assertEqual(self.runtime.scheduler.status()['running'], 0)
        self.assertEqual(self.runtime.scheduler.status()['waiting'], 0)

    async def test_parent_cancel_before_threshold_does_not_create_backup(self):
        self.operation = lambda *args: asyncio.Future()
        task = self.start()
        await until(lambda: bool(self.timers))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.runtime._hedge_tasks)

    async def test_close_drains_hedge_tasks_without_cancelling_business_parent(self):
        self.operation = lambda *args: asyncio.Future()
        task = self.start()
        await self.trigger()
        await until(lambda: len(self.calls) == 2)
        await self.runtime.close()
        with self.assertRaises(WorkClosed):
            await task
        self.assertFalse(task.cancelled())
        self.assertFalse(self.runtime._hedge_tasks)
        self.assertEqual(self.runtime.scheduler.status()['running'], 0)

    async def test_dynamic_disable_prevents_backup_and_invalid_config_fails_closed(self):
        gate = asyncio.Event()
        async def operation(*args):
            await gate.wait()
            return FakeResponse()
        self.operation = operation
        task = self.start()
        await until(lambda: bool(self.timers))
        self.cfg['reply_hedge_after_seconds'] = 0
        self.timers[0][1].set()
        gate.set()
        await task
        self.assertEqual(len(self.calls), 1)
        for value in (0, -1, float('nan'), float('inf'), None, True, 'bad'):
            self.cfg['reply_hedge_after_seconds'] = value
            self.assertEqual(self.runtime._hedge_seconds(), 0)

    async def test_optimization_switch_during_queued_backup_keeps_marker_private_and_stops_duplicate(self):
        self.cfg['model_concurrency'] = 1
        gate = asyncio.Event()
        async def operation(index, p, params):
            self.assertNotIn(TAIL_GUARD_MARKER, params['system_prompt'])
            await gate.wait()
            raise OSError('original request failed')
        self.operation = operation
        task = self.start()
        await self.trigger()
        await until(lambda: self.runtime.scheduler.status()['waiting'] == 1)
        self.cfg['latency_optimization_enabled'] = False
        gate.set()
        with self.assertRaises(OSError):
            await task
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.stats()['hedged'], 0)
        self.assertFalse(self.runtime._hedge_tasks)

    async def test_uncopyable_optional_payload_falls_back_without_duplicate_or_changed_arguments(self):
        class Payload:
            def __deepcopy__(self, memo):
                raise TypeError('uncopyable data')
        payload = Payload()
        response = FakeResponse()
        async def operation(index, p, params):
            self.assertIs(params['opaque'], payload)
            return response
        self.operation = operation
        self.assertIs(await self.start(opaque=payload), response)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.timers)


class RealPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_astrbot_processor_decorate_respond_and_onebot_lifetimes(self):
        if importlib.util.find_spec('astrbot') is None:
            self.skipTest('Optional AstrBot installation is not available')
        # AstrBot initializes paths on import. A fresh process in a temporary
        # directory keeps its imports/databases/logs away from live deployment.
        # Only agent generation and the final QQ transport are fakes; the actual
        # processor, nested process stages, decoration, responder and adapter run.
        script = r'''
import asyncio
import copy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0, sys.argv[1])
from aiocqhttp import CQHttp
from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult, ResultContentType
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.core.pipeline.process_stage.method.agent_request import AgentRequestSubStage
from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.pipeline.respond.stage import RespondStage
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.utils.metrics import Metric
from astrbot.core.utils.session_lock import session_lock_manager
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import WorkRuntime, install_work_runtime

async def main():
    clock, rows, sequence = [100.0], [], []
    mode = ''
    event = None
    owner = asyncio.current_task()
    bot = object.__new__(CQHttp)
    plugin = SimpleNamespace(_cfg={'latency_optimization_enabled': True}.get,
                             _audit=lambda *r: rows.append(r),
                             _platform=lambda: SimpleNamespace(bot=bot))
    plugin._work = WorkRuntime(plugin._cfg, plugin._audit, clock=lambda: clock[0])
    async def transport(*, action, **params):
        assert action == 'send_group_msg' and params['group_id'] == 100
        assert plugin._work.busy() and plugin._work._scope().trace.live
        assert asyncio.current_task() is owner
        locks = session_lock_manager._get_loop_manager()._locks
        held = event.unified_msg_origin in locks and locks[event.unified_msg_origin].locked()
        assert held == (mode != 'fallback'), (mode, held)
        sequence.append('send')
        clock[0] += 1
        return {'message_id': 123}
    bot._api = SimpleNamespace(call_action=transport)

    async def generated(stage, e, prefix):
        # Mirrors both actual InternalAgentSubStage shapes: yield with the
        # session lock held, and run_agent's exception/fallback result without yield.
        e.set_extra('qqbot_received_at', 100.0)
        async with session_lock_manager.acquire_lock(e.unified_msg_origin):
            sequence.append('internal')
            clock[0] += 29
            e.set_extra('qqbot_final_text', 'final body')
            plugin._work.mark(e, 'final_ready')
            if mode == 'stream':
                async def stream():
                    yield MessageChain([Plain('final body')])
                e.set_result(MessageEventResult().set_result_content_type(
                    ResultContentType.STREAMING_RESULT).set_async_stream(stream()))
            else:
                e.set_result(MessageEventResult().message('final body').set_result_content_type(ResultContentType.LLM_RESULT))
            if mode != 'fallback':
                yield None
            sequence.append('history')

    async def after_sent(*args, **kwargs):
        delivery = [r for name, r in rows if name == 'reply_text_delivery']
        assert len(delivery) == 1 and delivery[0]['elapsed_ms'] == 30000, rows
        sequence.append('after_sent')
        clock[0] += 15  # This tail must not move the already captured send timestamp.
        return False

    config = copy.deepcopy(DEFAULT_CONFIG)
    config['provider_settings'].update(enable=True, unsupported_streaming_strategy='turn_off', display_reasoning_text=False)
    config['platform_settings'].update(reply_prefix='', reply_with_mention=False, reply_with_quote=False, forward_threshold=10000)
    config['platform_settings']['segmented_reply']['enable'] = False
    config['provider_tts_settings']['enable'] = False
    config['content_safety']['also_use_in_response'] = False
    config['t2i'] = False
    ctx = SimpleNamespace(astrbot_config=config, plugin_manager=SimpleNamespace(
        context=SimpleNamespace(get_using_tts_provider_async=AsyncMock(return_value=None))))
    process = ProcessStage()
    process.ctx = ctx
    agent = AgentRequestSubStage()
    agent.ctx, agent.prov_wake_prefix = ctx, ''
    agent.agent_sub_stage = InternalAgentSubStage()
    process.agent_sub_stage = agent
    decorate, respond = ResultDecorateStage(), RespondStage()
    await decorate.initialize(ctx)
    await respond.initialize(ctx)
    scheduler = object.__new__(PipelineScheduler)
    scheduler.ctx, scheduler.stages = ctx, [process, decorate, respond]
    with patch.object(InternalAgentSubStage, 'process', generated), \
         patch.object(SessionServiceManager, 'should_process_llm_request', AsyncMock(return_value=True)), \
         patch.object(Metric, 'upload', AsyncMock()), \
         patch('astrbot.core.pipeline.respond.stage.call_event_hook', after_sent):
        original_execute = PipelineScheduler.execute
        restore = install_work_runtime(plugin)
        try:
            for mode in ('yield', 'fallback', 'stream'):
                clock[0] = 100.0
                rows.clear()
                sequence.clear()
                msg = AstrBotMessage()
                msg.type, msg.self_id, msg.session_id = MessageType.GROUP_MESSAGE, '999', '42_100'
                msg.message_id, msg.group_id, msg.sender = mode, '100', MessageMember('42', 'offline')
                msg.message, msg.message_str, msg.raw_message = [Plain('hello')], 'hello', None
                event = AiocqhttpMessageEvent('hello', msg, PlatformMetadata('aiocqhttp', 'offline', 'aiocqhttp'), '42_100', bot)
                event.is_at_or_wake_command = True
                await scheduler.execute(event)
                delivery = [r for name, r in rows if name == 'reply_text_delivery']
                assert len(delivery) == 1 and delivery[0]['elapsed_ms'] == 30000, (mode, rows)
                assert (sequence.index('send') < sequence.index('history')) == (mode != 'fallback'), sequence
                assert not plugin._work.busy(), plugin._work.status()
                assert not event.get_extra('_qqbot_work_state').live
                print('verified actual pipeline:', mode)
        finally:
            restore()
            assert PipelineScheduler.execute is original_execute
            await plugin._work.close()
        await asyncio.sleep(0)  # drain mocked telemetry tasks

asyncio.run(main())
'''
        with tempfile.TemporaryDirectory(prefix='qqbot-work-pipeline-') as directory:
            result = await asyncio.to_thread(subprocess.run,
                [sys.executable, '-B', '-c', script, str(ROOT / 'AstrBot')],
                cwd=directory, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('verified actual pipeline:'), 3)


if __name__ == '__main__':
    unittest.main()
