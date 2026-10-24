"""Offline read-concurrency tests. No plugin instance, credentials or real HTTP."""
import asyncio
import copy
from contextvars import ContextVar
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AstrBot'))
from data.plugins.astrbot_plugin_bili_scroll import client as module
from data.plugins.astrbot_plugin_bili_scroll.client import BiliClient, BiliUnavailable, _ReadSlots
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import model_work


PRIORITY = ContextVar('bili_test_priority', default=0)
ITEM = {'bvid': 'BV1xx411c7mD', 'aid': 123, 'title': '测试抽象视频',
        'duration': 180, 'pubdate': 1700000000, 'play': 1000, 'review': 100}
COMMENTS = [{'rpid': i + 1, 'content': {'message': text}} for i, text in enumerate(
    ['@甲', '@乙', '@丙 请你吃', '抽象绷不住', '甲', '乙', '丙', '丁', '戊', '己'])]


def payload(path, params):
    if path.endswith('/reply'):
        return {'replies': copy.deepcopy(COMMENTS)}
    if path.endswith('/related'):
        return [copy.deepcopy(ITEM)]
    if path.endswith('/arc/search'):
        return {'list': {'vlist': [{**ITEM, 'created': int(time.time())}]}}
    if path.endswith('/view'):
        return copy.deepcopy(ITEM)
    return {'result': [copy.deepcopy(ITEM)], 'list': [copy.deepcopy(ITEM)]}


class FakeAccount:
    def __init__(self):
        self.ready, self.uid, self.generation = True, 'fake-a', 0
        self.get = AsyncMock(side_effect=payload)
        self.post = AsyncMock(side_effect=AssertionError('External writes forbidden'))

    def identity(self):
        return (self.uid if self.ready else 'anonymous', self.generation)


class HeldReads:
    """Observable transport, explicitly released by the test, never by a timer."""
    def __init__(self):
        self.started = asyncio.Queue()
        self.release = {}
        self.active = self.peak = 0
        self.finished = []

    async def get(self, path, params):
        name = params.get('name', params.get('keyword', path))
        event = self.release.setdefault(name, asyncio.Event())
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.put_nowait(name)
        try:
            await event.wait()
            if name == 'error':
                raise BiliUnavailable('mock read failed')
            return payload(path, params)
        finally:
            self.active -= 1
            self.finished.append(name)


class FakeSession:
    def __init__(self, handler):
        self.handler, self.closed = handler, False
        self.calls = []
        self.active = self.peak = 0

    def get(self, url, *, params, allow_redirects):
        if self.closed:
            raise AssertionError('HTTP after close')
        if allow_redirects is not False:
            raise AssertionError('Unexpected redirect policy')
        self.calls.append((url, dict(params)))
        return FakeResponse(self, url, params)

    async def close(self):
        self.closed = True


class FakeResponse:
    status = 200

    def __init__(self, session, url, params):
        self.session, self.url, self.params = session, url, params
        self.content = self

    async def __aenter__(self):
        self.session.active += 1
        self.session.peak = max(self.session.peak, self.session.active)
        try:
            self.data = await self.session.handler(self.url, self.params)
            return self
        except BaseException:
            self.session.active -= 1
            raise

    async def __aexit__(self, *args):
        self.session.active -= 1

    async def read(self, limit):
        return b'public visitor page'

    async def iter_chunked(self, size):
        yield json.dumps({'code': 0, 'data': self.data}).encode()


class ReadCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients, self.tasks, self.loop_errors = [], [], []
        self.loop = asyncio.get_running_loop()
        self.previous_handler = self.loop.get_exception_handler()
        self.loop.set_exception_handler(lambda loop, context: self.loop_errors.append(context))
        self.network = patch.object(module.aiohttp, 'ClientSession', side_effect=AssertionError('Real HTTP forbidden'))
        self.priority = patch.object(module, '_caller_priority', side_effect=PRIORITY.get)
        self.network.start()
        self.priority.start()

    async def asyncTearDown(self):
        try:
            for client in self.clients:
                await client.close()
            for task in self.tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await asyncio.sleep(0)
            for client in self.clients:
                self.assertEqual(client.slots.active, 0)
                self.assertEqual(client.slots.waiters, [])
                self.assertEqual(client._flights, {})
                self.assertFalse(client._read_tasks)
                self.assertFalse(client._http_tasks)
                if client.account:
                    client.account.post.assert_not_awaited()
            self.assertEqual(self.loop_errors, [])
        finally:
            self.priority.stop()
            self.network.stop()
            self.loop.set_exception_handler(self.previous_handler)

    def client(self, account=None, enabled=None):
        client = BiliClient(account, lambda: enabled[0] if enabled is not None else account is not None)
        self.clients.append(client)
        return client

    def spawn(self, coroutine, priority=0):
        token = PRIORITY.set(priority)
        try:
            task = asyncio.create_task(coroutine)
            self.tasks.append(task)
            return task
        finally:
            PRIORITY.reset(token)

    async def until(self, condition):
        # Yield to actual task transitions, not a guessed wall-clock sleep.
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(0)

    async def next_started(self, reads):
        return await asyncio.wait_for(reads.started.get(), 3)

    async def occupy(self, client, reads):
        tasks = [self.spawn(client.get('/x/web-interface/view', {'name': name}), 1)
                 for name in ('hold-a', 'hold-b')]
        self.assertEqual([await self.next_started(reads), await self.next_started(reads)], ['hold-a', 'hold-b'])
        return tasks

    def scan_key(self, client, keyword='test'):
        return ('test', client._read_identity(), keyword)


