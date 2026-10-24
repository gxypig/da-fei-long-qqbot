"""Real Runner payloads and prediction admission; all model/transport I/O is fake."""
import ast
import asyncio
import copy
import importlib.util
import inspect
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'AstrBot'))
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import (
    TAIL_GUARD_MARKER, WorkRuntime, _has_current_media, _strip_tail_guard, model_work,
)
from test_work_scheduler import Clock, Event, provider, until


def provider_signature(self, prompt=None, contexts=None, system_prompt=None, **kwargs):
    pass


class RunnerPayloadTests(unittest.TestCase):
    def clean(self, **kwargs):
        return _strip_tail_guard(inspect.signature(provider_signature), object(), (), kwargs)

    def test_runner_context_marker_is_stripped_without_changing_history_or_user_text(self):
        contexts = [
            {'role': 'system', 'content': 'rules\n' + TAIL_GUARD_MARKER, 'keep': {'metadata': 1}},
            {'role': 'user', 'content': 'literal ' + TAIL_GUARD_MARKER},
        ]
        before = copy.deepcopy(contexts)
        args, kwargs, values = self.clean(contexts=contexts, request_max_retries=5)
        self.assertIsNotNone(values)
        self.assertEqual(values['contexts'][0]['content'], 'rules\n')
        self.assertIs(values['contexts'][1], contexts[1])
        self.assertEqual(contexts, before)
        bound = inspect.signature(provider_signature).bind(object(), *args, **kwargs)
        self.assertEqual(bound.arguments['contexts'], values['contexts'])
        self.assertEqual(values['request_max_retries'], 5)

    def test_typed_and_structured_system_parts_keep_metadata_and_container_shape(self):
        part = SimpleNamespace(type='text', text='trusted ' + TAIL_GUARD_MARKER, _no_save=True)
        thought = SimpleNamespace(type='think', think=TAIL_GUARD_MARKER)
        message = SimpleNamespace(role='system', content=(part, thought), checkpoint='retain')
        contexts = (message, {'role': 'developer', 'content': [
            {'type': 'input_text', 'text': 'rules' + TAIL_GUARD_MARKER, 'keep': 3} ]})
        _, _, values = self.clean(contexts=contexts, system_prompt='prefix' + TAIL_GUARD_MARKER)
        result = values['contexts']
        self.assertIsInstance(result, tuple)
        self.assertIsInstance(result[0].content, tuple)
        self.assertTrue(result[0].content[0]._no_save)
        self.assertEqual(result[0].content[0].text, 'trusted ')
        self.assertIs(result[0].content[1], thought)
        self.assertEqual(result[0].checkpoint, 'retain')
        self.assertEqual(result[1]['content'], [{'type': 'input_text', 'text': 'rules', 'keep': 3}])
        self.assertEqual(values['system_prompt'], 'prefix')
        self.assertIn(TAIL_GUARD_MARKER, part.text)

    def test_untrusted_markers_do_not_enable_hedging_or_change_request_identity(self):
        for role in ('user', 'assistant', 'tool'):
            contexts = [{'role': role, 'content': TAIL_GUARD_MARKER}]
            _, kwargs, values = self.clean(contexts=contexts)
            self.assertIsNone(values)
            self.assertIs(kwargs['contexts'], contexts)

    def test_unknown_context_iterator_is_not_consumed(self):
        def contexts():
            raise AssertionError('Do not consume unsupported context iterators')
            yield
        source = contexts()
        _, kwargs, values = self.clean(contexts=source)
        self.assertIsNone(values)
        self.assertIs(kwargs['contexts'], source)

    def test_current_media_is_detected_in_runner_shape_but_old_media_is_retained(self):
        image = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,offline'}}
        current = [{'role': 'user', 'content': [image]}]
        self.assertTrue(_has_current_media({'contexts': current}))
        self.assertTrue(_has_current_media({'extra_user_content_parts': [image]}))
        self.assertTrue(_has_current_media({'contexts': iter(current)}))
        old = current + [{'role': 'assistant', 'content': 'earlier answer'},
                         {'role': 'user', 'content': [{'type': 'text', 'text': 'new chat'}]}]
        self.assertFalse(_has_current_media({'contexts': old}))
        self.assertEqual(old[0]['content'], [image])
        self.assertFalse(_has_current_media({'contexts': [{'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': [{'type': 'think', 'think': 'reasoning'}]}]}))


class PredictionAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = WorkRuntime({'latency_optimization_enabled': True}.get, lambda *r: None, clock=Clock())
        self.addAsyncCleanup(self.runtime.close)
        self.tasks = []
        self.addAsyncCleanup(self.cleanup_tasks)

    async def cleanup_tasks(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def prediction(self, event, operation, p=None):
        async def run():
            with model_work('auxiliary'), self.runtime.prediction_work(event):
                return await self.runtime.scheduler.run(p or provider(), operation)
        task = asyncio.create_task(run())
        self.tasks.append(task)
        return task

    async def test_same_turn_primary_enters_first_then_prediction_runs_in_parallel(self):
        entered, release = [], asyncio.Event()
        async def operation(label):
            entered.append(label)
            await release.wait()
            return label
        with self.runtime.reply_work(Event()) as _:
            event = self.runtime._scope().trace.event
            prediction = self.prediction(event, lambda: operation('prediction'))
            await until(lambda: self.runtime.status()['predictions_awaiting_primary'] == 1)
            self.assertEqual(entered, [])
            self.assertEqual(self.runtime.status()['running'], 0)
            primary = asyncio.create_task(self.runtime.scheduler.run(provider(), lambda: operation('primary')))
            self.tasks.append(primary)
            await until(lambda: len(entered) == 2)
            self.assertEqual(entered, ['primary', 'prediction'])
            self.assertEqual(self.runtime.status()['running'], 2)
            release.set()
            self.assertEqual(await asyncio.gather(primary, prediction), ['primary', 'prediction'])

    async def test_existing_foreground_does_not_let_new_prediction_steal_last_slot(self):
        entered, release = [], asyncio.Event()
        async def operation(label):
            entered.append(label)
            await release.wait()
        blocker = asyncio.create_task(self.runtime.scheduler.run(provider(), lambda: operation('other')))
        self.tasks.append(blocker)
        await until(lambda: entered == ['other'])
        event = Event()
        with self.runtime.reply_work(event):
            prediction = self.prediction(event, lambda: operation('prediction'))
            await until(lambda: self.runtime.status()['predictions_awaiting_primary'] == 1)
            primary = asyncio.create_task(self.runtime.scheduler.run(provider(), lambda: operation('primary')))
            self.tasks.append(primary)
            await until(lambda: 'primary' in entered)
            self.assertEqual(entered, ['other', 'primary'])
            self.assertEqual(self.runtime.status()['running'], 2)
            release.set()
            await asyncio.gather(blocker, primary, prediction)
            self.assertEqual(entered, ['other', 'primary', 'prediction'])

    async def test_cancelled_prediction_waiter_never_runs_or_leaks_capacity(self):
        operation = Mock(side_effect=AssertionError('Cancelled prediction must not run'))
        event = Event()
        with self.runtime.reply_work(event):
            prediction = self.prediction(event, operation)
            await until(lambda: self.runtime.status()['waiting'] == 1)
            prediction.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await prediction
            self.assertEqual(self.runtime.status()['waiting'], 0)
            self.assertEqual(await self.runtime.scheduler.run(provider(), lambda: asyncio.sleep(0, result=7)), 7)
        operation.assert_not_called()

    async def test_turn_ending_without_primary_cancels_its_prediction_only(self):
        operation = Mock(side_effect=AssertionError('Orphan prediction must not run'))
        event = Event()
        with self.runtime.reply_work(event):
            prediction = self.prediction(event, operation)
            await until(lambda: self.runtime.status()['waiting'] == 1)
        with self.assertRaises(asyncio.CancelledError):
            await prediction
        self.assertEqual(self.runtime.status()['waiting'], 0)
        self.assertEqual(self.runtime.status()['running'], 0)
        operation.assert_not_called()

    async def test_stream_primary_opens_gate_and_cancellation_releases_slot(self):
        entered, release = [], asyncio.Event()
        async def stream():
            entered.append('stream')
            try:
                yield 'first'
                await release.wait()
            finally:
                entered.append('closed')
        event = Event()
        with self.runtime.reply_work(event):
            prediction = self.prediction(event, lambda: asyncio.sleep(0, result='prediction'))
            await until(lambda: self.runtime.status()['waiting'] == 1)
            iterator = self.runtime.scheduler.stream(provider(), stream)
            self.assertEqual(await anext(iterator), 'first')
            self.assertEqual(await prediction, 'prediction')
            await iterator.aclose()
        self.assertEqual(entered, ['stream', 'closed'])
        self.assertEqual(self.runtime.status()['running'], 0)

    async def test_one_slot_remains_serial_and_other_provider_waits_for_primary_admission(self):
        self.runtime.scheduler.cfg = {'model_concurrency': 1}
        entered, release = [], asyncio.Event()
        async def primary():
            entered.append('primary')
            await release.wait()
        async def predicted(label):
            entered.append(label)
        event = Event()
        with self.runtime.reply_work(event):
            prediction = self.prediction(event, lambda: predicted('same-source'))
            other = self.prediction(event, lambda: predicted('other-source'), provider('other'))
            await until(lambda: self.runtime.status()['waiting'] == 2)
            task = asyncio.create_task(self.runtime.scheduler.run(provider(), primary))
            self.tasks.append(task)
            await until(lambda: 'other-source' in entered)
            self.assertEqual(entered, ['primary', 'other-source'])
            release.set()
            await asyncio.gather(task, prediction, other)
        self.assertEqual(entered[-1], 'same-source')

    async def test_auxiliary_without_prediction_contract_retains_existing_behavior(self):
        event = Event()
        with self.runtime.reply_work(event), model_work('auxiliary'):
            value = await self.runtime.scheduler.run(provider(), lambda: asyncio.sleep(0, result=8))
        self.assertEqual(value, 8)
        detached = Event(message_id='preview')
        self.assertEqual(await self.prediction(detached, lambda: asyncio.sleep(0, result=9)), 9)

    async def test_cancelled_primary_waiter_closes_gate_without_starting_prediction(self):
        self.runtime.scheduler.cfg = {'model_concurrency': 1}
        block = asyncio.Event()
        blocker = asyncio.create_task(self.runtime.scheduler.run(provider(), block.wait))
        self.tasks.append(blocker)
        await until(lambda: self.runtime.status()['running'] == 1)
        operation = Mock(side_effect=AssertionError('Cancelled turn must not issue model I/O'))
        event = Event()
        with self.runtime.reply_work(event):
            prediction = self.prediction(event, operation)
            primary = asyncio.create_task(self.runtime.scheduler.run(provider(), operation))
            self.tasks.append(primary)
            await until(lambda: self.runtime.status()['waiting'] == 2)
            primary.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await primary
        with self.assertRaises(asyncio.CancelledError):
            await prediction
        self.assertEqual(self.runtime.status()['waiting'], 0)
        block.set()
        await blocker
        operation.assert_not_called()

    async def test_wait_for_child_retains_prediction_gate_and_close_wakes_it(self):
        operation = Mock(side_effect=AssertionError('Held prediction must not issue model I/O'))
        event = Event()
        async def predicted():
            with model_work('auxiliary'), self.runtime.prediction_work(event):
                return await asyncio.wait_for(self.runtime.scheduler.run(provider(), operation), 10)
        with self.runtime.reply_work(event):
            task = asyncio.create_task(predicted())
            self.tasks.append(task)
            await until(lambda: self.runtime.status()['predictions_awaiting_primary'] == 1)
            await self.runtime.close()
            from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import WorkClosed
            with self.assertRaises(WorkClosed):
                await task
        self.assertEqual(self.runtime.status()['waiting'], 0)
        operation.assert_not_called()

    async def test_final_stream_response_closes_provider_and_releases_before_consumer_resumes(self):
        closed = []
        final = SimpleNamespace(is_chunk=False, metadata={'usage': 42})
        async def factory():
            try:
                yield SimpleNamespace(is_chunk=True)
                yield final
                raise AssertionError('No speculative pull after terminal response')
            finally:
                closed.append(True)
        stream = self.runtime.scheduler.stream(provider(), factory, is_final=lambda x: not x.is_chunk)
        self.assertTrue((await anext(stream)).is_chunk)
        self.assertEqual(self.runtime.status()['running'], 1)
        self.assertIs(await anext(stream), final)
        self.assertEqual(closed, [True])
        self.assertEqual(self.runtime.status()['running'], 0)
        await stream.aclose()


class EmojiStatusTests(unittest.TestCase):
    def setUp(self):
        # Compile only this read-only method. Importing the whole plugin would
        # initialize AstrBot paths, which this isolated status test does not need.
        path = ROOT / 'AstrBot/data/plugins/astrbot_plugin_proactive_agent/reply_pipeline.py'
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'performance_status')
        ns = {'deepcopy': copy.deepcopy}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), ns)
        self.status = ns['performance_status']
        self.plugin = SimpleNamespace(_cfg={}.get, _latency_summary=lambda: {})

    def test_missing_or_inactive_hub_is_safe_empty_and_not_read(self):
        self.assertEqual(self.status(self.plugin)['emoji'], {})
        class Inactive:
            @property
            def _latency_emoji_stats(self):
                raise AssertionError('Do not inspect inactive plugin')
        star = SimpleNamespace(name='astrbot_plugin_smart_imagechat_hub', activated=False, star_cls=Inactive())
        self.plugin.context = SimpleNamespace(get_all_stars=lambda: [star])
        self.assertEqual(self.status(self.plugin)['emoji'], {})

    def test_active_stats_are_copied_and_only_counters_and_probability_are_read(self):
        stats = {'triggered': 3, 'sent_separate': 1}
        hub = SimpleNamespace(_latency_emoji_stats=stats,
                              _proactive_emoji_config=lambda: {'trigger_probability': .35})
        star = SimpleNamespace(root_dir_name='astrbot_plugin_smart_imagechat_hub', activated=True, star_cls=hub)
        self.plugin.context = SimpleNamespace(get_all_stars=lambda: [star])
        result = self.status(self.plugin)['emoji']
        self.assertEqual(result, {'trigger_probability': .35, 'stats': stats})
        result['stats']['triggered'] = 100
        self.assertEqual(stats['triggered'], 3)

    def test_unloaded_hub_instance_or_missing_stats_is_safe_empty(self):
        for hub in (None, SimpleNamespace(), SimpleNamespace(_latency_emoji_stats=None)):
            star = SimpleNamespace(name='astrbot_plugin_smart_imagechat_hub', activated=True, star_cls=hub)
            self.plugin.context = SimpleNamespace(get_all_stars=lambda: [star])
            self.assertEqual(self.status(self.plugin)['emoji'], {})


