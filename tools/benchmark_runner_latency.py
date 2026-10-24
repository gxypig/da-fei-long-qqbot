"""Opt-in, paired real ToolLoopAgentRunner benchmark. No QQ or tool execution.

This measures Runner generation + source scheduling ONLY, not final review or
emoji delivery. It must not be presented as end-to-end production reply latency.
The reusable build_runner() accepts the same ProviderRequest shapes as AstrBot.

Cases JSON: [{"prompt": "...", "system_prompt": "...", "contexts": [...],
              "guard_eligible": true}]. Supply the complete 9k/20k histories;
this script does not shorten them or replace their tools/reasoning settings.
Only trusted ordinary-chat fixtures should set guard_eligible. Every pair gets
the identical frozen cases, live provider config, tool schemas and runtime config.

Example (omit --execute to inspect the non-secret plan):
  python tools/benchmark_runner_latency.py --cases private-cases.json \
    --baseline-archive backups/<backup>/source-config.zip --report logs/runner.json
Real model calls require --execute. --self-test uses fake model I/O exclusively.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
from contextvars import ContextVar
import copy
import hashlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
WORK_PATH = Path('AstrBot/data/plugins/astrbot_plugin_proactive_agent/work_scheduler.py')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


class OfflineEvent:
    """Only identity/timing hooks; this object cannot send anything to QQ."""
    def __init__(self, index=0):
        self.unified_msg_origin = 'aiocqhttp:FriendMessage:benchmark'
        self.message_obj = SimpleNamespace(message_id=str(index))
        self.extras = {'qqbot_received_at': time.monotonic()}

    def get_extra(self, name, default=None):
        return self.extras.get(name, default)

    def set_extra(self, name, value):
        self.extras[name] = value

    def is_stopped(self):
        return False

    async def send(self, *args, **kwargs):
        raise AssertionError('QQ is disabled in this benchmark')


async def build_runner(provider, case, tool_set, *, event=None, hooks=None,
                       streaming=False, request_max_retries=5, runner_config=None):
    """Return (runner, event, tool_attempts); no tools/network run during construction.

    hooks may be an offline adapter of the real review/enrichment hooks supplied
    by a broader benchmark. Default hooks are empty, so CLI timings are explicitly
    generation-only. Model/provider options and complete tool schemas are retained.
    """
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.tool_executor import BaseFunctionToolExecutor
    from astrbot.core.provider.entities import ProviderRequest
    from mcp.types import CallToolResult, TextContent

    attempts = []
    class NeverExecuteTools(BaseFunctionToolExecutor):
        @classmethod
        async def execute(cls, tool, run_context, **kwargs):
            attempts.append(1)
            runner.request_stop()
            yield CallToolResult(isError=True, content=[TextContent(type='text', text='Benchmark tool execution disabled')])

    event = event or OfflineEvent()
    request = ProviderRequest(prompt=case.get('prompt'), system_prompt=case.get('system_prompt', ''),
        contexts=copy.deepcopy(case.get('contexts', [])), func_tool=tool_set,
        model=provider.provider_config.get('model'))
    runner = ToolLoopAgentRunner()
    await runner.reset(provider=provider, request=request,
        run_context=ContextWrapper(context=SimpleNamespace(event=event)),
        tool_executor=NeverExecuteTools(), agent_hooks=hooks or BaseAgentRunHooks(),
        streaming=streaming, request_max_retries=request_max_retries, **(runner_config or {}))
    return runner, event, attempts


def load_scheduler(path):
    # Only the selected scheduler source changes. The shim is an empty simulated
    # send receipt, not a replacement for review in an end-to-end benchmark.
    package = ModuleType('qqbot_runner_benchmark')
    package.__path__ = []
    review = ModuleType(package.__name__ + '.conversation_review')
    review._receipt = ContextVar('offline_receipt', default=None)
    sys.modules[package.__name__] = package
    sys.modules[review.__name__] = review
    name = package.__name__ + '.work_scheduler'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def run_worker(data, scheduler):
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

    rows, metrics = [], []
    plugin = SimpleNamespace(_cfg=data['runtime'].get, _audit=lambda k, v: metrics.append((k, v)), _platform=lambda: None)
    restore = scheduler.install_work_runtime(plugin)
    source = ProviderOpenAIOfficial(data['provider'], data['provider_settings']) if not data['fake'] else None
    if data['fake']:
        source = object.__new__(ProviderOpenAIOfficial)
        source.provider_config = copy.deepcopy(data['provider'])
        source.model_name = data['provider']['model']
        class Client:
            def with_options(self):
                return Client()
        source.client, source.api_keys = Client(), ['offline-only']
    tool_set = ToolSet([FunctionTool(**item) for item in data['tools']])
    try:
        for index, case in enumerate(data['cases']):
            metrics.clear()
            snapshot = digest(case)
            event = OfflineEvent(index)
            runner, _, tool_attempts = await build_runner(source, case, tool_set, event=event,
                streaming=data['streaming'], request_max_retries=data['request_max_retries'],
                runner_config=data['runner_config'])
            started, requested, ok, error = time.monotonic(), 0, False, None
            before = dict(plugin._work.status()['hedge'])
            try:
                async with asyncio.timeout(data['timeout']):
                    with plugin._work.reply_work(event):
                        for _ in range(2):
                            async for response in runner.step():
                                if response.type == 'tool_call':
                                    requested += 1
                                    runner.request_stop()
                            if runner.done():
                                break
                answer = runner.get_final_llm_resp()
                ok = bool(answer and answer.completion_text.strip() and not requested and not tool_attempts
                          and not runner.was_aborted())
            except Exception as exc:
                error = type(exc).__name__  # Never persist API messages, prompts or credentials.
            usage = runner.stats.token_usage
            after = plugin._work.status()['hedge']
            rows.append({'case_index': index, 'input_sha256': snapshot,
                'complete_generation': ok, 'elapsed_ms': round((time.monotonic() - started) * 1000),
                'tool_requests_blocked': requested + len(tool_attempts), 'qq_sends': 0, 'error_type': error,
                'input_tokens': usage.input, 'cached_input_tokens': usage.input_cached, 'output_tokens': usage.output,
                'model_admissions': sum(k == 'model_work' for k, _ in metrics),
                'hedge': {key: after[key] - before[key] for key in before}})
            assert snapshot == digest(case), 'Case mutated during benchmark'
    finally:
        restore()
        await plugin._work.close()
        if not data['fake']:
            await source.client.close()
    return rows


def worker(data):
    # AstrBot creates paths on import; the parent gives this subprocess a temporary
    # cwd. Suppress its logs so model text/config can never escape into the report.
    captured = io.StringIO()
    with redirect_stdout(captured), redirect_stderr(captured):
        logging.disable(logging.CRITICAL)
        scheduler = load_scheduler(Path(data['scheduler_path']))
        if data['fake']:
            from astrbot.core.provider.entities import LLMResponse
            from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
            async def fake_chat(self, contexts=None, **kwargs):
                assert contexts and kwargs.get('system_prompt') is None
                return LLMResponse(role='assistant', completion_text='offline complete body')
            async def fake_stream(self, contexts=None, **kwargs):
                yield await fake_chat(self, contexts=contexts, **kwargs)
            async def offline():
                # Windows builds the event loop's internal socketpair first.
                # Block network only after that local control channel exists.
                with patch.object(ProviderOpenAIOfficial, 'text_chat', fake_chat), \
                     patch.object(ProviderOpenAIOfficial, 'text_chat_stream', fake_stream), \
                     patch('socket.socket.connect', side_effect=AssertionError('Offline self-test')), \
                     patch('socket.socket.connect_ex', side_effect=AssertionError('Offline self-test')):
                    return await run_worker(data, scheduler)
            return asyncio.run(offline())
        return asyncio.run(run_worker(data, scheduler))


def source_pair(live, archive):
    current = (live / WORK_PATH).read_text(encoding='utf-8-sig')
    if archive is None:
        return {'before': current, 'after': current}
    with zipfile.ZipFile(archive) as stored:
        names = [n for n in stored.namelist() if n.replace('\\', '/') == 'project/' + WORK_PATH.as_posix()]
        if not names:
            names = [n for n in stored.namelist() if n.replace('\\', '/').endswith(WORK_PATH.as_posix())
                     and not n.replace('\\', '/').startswith('public/')]
        if len(names) != 1:
            raise ValueError('Expected exactly one baseline scheduler in archive')
        original = stored.read(names[0]).decode('utf-8-sig')
    return {'before': original, 'after': current}


def run_pair(data, sources):
    result = {}
    for label, source in sources.items():
        with tempfile.TemporaryDirectory(prefix='qqbot-runner-' + label + '-') as folder:
            path = Path(folder) / 'work_scheduler.py'
            path.write_text(source, encoding='utf-8')
            payload = {**data, 'scheduler_path': str(path)}
            env = {**os.environ, 'PYTHONUTF8': '1'}
            process = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), '--worker'],
                input=json.dumps(payload), cwd=folder, capture_output=True, encoding='utf-8', errors='replace',
                timeout=max(60, len(data['cases']) * data['timeout'] + 30), env=env)
            if process.returncode:
                raise RuntimeError('Benchmark subprocess failed; private diagnostics suppressed')
            value = json.loads(process.stdout)
            if 'error_type' in value:
                raise RuntimeError('Benchmark worker failed: ' + value['error_type'] + ' ' + str(value.get('frames', [])))
            result[label] = {'source_sha256': hashlib.sha256(source.encode()).hexdigest(), 'samples': value['samples']}
    assert [r['input_sha256'] for r in result['before']['samples']] == [r['input_sha256'] for r in result['after']['samples']]
    return {'scope': 'ToolLoopAgentRunner generation and scheduler only; excludes review, emoji and QQ delivery',
            'input_sha256': digest({k: data[k] for k in data if k != 'fake'}), 'qq_sends': 0, 'tool_executions': 0,
            'tools_count': len(data['tools']), 'variants': result}


def live_data(args):
    # Read configuration once, then feed exactly that in-memory snapshot to both
    # subprocesses. Never read account/model configuration from the backup ZIP.
    live = args.live_root.resolve()
    config = json.loads((live / 'AstrBot/data/cmd_config.json').read_text(encoding='utf-8-sig'))
    runtime = json.loads((live / 'AstrBot/data/config/astrbot_plugin_proactive_agent_config.json').read_text(encoding='utf-8-sig'))
    models = [m for m in config['provider'] if m.get('enable', True)]
    model = next((m for m in models if m.get('id') == args.provider), None) if args.provider else next(
        (m for m in models if m.get('provider_source_id')), None)
    if model is None:
        raise ValueError('No configured provider selected')
    source = next(s for s in config['provider_sources'] if s.get('id') == model['provider_source_id'])
    spec = importlib.util.spec_from_file_location('benchmark_local_admin', live / 'tools/local_admin.py')
    admin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admin)
    advertised = admin.api('/api/tools/list')['data']  # Read-only local dashboard.
    tools = [{k: row[k] for k in ('name', 'description', 'parameters')} for row in advertised
             if row.get('active') and (not row.get('builtin_config_statuses') or any(
                 s.get('enabled') and s.get('conf_id') == 'default' for s in row['builtin_config_statuses']))]
    cases = json.loads(args.cases.read_text(encoding='utf-8-sig'))
    if not isinstance(cases, list) or not cases:
        raise ValueError('Cases must be a nonempty list')
    for case in cases:
        if not isinstance(case, dict) or set(case) - {'prompt', 'contexts', 'system_prompt', 'guard_eligible'}:
            raise ValueError('Unsupported case fields')
        if not isinstance(case.get('contexts', []), list) or not isinstance(case.get('system_prompt', ''), str):
            raise ValueError('Invalid complete context/system prompt')
        if case.pop('guard_eligible', False):
            case['system_prompt'] = case.get('system_prompt', '') + '\n[QQBOT_CASUAL_TAIL_GUARD_V1]'
    runner_cfg = config.get('agent_runner', {}).get('config', {})
    compression = runner_cfg.get('compression', {})
    # Fixtures should represent provider-bound contexts within the configured
    # limits. Keep the existing Runner parameters; never silently tune them for
    # faster benchmark numbers or change the model/reasoning configuration.
    return {'provider': {**source, **model}, 'provider_settings': config['provider_settings'],
            'runtime': runtime, 'cases': cases, 'tools': tools, 'fake': False,
            'streaming': config['provider_settings'].get('streaming_response', False),
            'request_max_retries': runner_cfg.get('model', {}).get('request_max_retries', 5),
            'runner_config': {'enforce_max_turns': compression.get('max_turns', -1),
                              'truncate_turns': compression.get('trim_turns', 1),
                              'tool_schema_mode': runner_cfg.get('misc', {}).get('tool_schema_mode', 'full')},
            'timeout': args.timeout}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-root', type=Path, default=ROOT)
    parser.add_argument('--baseline-archive', type=Path)
    parser.add_argument('--cases', type=Path)
    parser.add_argument('--provider', default='')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--execute', action='store_true', help='Explicitly allow real model API calls')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        data = {'provider': {'model': 'offline', 'provider_source_id': 'offline', 'max_context_tokens': 0},
            'provider_settings': {}, 'runtime': {'latency_optimization_enabled': True}, 'fake': True,
            'cases': [{'prompt': 'synthetic current turn', 'system_prompt': 'fixed\n[QQBOT_CASUAL_TAIL_GUARD_V1]',
                       'contexts': [{'role': 'user', 'content': 'synthetic history'}]}],
            'tools': [], 'streaming': False, 'request_max_retries': 5,
            'runner_config': {'enforce_max_turns': -1}, 'timeout': 10}
        report = run_pair(data, source_pair(args.live_root, args.baseline_archive))
        assert all(v['samples'][0]['complete_generation'] for v in report['variants'].values())
        print(json.dumps({'offline_self_test': 'passed', 'real_api_calls': 0, 'qq_sends': 0}))
        return
    if not args.cases or not args.baseline_archive:
        parser.error('--cases and --baseline-archive are required for paired benchmarking')
    if not args.execute:
        print(json.dumps({'mode': 'plan_only', 'real_api_calls': 0, 'scope': 'generation only',
                          'next': 'Use --execute for a paired real-model run; QQ/tools remain disabled'}))
        return
    if not args.report:
        parser.error('--report is required with --execute')
    report = run_pair(live_data(args), source_pair(args.live_root, args.baseline_archive))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report_saved': True, 'qq_sends': 0, 'tool_executions': 0}))


if __name__ == '__main__':
    if sys.argv[1:] == ['--worker']:
        try:
            value = {'samples': worker(json.load(sys.stdin))}
        except Exception as exc:
            value = {'error_type': type(exc).__name__,
                     'frames': [(Path(frame.filename).name, frame.name, frame.lineno)
                                for frame in traceback.extract_tb(exc.__traceback__)[-4:]]}
        print(json.dumps(value))
    else:
        main()
