"""Privacy/measurement regressions with fake I/O and a manual clock.

These are synthetic duration assertions, NOT measurements of the deployed
provider. The wrappers and scheduler under test are the production code.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from test_work_scheduler import Clock, Event, FakeResponse, fake_dependencies, until
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import (
    TAIL_GUARD_MARKER, WorkRuntime, install_work_runtime,
)


class RequestMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock, self.rows, self.calls, self.tasks = Clock(), [], [], []
        self.cfg = {'latency_optimization_enabled': True, 'reply_hedge_after_seconds': 15}
        test = self

        class Client:
            def with_options(self):
                return Client()

        class Provider:
            def __init__(self):
                self.provider_config = {'provider_source_id': 'PRIVATE-SOURCE-ID',
                    'model': 'configured-model', 'key': ['PRIVATE-CREDENTIAL'],
                    'api_base': 'https://PRIVATE-HOST.invalid/v1'}
                self.client = Client()
                self.model_name = 'selected-model'

            async def text_chat(self, prompt=None, session_id=None, contexts=None, system_prompt=None,
                                func_tool=None, model=None, **kwargs):
                test.calls.append((self, prompt, session_id, contexts, system_prompt, func_tool, model, kwargs))
                return await test.chat_operation()

            async def text_chat_stream(self, prompt=None, contexts=None, model=None, **kwargs):
                test.calls.append((self, prompt, contexts, model, kwargs))
                async for response in test.stream_operation():
                    yield response

        class Stage:
            async def process(self, event, provider_wake_prefix):
                yield event

        class Transport:
            async def call_action(self, *args, **kwargs):
                raise AssertionError('Tests must never send a message')

        self.Provider, self.Stage, self.Transport = Provider, Stage, Transport
        self.dependencies = fake_dependencies(Stage, Provider, Transport)
        self.dependencies.__enter__()
        self.runtime = WorkRuntime(self.cfg.get, lambda name, fields: self.rows.append((name, fields)), clock=self.clock)
        self.plugin = SimpleNamespace(_work=self.runtime, _cfg=self.cfg.get,
                                      _audit=self.runtime.audit, _platform=lambda: None)
        self.restore = install_work_runtime(self.plugin)
        self.provider = Provider()
        self.response = FakeResponse('PRIVATE-RESPONSE', usage=SimpleNamespace(
            input=13, input_other=10, input_cached=3, output=7, total=20),
            id='PRIVATE-RESPONSE-ID', raw_completion={'private': 'PRIVATE-RAW'})

        async def chat():
            self.clock.advance(2)
            return self.response

        async def stream():
            self.clock.advance(1)
            yield FakeResponse('PRIVATE-CHUNK', is_chunk=True, usage=self.response.usage)
            self.clock.advance(2)
            yield self.response

        self.chat_operation, self.stream_operation = chat, stream

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.restore()
        await self.runtime.close()
        self.dependencies.__exit__(None, None, None)

    def start(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.append(task)
        return task

    def metrics(self, name='model_provider_call'):
        return [fields for event, fields in self.rows if event == name]

    async def test_positional_bound_shape_usage_trace_and_privacy(self):
        contexts = [
            SimpleNamespace(role='system', content='PRIVATE-SYSTEM'),
            SimpleNamespace(role='user', content=[
                SimpleNamespace(type='text', text='PRIVATE-CONTENT'),
                SimpleNamespace(type='image_url', image_url={'url': 'https://PRIVATE-IMAGE.invalid', 'id': 'PRIVATE-IMAGE-ID'}),
                {'type': 'audio_url', 'audio_url': {'url': 'https://PRIVATE-AUDIO.invalid'}},
                {'type': 'video_url', 'video_url': 'https://PRIVATE-VIDEO.invalid'},
                {'type': 'file', 'file': {'file_id': 'PRIVATE-FILE-ID'}},
            ]),
            {'role': 'tool', 'content': 'PRIVATE-TOOL-RESULT', 'tool_call_id': 'PRIVATE-CALL-ID'},
        ]
        class Tools:
            tools = [SimpleNamespace(name='PRIVATE-TOOL-NAME', parameters={'secret': 'PRIVATE-SCHEMA'})]
            def get_func_desc_openai_style(self, **kwargs):
                raise AssertionError('Metric collection must not serialize tool schemas')
        tools = Tools()
        event = Event(target='aiocqhttp:GroupMessage:PRIVATE-SENDER_PRIVATE-GROUP', received=100)
        abort = asyncio.Event()
        with self.runtime.reply_work(event):
            result = await self.provider.text_chat('PRIVATE-PROMPT', 'PRIVATE-SESSION-ID', contexts, 'PRIVATE-RULES', tools,
                'override-model', image_urls=['https://PRIVATE-DIRECT-IMAGE.invalid'], audio_urls=['PRIVATE-AUDIO'],
                extra_user_content_parts=[{'type': 'text', 'text': 'PRIVATE-EXTRA'}],
                conversation_id='PRIVATE-CONVERSATION-ID', abort_signal=abort, api_key='PRIVATE-OVERRIDE-CREDENTIAL')
        self.assertIs(result, self.response)
        fields, = self.metrics()
        self.assertEqual(fields['trace_id'], event.get_extra('qqbot_work_trace_id'))
        self.assertEqual(fields['model'], 'override-model')
        self.assertEqual(fields['context_count'], 3)
        self.assertEqual(fields['context_chars'], sum(map(len, ['PRIVATE-SYSTEM', 'PRIVATE-CONTENT', 'PRIVATE-TOOL-RESULT'])))
        self.assertEqual(fields['prompt_chars'], len('PRIVATE-PROMPT'))
        self.assertEqual(fields['system_prompt_chars'], len('PRIVATE-RULES'))
        self.assertEqual(fields['extra_user_content_parts_chars'], len('PRIVATE-EXTRA'))
        self.assertEqual([fields[key] for key in ('image_count', 'audio_count', 'video_count', 'file_count', 'tools_count')], [2, 2, 1, 1, 1])
        self.assertEqual([fields[key] for key in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'total_tokens')], [13, 3, 7, 20])
        self.assertEqual((fields['provider_elapsed_ms'], fields['queue_ms'], fields['status']), (2000, 0, 'ok'))
        self.assertEqual(fields['work_kind'], 'interactive')
        self.assertNotIn('kind', fields)
        self.assertTrue(fields['request_shape_complete'])
        self.assertIs(self.calls[0][3], contexts)
        self.assertIs(self.calls[0][5], tools)
        self.assertIs(self.calls[0][-1]['abort_signal'], abort)
        self.assertFalse(abort.is_set())
        encoded = json.dumps(self.rows)
        self.assertNotIn('PRIVATE-', encoded)
        self.assertNotIn('https://', encoded)
        self.assertEqual(len(self.metrics('model_request')), 1)

    async def test_runner_context_only_and_selected_default_model(self):
        contexts = [SimpleNamespace(role='system', content='rules' + TAIL_GUARD_MARKER),
                    SimpleNamespace(role='user', content=[SimpleNamespace(type='text', text='hello')])]
        with self.runtime.reply_work(Event()):
            await self.provider.text_chat(contexts=contexts)
        row, = self.metrics()
        self.assertEqual((row['context_count'], row['context_chars'], row['prompt_chars'], row['system_prompt_chars']), (2, 10, 0, 0))
        self.assertEqual(row['model'], 'selected-model')
        self.assertEqual(row['call_role'], 'primary')
        self.assertIn(TAIL_GUARD_MARKER, contexts[0].content)

    async def test_scheduler_wait_excluded_and_queued_cancel_emits_no_provider_call(self):
        self.cfg['model_concurrency'] = 1
        gate, entered = asyncio.Event(), asyncio.Event()
        async def blocking():
            entered.set()
            await gate.wait()
        blocker = self.start(self.runtime.scheduler.run(self.provider, blocking))
        await entered.wait()
        waiting = self.start(self.provider.text_chat('PRIVATE-WAITING'))
        cancelled = self.start(self.provider.text_chat('PRIVATE-CANCELLED'))
        await until(lambda: self.runtime.scheduler.status()['waiting'] == 2)
        self.assertFalse(self.metrics('model_request'))
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.clock.advance(30)
        gate.set()
        await blocker
        self.assertIs(await waiting, self.response)
        row, = self.metrics()
        self.assertEqual((row['queue_ms'], row['provider_elapsed_ms']), (30000, 2000))
        self.assertEqual(len(self.calls), 1)

    async def test_hedge_both_attempts_same_trace_and_shared_abort_unset(self):
        first = asyncio.Event()
        timer = asyncio.Event()
        abort = asyncio.Event()
        self.runtime._hedge_sleep = lambda delay: timer.wait()
        async def operation():
            if len(self.calls) == 1:
                first.set()
                await asyncio.Future()
            self.clock.advance(4)
            return self.response
        self.chat_operation = operation
        async def turn():
            with self.runtime.reply_work(Event()):
                return await self.provider.text_chat(contexts=[{'role': 'system', 'content': TAIL_GUARD_MARKER},
                    {'role': 'user', 'content': 'PRIVATE-HEDGE'}], abort_signal=abort, model='override-model')
        task = self.start(turn())
        await first.wait()
        self.clock.advance(15)
        timer.set()
        self.assertIs(await task, self.response)
        rows = {row['call_role']: row for row in self.metrics()}
        self.assertEqual(set(rows), {'primary', 'hedge'})
        self.assertEqual(rows['primary']['status'], 'cancelled')
        self.assertEqual(rows['hedge']['status'], 'ok')
        self.assertEqual(rows['primary']['provider_elapsed_ms'], 19000)
        self.assertEqual(rows['hedge']['provider_elapsed_ms'], 4000)
        self.assertEqual(rows['primary']['trace_id'], rows['hedge']['trace_id'])
        self.assertEqual(rows['hedge']['work_kind'], 'auxiliary')
        self.assertIsNone(rows['primary']['total_tokens'])
        self.assertEqual(rows['hedge']['total_tokens'], 20)
        self.assertFalse(abort.is_set())
        self.assertTrue(all(call[-1]['abort_signal'] is abort for call in self.calls))
        self.assertEqual(self.runtime.scheduler.status()['running'], 0)

    async def test_new_interactive_reclaims_running_hedge_slot(self):
        timer, first, second, release = (asyncio.Event() for _ in range(4))
        self.runtime._hedge_sleep = lambda delay: timer.wait()
        async def operation():
            index = len(self.calls)
            if index == 1:
                first.set()
                await release.wait()
            elif index == 2:
                second.set()
                await asyncio.Future()
            return self.response
        self.chat_operation = operation
        async def turn():
            with self.runtime.reply_work(Event()):
                return await self.provider.text_chat(system_prompt=TAIL_GUARD_MARKER)
        race = self.start(turn())
        await first.wait()
        timer.set()
        await second.wait()
        explicit = self.start(self.provider.text_chat('PRIVATE-EXPLICIT'))
        await until(lambda: len(self.calls) == 3)
        self.assertIs(await explicit, self.response)
        self.assertFalse(race.done())
        release.set()
        await asyncio.gather(race, explicit)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.metrics()), 3)
        hedge, = [row for row in self.metrics() if row['call_role'] == 'hedge']
        self.assertEqual(hedge['status'], 'preempted')

    async def test_stream_pull_time_final_usage_and_early_close(self):
        iterator = self.provider.text_chat_stream(contexts=[{'role': 'user', 'content': 'PRIVATE-STREAM'}])
        first = await anext(iterator)
        self.assertTrue(first.is_chunk)
        self.assertFalse(self.metrics())
        self.clock.advance(90)  # synthetic consumer/tool work, not provider I/O
        self.assertIs(await anext(iterator), self.response)
        await iterator.aclose()
        row, = self.metrics()
        self.assertEqual((row['mode'], row['provider_elapsed_ms'], row['status']), ('stream', 3000, 'ok'))
        self.assertEqual(row['total_tokens'], 20)  # do not sum cumulative snapshots
        self.rows.clear()
        iterator = self.provider.text_chat_stream()
        await anext(iterator)
        self.clock.advance(90)
        await iterator.aclose()
        row, = self.metrics()
        self.assertEqual((row['provider_elapsed_ms'], row['status']), (1000, 'cancelled'))
        self.assertEqual(self.runtime.scheduler.status()['running'], 0)

    async def test_stream_read_cancellation_reaps_worker_and_preserves_exception(self):
        entered = asyncio.Event()
        async def blocked_stream():
            entered.set()
            await asyncio.Future()
            yield self.response
        self.stream_operation = blocked_stream
        iterator = self.provider.text_chat_stream()
        task = self.start(anext(iterator))
        await entered.wait()
        self.clock.advance(5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await iterator.aclose()
        row, = self.metrics()
        self.assertEqual((row['status'], row['provider_elapsed_ms']), ('cancelled', 5000))
        self.assertIsNone(row['total_tokens'])
        self.assertEqual(self.runtime.scheduler.status()['running'], 0)

    async def test_failed_call_unknown_usage_and_audit_failure_never_retry(self):
        error = ValueError('PRIVATE-ERROR https://PRIVATE-URL.invalid')
        async def fail():
            self.clock.advance(6)
            raise error
        self.chat_operation = fail
        with self.assertRaises(ValueError) as caught:
            await self.provider.text_chat()
        self.assertIs(caught.exception, error)
        row, = self.metrics()
        self.assertEqual((row['status'], row['provider_elapsed_ms']), ('failed', 6000))
        self.assertIsNone(row['input_tokens'])
        self.assertNotIn('PRIVATE-', json.dumps(self.rows))
        self.assertEqual(len(self.calls), 1)
        def broken_audit(*args):
            raise RuntimeError('PRIVATE-AUDIT-ERROR')
        self.runtime.audit = broken_audit
        async def ok():
            return self.response
        self.chat_operation = ok
        self.assertIs(await self.provider.text_chat(), self.response)
        self.assertEqual(len(self.calls), 2)

    async def test_opaque_inputs_not_consumed_or_stringified(self):
        class Opaque:
            def __iter__(self):
                raise AssertionError('Do not consume application iterators')
            def __str__(self):
                raise AssertionError('Do not stringify application objects')
        opaque = Opaque()
        await self.provider.text_chat(contexts=opaque, func_tool=opaque, image_urls=opaque)
        row, = self.metrics()
        self.assertIsNone(row['context_count'])
        self.assertIsNone(row['context_chars'])
        self.assertIsNone(row['tools_count'])
        self.assertIsNone(row['image_count'])
        self.assertFalse(row['request_shape_complete'])
        self.assertIs(self.calls[0][3], opaque)

    async def test_disabled_and_reentrant_adapters_keep_existing_behavior(self):
        self.cfg['latency_optimization_enabled'] = False
        self.assertIs(await self.provider.text_chat(), self.response)
        self.assertFalse(self.rows)
        self.cfg['latency_optimization_enabled'] = True
        async def nested():
            async for item in self.provider.text_chat_stream():
                if not item.is_chunk:
                    return item
        self.chat_operation = nested
        self.assertIs(await self.provider.text_chat(), self.response)
        self.assertEqual(len(self.metrics('model_request')), 1)
        self.assertEqual(len(self.metrics()), 1)
        self.assertEqual(self.metrics()[0]['mode'], 'nonstream')


class InstalledTypesMetricsTests(unittest.TestCase):
    def test_actual_message_toolset_and_usage_types_with_synthetic_history(self):
        if importlib.util.find_spec('astrbot') is None:
            self.skipTest('Optional AstrBot installation is not available')
        script = r'''
import inspect, json, os, socket, sys
os.environ['ASTRBOT_ROOT'] = os.getcwd()
sys.path.insert(0, sys.argv[1])
def no_network(*args, **kwargs):
    raise AssertionError('This test must never access a network')
socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.create_connection = no_network
from astrbot.core.agent.message import Message, TextPart, ImageURLPart
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import LLMResponse, TokenUsage
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from data.plugins.astrbot_plugin_proactive_agent.work_scheduler import _request_metrics, _usage_metrics
p = object.__new__(ProviderOpenAIOfficial)
p.model_name = 'actual-selected-model'
p.provider_config = {'model': 'configured-model'}
image = ImageURLPart(image_url=ImageURLPart.ImageURL(url='data:image/png;base64,' + 'A' * 250000))
contexts = [Message(role='system', content='PRIVATE-RULES'),
            Message(role='user', content=[TextPart(text='PRIVATE-OLD'), *([image] * 14)]),
            Message(role='user', content='PRIVATE-CURRENT')]
tools = ToolSet([FunctionTool(name='PRIVATE-TOOL', description='PRIVATE-DESCRIPTION', parameters={'type': 'object'})])
fields = _request_metrics(inspect.signature(ProviderOpenAIOfficial.text_chat), p, (),
    {'contexts': contexts, 'func_tool': tools, 'model': 'override-model', 'conversation_id': 'PRIVATE-ID'})
assert fields['context_count'] == 3 and fields['image_count'] == 14 and fields['tools_count'] == 1, fields
assert fields['context_chars'] == len('PRIVATE-RULESPRIVATE-OLDPRIVATE-CURRENT'), fields
assert fields['prompt_chars'] == 0 and fields['system_prompt_chars'] == 0, fields
assert fields['model'] == 'override-model' and fields['request_shape_complete'], fields
assert _request_metrics(inspect.signature(ProviderOpenAIOfficial.text_chat), p, (), {})['model'] == 'actual-selected-model'
usage = _usage_metrics(LLMResponse(role='assistant', usage=TokenUsage(input_other=30000, input_cached=5000, output=17)))
assert usage == {'input_tokens': 35000, 'cached_input_tokens': 5000, 'output_tokens': 17, 'total_tokens': 35017}, usage
assert 'PRIVATE-' not in json.dumps(fields) and 'data:image/' not in json.dumps(fields)
assert len(contexts[1].content) == 15
print('verified installed types with synthetic history; no provider call')
'''
        with tempfile.TemporaryDirectory(prefix='qqbot-request-metrics-') as directory:
            result = subprocess.run([sys.executable, '-B', '-c', script, str(Path(__file__).resolve().parents[1] / 'AstrBot')],
                cwd=directory, capture_output=True, encoding='utf-8', errors='replace', timeout=50)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('verified installed types with synthetic history; no provider call', result.stdout)


if __name__ == '__main__':
    unittest.main()