class SharedScans(ReadCase):
    async def test_cached_normal_and_chishi_do_not_wait_for_another_scan(self):
        for mode in ('normal', 'chishi'):
            with self.subTest(mode=mode):
                client = self.client()
                client.get = AsyncMock(side_effect=payload)
                expected = await client.discover('warm', mode=mode, mygo=False)
                entered, release = asyncio.Event(), asyncio.Event()

                async def blocked(path, params):
                    entered.set()
                    await release.wait()
                    return payload(path, params)

                client.get = AsyncMock(side_effect=blocked)
                cold = self.spawn(client.discover('cold', mode=mode, mygo=False))
                await asyncio.wait_for(entered.wait(), 3)
                warm = await asyncio.wait_for(client.discover('warm', mode=mode, mygo=False), 3)
                self.assertEqual(warm, expected)
                self.assertFalse(cold.done())
                release.set()
                await cold

    async def test_identical_scans_share_but_exclusion_and_mutations_do_not(self):
        for mode in ('normal', 'chishi'):
            with self.subTest(mode=mode):
                client = self.client()
                entered, release = asyncio.Event(), asyncio.Event()

                async def read(path, params):
                    entered.set()
                    await release.wait()
                    return payload(path, params)

                client.get = AsyncMock(side_effect=read)
                first = self.spawn(client.discover('same', mode=mode, mygo=False))
                second = self.spawn(client.discover('same', [ITEM['bvid']], mode=mode, mygo=False))
                await asyncio.wait_for(entered.wait(), 3)
                await self.until(lambda: any(len(f.waiters) == 2 for f in client._flights.values()))
                release.set()
                a, b = await asyncio.gather(first, second)
                self.assertTrue(a['items'])
                self.assertEqual(b['items'], [])
                count = client.get.await_count
                # One normal scan has exactly two GETs. One chishi scan has
                # 4 keyword lanes, 3 fresh lanes, 1 related and 1 comment GET.
                self.assertEqual(count, 2 if mode == 'normal' else 9)
                a['items'][0]['comments'].append('local mutation')
                a['warnings'].append('local warning')
                again = await client.discover('same', mode=mode, mygo=False)
                self.assertNotIn('local mutation', again['items'][0]['comments'])
                self.assertNotIn('local warning', again['warnings'])
                self.assertEqual(client.get.await_count, count)

    async def test_different_queries_make_progress_without_global_scan_lock(self):
        client = self.client()
        entered, release = asyncio.Queue(), asyncio.Event()

        async def read(path, params):
            if path.endswith('/type'):
                entered.put_nowait(params['keyword'])
                await release.wait()
            return payload(path, params)

        client.get = AsyncMock(side_effect=read)
        a = self.spawn(client.discover('a'))
        b = self.spawn(client.discover('b'))
        self.assertEqual({await asyncio.wait_for(entered.get(), 3), await asyncio.wait_for(entered.get(), 3)}, {'a', 'b'})
        release.set()
        await asyncio.gather(a, b)

    async def test_cancel_one_waiter_preserves_shared_scan_in_both_modes(self):
        for mode in ('normal', 'chishi'):
            with self.subTest(mode=mode):
                client = self.client()
                entered, release = asyncio.Event(), asyncio.Event()
                cancelled = []

                async def read(path, params):
                    entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelled.append(path)
                        raise
                    return payload(path, params)

                client.get = AsyncMock(side_effect=read)
                a = self.spawn(client.discover('same', mode=mode, mygo=False))
                b = self.spawn(client.discover('same', mode=mode, mygo=False))
                await asyncio.wait_for(entered.wait(), 3)
                await self.until(lambda: any(len(f.waiters) == 2 for f in client._flights.values()))
                a.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await a
                self.assertFalse(b.done())
                self.assertEqual(cancelled, [])
                release.set()
                self.assertTrue((await b)['items'])

    async def test_last_waiter_cancel_drains_all_scan_children(self):
        for mode in ('normal', 'chishi'):
            with self.subTest(mode=mode):
                client = self.client()
                entered = asyncio.Event()
                active = set()

                async def read(path, params):
                    task = asyncio.current_task()
                    active.add(task)
                    entered.set()
                    try:
                        await asyncio.Future()
                    finally:
                        active.remove(task)

                client.get = AsyncMock(side_effect=read)
                a = self.spawn(client.discover('same', mode=mode, mygo=False))
                await asyncio.wait_for(entered.wait(), 3)
                a.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await a
                self.assertFalse(active)
                self.assertFalse(client._flights)
                self.assertFalse(client.cache)
                client.get = AsyncMock(side_effect=payload)
                self.assertTrue((await client.discover('same', mode=mode, mygo=False))['items'])

    async def test_old_cancel_cleanup_does_not_remove_replacement(self):
        client = self.client()
        key = self.scan_key(client)
        entered, cleaning, finish_cleanup, new_release = (asyncio.Event() for _ in range(4))

        async def old():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await finish_cleanup.wait()

        async def new():
            await new_release.wait()
            return (time.time(), [], [])

        a = self.spawn(client._cached_scan(key, old))
        await asyncio.wait_for(entered.wait(), 3)
        a.cancel()
        await asyncio.wait_for(cleaning.wait(), 3)
        b = self.spawn(client._cached_scan(key, new))
        await self.until(lambda: ('scan', key) in client._flights)
        replacement = client._flights[('scan', key)]
        finish_cleanup.set()
        with self.assertRaises(asyncio.CancelledError):
            await a
        self.assertIs(client._flights[('scan', key)], replacement)
        new_release.set()
        await b

    async def test_failed_shared_read_not_cached_and_next_attempt_succeeds(self):
        client = self.client()
        key = self.scan_key(client)
        fail = AsyncMock(side_effect=BiliUnavailable('mock failure'))
        with self.assertRaises(BiliUnavailable):
            await client._cached_scan(key, fail)
        self.assertNotIn(key, client.cache)
        success = AsyncMock(return_value=(time.time(), [], []))
        await client._cached_scan(key, success)
        success.assert_awaited_once()

    async def test_malformed_normal_comment_response_drains_sibling_reads(self):
        client = self.client()
        sibling_started, sibling_finished = asyncio.Event(), asyncio.Event()

        async def read(path, params):
            if path.endswith('/type'):
                return {'result': [ITEM, {**ITEM, 'bvid': 'BV1xx411c7mE', 'aid': 124}]}
            if params['oid'] == 123:
                await sibling_started.wait()
                return []  # Malformed upstream shape; no silent success/cache.
            sibling_started.set()
            try:
                await asyncio.Future()
            finally:
                sibling_finished.set()

        client.get = AsyncMock(side_effect=read)
        with self.assertRaises(AttributeError):
            await asyncio.wait_for(client.discover('same'), 3)
        self.assertTrue(sibling_finished.is_set())
        self.assertFalse(client.cache)

    async def test_cache_ttl_capacity_and_query_options_unchanged(self):
        client = self.client()
        build = AsyncMock(side_effect=lambda: (time.time(), [], []))
        key = self.scan_key(client)
        with patch.object(module.time, 'time', return_value=1000):
            await client._cached_scan(key, build)
        with patch.object(module.time, 'time', return_value=1300):
            await client._cached_scan(key, build)
            self.assertEqual(build.await_count, 1)
        with patch.object(module.time, 'time', return_value=1301):
            await client._cached_scan(key, build)
            self.assertEqual(build.await_count, 2)
        for i in range(40):
            await client._cached_scan(self.scan_key(client, str(i)), build)
        self.assertEqual(len(client.cache), 32)
        client.get = AsyncMock(side_effect=payload)
        for options in ({'mode': 'normal'}, {'mode': 'chishi', 'mygo': False},
                        {'mode': 'chishi', 'mygo': True}, {'mode': 'chishi', 'mygo': False, 'up_list': '123'}):
            before = client.get.await_count
            await client.discover('same', **options)
            self.assertGreater(client.get.await_count, before)

    async def test_close_rejects_even_cached_queries(self):
        client = self.client()
        client.get = AsyncMock(side_effect=payload)
        await client.discover('same')
        await client.close()
        with self.assertRaises(BiliUnavailable):
            await client.discover('same')