class ActualRunnerTests(unittest.TestCase):
    def test_real_tool_loop_runner_payload_hedge_tools_stream_and_abort(self):
        if importlib.util.find_spec('astrbot') is None:
            self.skipTest('AstrBot is not installed')
        with tempfile.TemporaryDirectory(prefix='qqbot-runner-regression-') as directory:
            completed = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--runner-child'],
                cwd=directory, capture_output=True, encoding='utf-8', errors='replace', timeout=50)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn('verified actual ToolLoopAgentRunner: 6 scenarios', completed.stdout)


async def _actual_runner_scenarios():
    # Imports are isolated in a temporary subprocess cwd, never the live runtime.
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.message import Message, TextPart
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.agent.tool_executor import BaseFunctionToolExecutor
    from astrbot.core.provider.entities import LLMResponse, ProviderRequest
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from mcp.types import CallToolResult, TextContent
    from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import install_work_runtime

    class Client:
        def with_options(self):
            return Client()

    class Executor(BaseFunctionToolExecutor):
        @classmethod
        async def execute(cls, tool, run_context, **kwargs):
            executed.append(tool.name)
            yield CallToolResult(content=[TextContent(type='text', text='synthetic tool result')])

    class Hooks(BaseAgentRunHooks):
        async def on_agent_done(self, context, response):
            done.append(response.completion_text)

    def response(text='final answer'):
        return LLMResponse(role='assistant', completion_text=text)

    calls, executed, done, timers, cancelled = [], [], [], [], []
    timer_gate, first_ready = asyncio.Event(), asyncio.Event()
    mode = ''
    async def chat(p, prompt=None, contexts=None, system_prompt=None, func_tool=None, **kwargs):
        index = len(calls)
        calls.append((p, contexts, system_prompt, func_tool, kwargs))
        assert prompt is None and system_prompt is None, 'Must exercise real Runner contexts API'
        assert TAIL_GUARD_MARKER not in str(contexts), 'Internal marker must not reach provider'
        assert p.provider_config['custom_extra_body'] == {'reasoning_effort': 'high'}
        assert kwargs['request_max_retries'] == 5
        if mode in {'hedge', 'tool', 'abort'} and index == 0:
            first_ready.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.append(index)
        if mode == 'abort':
            try:
                await asyncio.Future()
            finally:
                cancelled.append(index)
        if mode == 'tool' and index == 1:
            return LLMResponse(role='tool', completion_text='', tools_call_name=['synthetic_action'],
                tools_call_args=[{}], tools_call_ids=['synthetic-call'])
        return response()

    async def stream(p, contexts=None, **kwargs):
        calls.append((p, contexts, kwargs.get('system_prompt'), kwargs.get('func_tool'), kwargs))
        assert TAIL_GUARD_MARKER not in str(contexts)
        yield LLMResponse(role='assistant', completion_text='piece', is_chunk=True)
        yield response()

    async def timer(seconds):
        timers.append(seconds)
        await timer_gate.wait()

    provider_obj = object.__new__(ProviderOpenAIOfficial)
    provider_obj.provider_config = {'id': 'offline', 'provider_source_id': 'same-source', 'model': 'unchanged-model',
        'type': 'openai_chat_completion', 'max_context_tokens': 0,
        'custom_extra_body': {'reasoning_effort': 'high'}}
    provider_obj.model_name = 'unchanged-model'
    provider_obj.client, provider_obj.api_keys = Client(), ['offline-only']
    cfg = {'latency_optimization_enabled': True, 'model_concurrency': 2}
    plugin = SimpleNamespace(_cfg=cfg.get, _audit=lambda *r: None, _platform=lambda: None)
    tools = ToolSet(tools=[FunctionTool(name='synthetic_action', description='offline side-effect counter',
                                      parameters={'type': 'object', 'properties': {}})])

    with patch.object(ProviderOpenAIOfficial, 'text_chat', chat), \
         patch.object(ProviderOpenAIOfficial, 'text_chat_stream', stream), \
         patch('socket.socket.connect', side_effect=AssertionError('Offline tests must not connect')), \
         patch('socket.socket.connect_ex', side_effect=AssertionError('Offline tests must not connect')):
        restore = install_work_runtime(plugin)
        runtime = plugin._work
        runtime._hedge_sleep = timer
        try:
            for mode in ('fast', 'hedge', 'tool', 'media', 'stream', 'abort'):
                calls.clear(); executed.clear(); done.clear(); timers.clear(); cancelled.clear()
                timer_gate.clear(); first_ready.clear()
                before_hedges = runtime.status()['hedge']['hedged']
                event = Event(message_id=mode)
                contexts = [{'role': 'user', 'content': 'earlier context'},
                            {'role': 'assistant', 'content': 'earlier answer'}]
                prompt = 'ordinary current chat'
                if mode == 'media':
                    contexts.append({'role': 'user', 'content': [{'type': 'image_url',
                        'image_url': {'url': 'data:image/png;base64,offline'}}]})
                    prompt = None
                req = ProviderRequest(prompt=prompt, contexts=contexts, system_prompt='fixed rules\n' + TAIL_GUARD_MARKER,
                                      func_tool=tools, model='unchanged-model')
                saved_contexts = copy.deepcopy(req.contexts)
                runner = ToolLoopAgentRunner()
                context = ContextWrapper(context=SimpleNamespace(event=event))
                await runner.reset(provider_obj, req, context, Executor(), Hooks(),
                    streaming=mode == 'stream', enforce_max_turns=-1, request_max_retries=5)
                # Verify typed content metadata survives the copy used for requests.
                system_message = context.messages[0]
                assert isinstance(system_message, Message) and system_message.role == 'system'
                system_message.content = [TextPart(text=system_message.content).mark_as_temp()]
                async def run():
                    with runtime.reply_work(event):
                        for _ in range(3):
                            async for item in runner.step():
                                pass
                            if runner.done():
                                break
                    assert runner.done()
                task = asyncio.create_task(run())
                try:
                    if mode in {'hedge', 'tool', 'abort'}:
                        await until(first_ready.is_set)
                        await until(lambda: bool(timers))
                        timer_gate.set()
                        if mode == 'abort':
                            await until(lambda: len(calls) == 2)
                            runner.request_stop()
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                assert req.contexts == saved_contexts
                assert system_message.content[0]._no_save
                assert TAIL_GUARD_MARKER in system_message.content[0].text, 'History must be immutable'
                assert runtime.status()['running'] == runtime.status()['waiting'] == 0
                assert not runtime._hedge_tasks
                if mode in {'hedge', 'tool', 'abort'}:
                    assert runtime.status()['hedge']['hedged'] == before_hedges + 1
                    assert 0 in cancelled
                else:
                    assert runtime.status()['hedge']['hedged'] == before_hedges
                    if mode in {'media', 'stream'}:
                        assert not timers
                if mode == 'tool':
                    assert executed == ['synthetic_action'], executed
                    assert len(calls) == 3, 'Two raced first calls, exactly one continuation'
                    assert calls[-1][3] is tools, 'Do not shrink the available tool set'
                elif mode != 'abort':
                    assert executed == []
                if mode != 'abort':
                    assert done == ['final answer'], done
                print('runner scenario passed:', mode)
        finally:
            restore()
            await runtime.close()
    print('verified actual ToolLoopAgentRunner: 6 scenarios')


if __name__ == '__main__':
    if '--runner-child' in sys.argv:
        asyncio.run(_actual_runner_scenarios())
    else:
        unittest.main()