class HttpScheduling(ReadCase):
    def held(self):
        account, reads = FakeAccount(), HeldReads()
        account.get = AsyncMock(side_effect=reads.get)
        return self.client(account), reads

    async def test_http_limit_manual_priority_and_fifo(self):
        client, reads = self.held()
        occupied = await self.occupy(client, reads)
        background = self.spawn(client.get('/x/web-interface/view', {'name': 'background'}), 1)
        foreground = self.spawn(client.get('/x/web-interface/view', {'name': 'foreground'}), 0)
        next_foreground = self.spawn(client.get('/x/web-interface/view', {'name': 'next'}), 0)
        await self.until(lambda: len(client.slots.waiters) == 3)
        self.assertEqual(reads.active, 2)
        reads.release['hold-a'].set()
        self.assertEqual(await self.next_started(reads), 'foreground')
        reads.release['hold-b'].set()
        self.assertEqual(await self.next_started(reads), 'next')
        reads.release['foreground'].set()
        self.assertEqual(await self.next_started(reads), 'background')
        reads.release['next'].set()
        reads.release['background'].set()
        await asyncio.gather(*occupied, background, foreground, next_foreground)
        self.assertEqual(reads.peak, 2)

    async def test_joining_and_leaving_manual_waiter_changes_shared_priority(self):
        for cancel_manual in (False, True):
            with self.subTest(cancel_manual=cancel_manual):
                client, reads = self.held()
                occupied = await self.occupy(client, reads)
                older = self.spawn(client.get('/x/web-interface/view', {'name': 'older-background'}), 1)
                background = self.spawn(client.discover('shared'), 1)
                await self.until(lambda: len(client.slots.waiters) == 2)
                manual = self.spawn(client.discover('shared'), 0)
                await self.until(lambda: any(len(f.waiters) == 2 for f in client._flights.values()))
                if cancel_manual:
                    manual.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await manual
                reads.release['hold-a'].set()
                expected = 'older-background' if cancel_manual else 'shared'
                self.assertEqual(await self.next_started(reads), expected)
                self.assertEqual(client.account.get.await_count, 3)
                # Teardown cancels held reads; no artificial release ordering is
                # needed for the remaining irrelevant comment phase.
                await client.close()
                await asyncio.gather(*occupied, older, background, manual, return_exceptions=True)

    async def test_queued_cancellation_does_not_consume_a_slot(self):
        client, reads = self.held()
        await self.occupy(client, reads)
        abandoned = self.spawn(client.get('/x/web-interface/view', {'name': 'abandoned'}))
        await self.until(lambda: len(client.slots.waiters) == 1)
        abandoned.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await abandoned
        self.assertEqual(client.slots.waiters, [])
        self.assertEqual(reads.active, 2)
        self.assertNotIn('abandoned', reads.release)

    async def test_cancel_after_grant_before_resume_releases_exactly_once(self):
        slots = _ReadSlots(1)
        holder = slots.hold(lambda: 1)
        await holder.__aenter__()
        entered = []

        async def waiter():
            async with slots.hold(lambda: 0):
                entered.append(True)

        task = self.spawn(waiter())
        await self.until(lambda: len(slots.waiters) == 1)
        await holder.__aexit__(None, None, None)  # Grants without yielding.
        self.assertEqual(slots.active, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(entered, [])
        self.assertEqual(slots.active, 0)
        async with slots.hold(lambda: 0):
            self.assertEqual(slots.active, 1)
        self.assertEqual(slots.active, 0)

    async def test_http_error_releases_slot_and_does_not_retry(self):
        client, reads = self.held()
        error = self.spawn(client.get('/x/web-interface/view', {'name': 'error'}))
        self.assertEqual(await self.next_started(reads), 'error')
        reads.release['error'].set()
        with self.assertRaises(BiliUnavailable):
            await error
        self.assertEqual(client.slots.active, 0)
        self.assertEqual(client.account.get.await_count, 1)
        client.account.get = AsyncMock(side_effect=payload)
        self.assertEqual((await client.detail(ITEM['bvid']))['bvid'], ITEM['bvid'])

    async def test_close_drains_running_and_waiting_http(self):
        client, reads = self.held()
        await self.occupy(client, reads)
        self.spawn(client.discover('waiting'))
        await self.until(lambda: len(client.slots.waiters) == 1)
        await client.close()
        self.assertEqual(reads.active, 0)
        self.assertEqual(client.slots.active, 0)
        self.assertEqual(client.slots.waiters, [])
        with self.assertRaises(BiliUnavailable):
            await client.get('/x/web-interface/view', {})

    async def test_uses_existing_task_marker_without_changing_it(self):
        self.priority.stop()
        self.assertEqual(module._caller_priority(), 0)
        with model_work('scheduled'):
            self.assertEqual(module._caller_priority(), 1)
            with model_work('interactive'):
                self.assertEqual(module._caller_priority(), 0)
            self.assertEqual(module._caller_priority(), 1)
        self.assertEqual(module._caller_priority(), 0)
        self.priority.start()


class IdentityIsolation(ReadCase):
    async def test_account_changes_never_reuse_cached_or_inflight_scans(self):
        for change in ('account', 'relogin', 'logout', 'toggle', 'replace', 'anonymous-generation'):
            with self.subTest(change=change):
                account, enabled = FakeAccount(), [change != 'anonymous-generation']
                client = self.client(account, enabled)
                client.get = AsyncMock(side_effect=payload)
                await client.discover('warm')
                old_key = next(iter(client.cache))
                entered, release = asyncio.Event(), asyncio.Event()

                async def old_read(path, params):
                    if params.get('keyword') == 'cold':
                        entered.set()
                        await release.wait()
                    return payload(path, params)

                client.get = AsyncMock(side_effect=old_read)
                old = self.spawn(client.discover('cold'))
                await asyncio.wait_for(entered.wait(), 3)
                if change == 'toggle':
                    enabled[0] = False
                elif change == 'replace':
                    client.account = FakeAccount()
                else:
                    account.generation += 1
                    if change == 'account':
                        account.uid = 'fake-b'
                    elif change == 'logout':
                        account.ready = False
                client.get = AsyncMock(side_effect=payload)
                fresh = await client.discover('cold')
                self.assertTrue(fresh['items'])
                await client.discover('warm')
                self.assertEqual(client.get.await_count, 4)
                self.assertNotEqual(next(k for k in client.cache if k[2] == 'cold')[1], old_key[1])
                release.set()
                with self.assertRaises(BiliUnavailable):
                    await old

    async def test_queued_old_account_get_cannot_use_new_account(self):
        account, reads = FakeAccount(), HeldReads()
        account.get = AsyncMock(side_effect=reads.get)
        client = self.client(account)
        occupied = await self.occupy(client, reads)
        waiting = self.spawn(client.get('/x/web-interface/view', {'name': 'old-waiter'}))
        await self.until(lambda: len(client.slots.waiters) == 1)
        account.generation += 1
        reads.release['hold-a'].set()
        with self.assertRaises(BiliUnavailable):
            await occupied[0]
        with self.assertRaises(BiliUnavailable):
            await waiting
        self.assertNotIn('old-waiter', reads.release)
        self.assertEqual(account.get.await_count, 2)

    async def test_identity_checked_again_after_shared_task_completes(self):
        account = FakeAccount()
        client = self.client(account)
        entered, release = asyncio.Event(), asyncio.Event()

        async def build():
            entered.set()
            await release.wait()
            return (time.time(), [], [])

        task = self.spawn(client._cached_scan(self.scan_key(client), build))
        await asyncio.wait_for(entered.wait(), 3)
        flight = next(iter(client._flights.values()))
        flight.task.add_done_callback(lambda _: setattr(account, 'generation', account.generation + 1))
        release.set()
        with self.assertRaises(BiliUnavailable):
            await task


class AnonymousSetup(ReadCase):
    def anonymous(self, handler, account=None):
        client = self.client(account, [False])
        client.session = FakeSession(handler)
        return client

    async def test_initialization_is_shared_and_http_peak_is_two(self):
        visitor_entered, visitor_release, api_release = (asyncio.Event() for _ in range(3))
        api_started = asyncio.Queue()

        async def http(url, params):
            if url.startswith('https://search.'):
                visitor_entered.set()
                await visitor_release.wait()
                return {}
            api_started.put_nowait(params['keyword'])
            await api_release.wait()
            return {'result': []}

        client = self.anonymous(http)
        tasks = [self.spawn(client.get('/x/web-interface/search/type', {'keyword': str(i)})) for i in range(6)]
        await asyncio.wait_for(visitor_entered.wait(), 3)
        await self.until(lambda: len(next(iter(client._flights.values())).waiters) == 6)
        self.assertEqual(client.session.active, 1)
        self.assertEqual(len(client.session.calls), 1)
        visitor_release.set()
        await asyncio.wait_for(api_started.get(), 3)
        await asyncio.wait_for(api_started.get(), 3)
        self.assertEqual(client.session.active, 2)
        api_release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(client.session.peak, 2)
        self.assertEqual(sum(url.startswith('https://search.') for url, _ in client.session.calls), 1)

    async def test_manual_waiter_promotes_shared_visitor_initialization(self):
        reads = HeldReads()

        async def http(url, params):
            label = 'visitor' if url.startswith('https://search.') else params.get('name', 'api')
            return await reads.get('/x/web-interface/view', {'name': label})

        client = self.anonymous(http)
        await self.occupy(client, reads)
        self.spawn(client.get('/x/web-interface/view', {'name': 'older-background'}), 1)
        self.spawn(client.get('/x/web-interface/search/type', {'keyword': 'background'}), 1)
        await self.until(lambda: len(client.slots.waiters) == 2)
        self.spawn(client.get('/x/web-interface/search/type', {'keyword': 'manual'}), 0)
        await self.until(lambda: any(len(f.waiters) == 2 for f in client._flights.values()))
        reads.release['hold-a'].set()
        self.assertEqual(await self.next_started(reads), 'visitor')
        self.assertEqual(client.session.peak, 2)

    async def test_one_cancel_keeps_visitor_for_other_reader(self):
        entered, release = asyncio.Event(), asyncio.Event()
        cancelled = []

        async def http(url, params):
            if url.startswith('https://search.'):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise
            return {}

        client = self.anonymous(http)
        a = self.spawn(client.get('/x/web-interface/search/type', {'keyword': 'a'}))
        b = self.spawn(client.get('/x/web-interface/search/type', {'keyword': 'b'}))
        await asyncio.wait_for(entered.wait(), 3)
        await self.until(lambda: len(next(iter(client._flights.values())).waiters) == 2)
        a.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await a
        self.assertEqual(cancelled, [])
        release.set()
        await b
        self.assertTrue(client.visitor_ready)

    async def test_failed_or_cancelled_initializer_can_be_retried(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                entered = asyncio.Event()

                async def http(url, params):
                    entered.set()
                    if cancel:
                        await asyncio.Future()
                    raise TimeoutError('mock timeout')

                client = self.anonymous(http)
                old = self.spawn(client.get('/x/web-interface/search/type', {'keyword': 'old'}))
                await asyncio.wait_for(entered.wait(), 3)
                if cancel:
                    old.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else BiliUnavailable):
                    await old
                self.assertFalse(client.visitor_ready)
                self.assertEqual(client.session.active, 0)
                client.session.handler = AsyncMock(return_value={})
                await client.get('/x/web-interface/search/type', {'keyword': 'new'})
                self.assertTrue(client.visitor_ready)
                self.assertEqual(client.session.handler.await_count, 2)

    async def test_account_transition_during_initializer_rejects_old_result(self):
        account = FakeAccount()
        entered, release = asyncio.Event(), asyncio.Event()

        async def http(url, params):
            entered.set()
            await release.wait()
            return {}

        client = self.anonymous(http, account)
        old = self.spawn(client.get('/x/web-interface/search/type', {'keyword': 'old'}))
        await asyncio.wait_for(entered.wait(), 3)
        account.generation += 1
        release.set()
        with self.assertRaises(BiliUnavailable):
            await old
        self.assertFalse(client.visitor_ready)
        self.assertEqual(len(client.session.calls), 1)
        client.session.handler = AsyncMock(return_value={})
        await client.get('/x/web-interface/search/type', {'keyword': 'new'})
        self.assertTrue(client.visitor_ready)

    async def test_close_during_shared_initialization_drains_every_waiter(self):
        entered = asyncio.Event()

        async def http(url, params):
            entered.set()
            await asyncio.Future()

        client = self.anonymous(http)
        tasks = [self.spawn(client.get('/x/web-interface/search/type', {'keyword': str(i)})) for i in range(4)]
        await asyncio.wait_for(entered.wait(), 3)
        await client.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(client.session.active, 0)
        self.assertTrue(client.session.closed)
        self.assertFalse(client.visitor_ready)


if __name__ == '__main__':
    unittest.main()
