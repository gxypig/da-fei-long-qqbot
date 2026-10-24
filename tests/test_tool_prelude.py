"""Offline real-AstrBot runner/executor/history tests: no model, QQ or services.

Only model responses, hook registration and the final transport are fixtures.
ToolLoopAgentRunner, run_agent, MainAgentHooks, FunctionToolExecutor, the plugin's
final hooks, exact-text approval guard and history serialization are real code.
"""
import asyncio
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_maintenance import Main  # Imports AstrBot inside its temp data sandbox.
from aiocqhttp import CQHttp
from astrbot.core.agent.message import Message, TextPart
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_hooks import MainAgentHooks
from astrbot.core.astr_agent_run_util import run_agent
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.message.components import Plain, Image, Video, At, Reply
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.star_handler import EventType
from data.plugins.astrbot_plugin_proactive_agent.conversation_review import (
    attachment_policy, bind_request, confirmed_sent, install_outbound_guard,
    sent_text, ReviewUnavailable,
)
from data.plugins.astrbot_plugin_proactive_agent.tool_prelude import (
    install_tool_prelude, is_plan_only, MAX_PRELUDES,
)


def response(text, tools=(), *, chain=None, role='assistant', ids=None):
    return LLMResponse(role=role, completion_text=text, result_chain=chain,
                       tools_call_name=list(tools), tools_call_args=[{} for _ in tools],
                       tools_call_ids=ids or [f'call-{i}-{name}' for i, name in enumerate(tools)])


class Provider:
    provider_config = {'id': 'offline', 'max_context_tokens': 0}

    def __init__(self, responses, timeline):
        self.responses = list(responses)
        self.calls = []
        self.timeline = timeline

    def get_model(self):
        return 'offline-fixture'

    def meta(self):
        return SimpleNamespace(type='offline-fixture')

    async def text_chat(self, **kwargs):
        self.calls.append(copy.deepcopy({k: v for k, v in kwargs.items()
                                         if k not in {'abort_signal', 'func_tool'}}))
        self.timeline.append(('model', len(self.calls)))
        answer = self.responses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def text_chat_stream(self, **kwargs):
        answer = await self.text_chat(**kwargs)
        yield LLMResponse(role='assistant',
            completion_text=answer.completion_text, is_chunk=True)
        yield answer


class Event:
    def __init__(self, bot, timeline, target='100', platform='aiocqhttp'):
        self.bot, self.timeline = bot, timeline
        self.target, self.platform = target, platform
        self.unified_msg_origin = f'{platform}:GroupMessage:{target}'
        self.extras = {}
        self.result = None
        self.stopped = False
        self.message_str = '帮我看看这个网页'
        self.trace = SimpleNamespace(record=Mock())
        self.plugins_name = None
        self.admin = False

    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
    def get_platform_id(self): return self.platform
    def get_platform_name(self): return self.platform
    def get_group_id(self): return self.target
    def get_sender_id(self): return '200'
    def get_message_str(self): return self.message_str
    def get_result(self): return self.result
    def set_result(self, result): self.result = result
    def clear_result(self): self.result = None
    def is_stopped(self): return self.stopped
    def stop_event(self): self.stopped = True
    def is_admin(self): return self.admin

    async def send(self, chain):
        message = [({'type': 'text', 'data': {'text': part.text}}
                    if isinstance(part, Plain) else part.toDict()) for part in chain.chain]
        await self.bot.send_group_msg(group_id=self.target, message=message)


class PlanGrammar(unittest.TestCase):
    def test_only_prospective_read_search_acknowledgements_qualify(self):
        for text in ['本龙先查一下', '稍等，我先点开看看', '我先读取网页内容',
                     '好呀，本龙去找找相关视频', '先看看评论，再查一下来源',
                     '本龙先打开刚才发的这条看看，不能只凭标题下结论',
                     "I'll check the page", 'Let me read that link']:
            with self.subTest(text=text): self.assertTrue(is_plan_only(text))

    def test_results_permission_promises_quotes_and_ambiguous_text_stay_visible(self):
        for text in ['查好了，日期是五月', '已经发出去了，我再查一下', '本龙查到答案了',
                     '先查一下，需要你同意吗？', '你确认后我再打开', '先删除再查一下',
                     '这是结果：https://example.invalid', '我先查一下，42',
                     '先看看他说的“已删除”', '计划成功了', '本龙先查一下，顺便夸你两句',
                     '不能只凭标题下结论', '先查一下，不能光凭文案瞎猜，第一项已完成',
                     '稍等', '我看了原文', '本龙查了结果', '我准备给别人发消息',
                     '本龙先查一下\n' + '很长' * 130]:
            with self.subTest(text=text): self.assertFalse(is_plan_only(text))


class RealRunner(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.timeline, self.sends, self.reviews = [], [], []
        self.bot = object.__new__(CQHttp)
        async def transport(action, **params):
            self.timeline.append(('sent', copy.deepcopy(params)))
            self.sends.append(copy.deepcopy(params))
            return {'message_id': len(self.sends)}
        self.bot._api = SimpleNamespace(call_action=transport)
        self.plugin = object.__new__(Main)
        self.plugin.config = {'latency_optimization_enabled': True,
                              'reply_review_enabled': True, 'review_policy': 'all'}
        self.plugin._audit = Mock()
        self.plugin._platform = lambda: SimpleNamespace(bot=self.bot)
        self.plugin.review_context = lambda origin: ['帮我看看网页']
        self.plugin.review_mode = lambda origin: 'chat'
        self.plugin.character_context = lambda event: {}
        self.plugin._init_reply_pipeline()
        self.rewrite = None
        self.reject = False
        async def review(text, **kwargs):
            self.timeline.append(('review', text))
            self.reviews.append((text, kwargs))
            if self.reject:
                raise ReviewUnavailable('offline rejection')
            return self.rewrite if self.rewrite is not None else text
        self.plugin._reviewer = SimpleNamespace(review=review)
        self.after_final = None
        async def hooks(event, kind, *args, **kwargs):
            if kind == EventType.OnAgentDoneEvent:
                await self.plugin.prepare_chat_punctuation(event, *args)
                if not event.is_stopped():
                    await self.plugin.review_agent_reply(event, *args)
                if self.after_final:
                    self.after_final(event, *args)
            return event.is_stopped()
        self.hook_patch = patch('astrbot.core.astr_agent_hooks.call_event_hook', side_effect=hooks)
        self.hook_patch.start()
        self.addCleanup(self.hook_patch.stop)
        self.restore_guard = install_outbound_guard(self.plugin, self.plugin._reviewer)
        self.addCleanup(self.restore_guard)
        self.original_step = ToolLoopAgentRunner.step
        self.original_done = MainAgentHooks.on_agent_done
        self.restore = install_tool_prelude(self.plugin)
        self.addCleanup(self.restore)

    async def make(self, responses, handlers=None, *, target='100', streaming=False,
                   mode='full', platform='aiocqhttp'):
        event = Event(self.bot, self.timeline, target, platform)
        async def read(event):
            self.timeline.append(('tool', 'read'))
            return '事实：测试网页介绍白龙；链接 https://example.invalid/source'
        handlers = {'read': read} if handlers is None else handlers
        tools = ToolSet([FunctionTool(name=name, description='Offline fixture',
            parameters={'type': 'object', 'properties': {}}, handler=handler)
            for name, handler in handlers.items()])
        provider = Provider(responses, self.timeline)
        request = ProviderRequest(prompt=event.message_str, system_prompt='Offline test',
                                  func_tool=tools, contexts=[])
        context = ContextWrapper(context=SimpleNamespace(event=event))
        runner = ToolLoopAgentRunner()
        await runner.reset(provider=provider, request=request, run_context=context,
            tool_executor=FunctionToolExecutor(), agent_hooks=MainAgentHooks(),
            streaming=streaming, tool_schema_mode=mode, request_max_retries=0)
        return runner, event, provider

    async def drive(self, runner, event, *, buffering=False, show_tools=False,
                    send_stream=False, mutate=None):
        bind_request(event.unified_msg_origin, ['帮我看看网页'], False)
        async for chain in run_agent(runner, max_step=12, show_tool_use=show_tools,
                                     buffer_intermediate_messages=buffering):
            if event.is_stopped():
                continue
            payload = chain if send_stream else event.get_result()
            if payload and payload.chain:
                if mutate: mutate(payload)
                await event.send(payload)
        if event.get_result() and event.get_result().chain and not event.is_stopped():
            await event.send(event.get_result())

    def sent_texts(self):
        return ['\n'.join(s['data']['text'] for s in params['message']
                          if s['type'] == 'text' and s['data']['text']) for params in self.sends]

    async def test_real_runner_executes_tool_before_only_final_review_and_send(self):
        runner, event, provider = await self.make([
            response('本龙先点开看看', ['read']), response('网页介绍的是白龙')])
        await self.drive(runner, event)
        self.assertEqual([r[0] for r in self.timeline], ['model', 'tool', 'model', 'review', 'sent'])
        self.assertEqual(self.sent_texts(), ['本龙先点开看看\n网页介绍的是白龙'])
        self.assertEqual(len(self.reviews), 1)
        self.assertTrue(confirmed_sent(event.unified_msg_origin, self.sent_texts()[0]))
        self.assertEqual(provider.calls[1]['contexts'][2].tool_calls[0].id, 'call-0-read')
        self.assertIn('测试网页', provider.calls[1]['contexts'][3].content)
        self.assertNotIn('本龙先点开看看', self.reviews[0][1]['prior_replies'])
        self.assertFalse(hasattr(runner.run_context, '_qqbot_tool_prelude'))
        self.assertTrue(event.get_extra('qqbot_agent_final_done'))

    async def test_reference_and_verification_qualifier_is_held_until_final_review(self):
        # Synthetic wording, not a user's private chat or a live URL.
        prelude = '本龙先打开刚才发的这条看看，不能只凭标题下结论'
        runner, event, _ = await self.make([response(prelude, ['read']), response('读到的有效信息')])
        await self.drive(runner, event)
        self.assertEqual([row[0] for row in self.timeline], ['model', 'tool', 'model', 'review', 'sent'])
        self.assertEqual(self.sent_texts(), [prelude + '\n读到的有效信息'])
        self.assertEqual(len(self.reviews), 1)

    async def test_final_marker_false_for_intermediate_sentence_true_only_after_review(self):
        runner, event, _ = await self.make([
            response('第一项已经完成，再看看第二项', ['read']), response('最终有效信息')])
        observed = []
        with attachment_policy(lambda action, params: observed.append(event.get_extra('qqbot_agent_final_done'))):
            await self.drive(runner, event)
        self.assertEqual(observed, [False, True])

    async def test_marker_clears_when_event_is_reused_for_another_run(self):
        runner, event, _ = await self.make([response('第一轮完成')])
        await self.drive(runner, event)
        self.assertTrue(event.get_extra('qqbot_agent_final_done'))
        second, _, _ = await self.make([
            response('先前结果已经发出，现在查第二项', ['read']), response('第二轮完成')])
        second.run_context.context.event = event
        observed = []
        with attachment_policy(lambda action, params: observed.append(event.get_extra('qqbot_agent_final_done'))):
            await self.drive(second, event)
        self.assertEqual(observed, [False, True])

    async def test_real_internal_pipeline_respond_adapter_and_history_consume_one_approval(self):
        from astrbot.core.config.default import DEFAULT_CONFIG
        from astrbot.core.pipeline.scheduler import PipelineScheduler
        from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
        from astrbot.core.pipeline.respond.stage import RespondStage
        from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        from astrbot.core.star.session_llm_manager import SessionServiceManager
        from astrbot.core.utils.metrics import Metric

        config = copy.deepcopy(DEFAULT_CONFIG)
        config['provider_settings'].update(enable=True, streaming_response=False,
            show_tool_use_status=False, buffer_intermediate_messages=False, display_reasoning_text=False)
        config['platform_settings'].update(reply_prefix='', reply_with_mention=False, reply_with_quote=False,
                                            forward_threshold=10000)
        config['platform_settings']['segmented_reply']['enable'] = False
        config['provider_tts_settings']['enable'] = False
        config['content_safety']['also_use_in_response'] = False
        config['t2i'] = False
        conv = SimpleNamespace(update_conversation=AsyncMock())
        plugin_context = SimpleNamespace(get_config=lambda *a, **k: config, conversation_manager=conv,
                                        get_using_tts_provider_async=AsyncMock(return_value=None))
        ctx = SimpleNamespace(astrbot_config=config, plugin_manager=SimpleNamespace(context=plugin_context))
        internal, decorate, respond = InternalAgentSubStage(), ResultDecorateStage(), RespondStage()
        await internal.initialize(ctx)
        await decorate.initialize(ctx)
        await respond.initialize(ctx)
        runner, _, provider = await self.make([response('本龙先查一下', ['read']), response('最终有效信息')])
        message = AstrBotMessage()
        message.type, message.self_id, message.session_id = MessageType.GROUP_MESSAGE, '999', '200_100'
        message.message_id, message.group_id, message.sender = 'pipeline-in', '100', MessageMember('200', 'offline')
        message.message, message.message_str, message.raw_message = [Plain('看看网页')], '看看网页', None
        event = AiocqhttpMessageEvent('看看网页', message,
            PlatformMetadata('aiocqhttp', 'offline', 'aiocqhttp'), '200_100', self.bot)
        event.is_at_or_wake_command = True
        runner.run_context.context.event = event
        runner.req.conversation = SimpleNamespace(cid='offline-pipeline-history', token_usage=0)
        async def build(**kwargs):
            return SimpleNamespace(agent_runner=runner, provider_request=runner.req,
                                   provider=provider, reset_coro=None)
        class Entry:
            async def process(self, e):
                async for value in internal.process(e, ''): yield value
        scheduler = object.__new__(PipelineScheduler)
        scheduler.ctx, scheduler.stages = ctx, [Entry(), decorate, respond]
        module = 'astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal'
        with patch(module + '.build_main_agent', side_effect=build), \
             patch(module + '.call_event_hook', AsyncMock(return_value=False)), \
             patch(module + '._record_internal_agent_stats', AsyncMock()), \
             patch('astrbot.core.star.star_handler.star_handlers_registry.get_handlers_by_event_type', return_value=[]), \
             patch('astrbot.core.pipeline.respond.stage.call_event_hook', AsyncMock(return_value=False)), \
             patch.object(SessionServiceManager, 'should_process_llm_request', AsyncMock(return_value=True)), \
             patch.object(AiocqhttpMessageEvent, 'send_typing', AsyncMock()), \
             patch.object(AiocqhttpMessageEvent, 'stop_typing', AsyncMock()), \
             patch.object(Metric, 'upload', AsyncMock()):
            bind_request(event.unified_msg_origin, ['看看网页'], False)
            await scheduler.execute(event)
            await asyncio.sleep(0)
        self.assertEqual(self.sent_texts(), ['本龙先查一下\n最终有效信息'])
        self.assertEqual(len(self.reviews), 1)
        saved = conv.update_conversation.call_args.kwargs['history']
        self.assertEqual(saved[-1]['content'], [{'type': 'text', 'text': self.sent_texts()[0]}])
        self.assertEqual([row[0] for row in self.timeline], ['model', 'tool', 'model', 'review', 'sent'])

    async def test_no_tool_permission_is_granted_by_deferring_plan(self):
        effects = []
        async def protected(event):
            if not event.is_admin(): return '权限不足，未执行'
            effects.append('write')
            return '已执行'
        runner, event, _ = await self.make([
            response('本龙先查一下', ['protected']), response('权限不足，未执行')], {'protected': protected})
        await self.drive(runner, event)
        self.assertEqual(effects, [])
        self.assertIn('权限不足，未执行', self.sent_texts()[0])
        self.assertEqual(len(self.reviews), 1)

    async def test_disabled_keeps_original_pre_tool_review_order(self):
        self.plugin.config['latency_optimization_enabled'] = False
        runner, event, _ = await self.make([response('本龙先查一下', ['read']), response('查到了')])
        await self.drive(runner, event)
        self.assertEqual([r[0] for r in self.timeline],
                         ['model', 'review', 'sent', 'tool', 'model', 'review', 'sent'])
        self.assertEqual(self.sent_texts(), ['本龙先查一下', '查到了'])

    async def test_optional_review_off_retains_original_unbuffered_behavior(self):
        self.restore_guard()
        self.plugin.config['reply_review_enabled'] = False
        runner, event, _ = await self.make([response('本龙先查一下', ['read']), response('查到了')])
        await self.drive(runner, event)
        self.assertEqual(self.sent_texts(), ['本龙先查一下', '查到了'])
        self.assertEqual(self.reviews, [])
        self.assertTrue(event.get_extra('qqbot_agent_final_done'))

    async def test_speed_first_still_reviews_merged_answer_after_context_compression(self):
        self.plugin.config['review_policy'] = 'speed_first'
        runner, event, _ = await self.make([response('本龙先查一下', ['read']), response('这内容看着挺有意思')])
        bind_request(event.unified_msg_origin, ['随便看看'], False)
        async for _ in runner.step(): pass
        # The real runner supports context compaction and tool-image user turns;
        # either can hide earlier tool calls from a latest-user scan. Retain our
        # per-run evidence so that the merged draft still takes the full review.
        runner.request_context_manager.process = AsyncMock(return_value=[Message(role='user', content='随便看看')])
        event.message_str = '随便看看'
        await self.drive(runner, event)
        self.assertEqual(len(self.reviews), 1)
        self.assertIn('本龙先查一下', self.reviews[0][0])

    async def test_completed_information_is_not_misclassified_as_planning(self):
        runner, event, _ = await self.make([
            response('第一项已完成，已经发出去了；我再查第二项', ['read']), response('第二项也查到了')])
        await self.drive(runner, event)
        self.assertEqual(len(self.sends), 2)
        self.assertTrue(self.sent_texts()[0].startswith('第一项已完成'))
        self.assertLess(next(i for i, r in enumerate(self.timeline) if r[0] == 'review'),
                        next(i for i, r in enumerate(self.timeline) if r[0] == 'tool'))

    async def test_permission_question_and_real_executor_permission_check_unchanged(self):
        effects = []
        async def protected(event):
            self.timeline.append(('permission', event.is_admin()))
            if not event.is_admin(): return '权限不足，未执行'
            effects.append('write')
            return '已执行'
        runner, event, _ = await self.make([
            response('要先获得你的确认，是否继续？', ['protected']), response('权限不足，未执行')],
            {'protected': protected})
        await self.drive(runner, event)
        self.assertEqual(effects, [])
        self.assertIn(('permission', False), self.timeline)
        self.assertEqual(self.sent_texts()[0], '要先获得你的确认，是否继续？')

    async def test_multiple_rounds_and_multiple_tools_keep_order_and_exactly_once_effects(self):
        effects = []
        async def first(event): effects.append('first'); return 'FIRST-RESULT'
        async def second(event): effects.append('second'); return 'SECOND-RESULT'
        runner, event, provider = await self.make([
            response('本龙先查一下', ['first', 'second']),
            response('再看看来源', ['first'], ids=['third-call']),
            response('第一项和第二项都查完了')], {'first': first, 'second': second})
        await self.drive(runner, event)
        self.assertEqual(effects, ['first', 'second', 'first'])
        self.assertEqual(len(self.reviews), 1)
        self.assertEqual(len(self.sends), 1)
        self.assertIn('再看看来源\n第一项和第二项', self.sent_texts()[0])
        self.assertEqual([m.tool_call_id for m in runner.run_context.messages if m.role == 'tool'],
                         ['call-0-first', 'call-1-second', 'third-call'])
        self.assertEqual(len(provider.calls), 3)

    async def test_direct_tool_image_video_and_text_are_reviewed_without_plan_only_followup(self):
        async def direct(event):
            self.timeline.append(('tool', 'direct'))
            yield MessageEventResult(chain=[Plain('这是工具发来的结果'),
                Image(file='https://example.invalid/a.png'), Video(file='https://example.invalid/a.mp4')])
        runner, event, provider = await self.make([response('本龙先看看', ['direct'])], {'direct': direct})
        await self.drive(runner, event)
        self.assertEqual(self.sent_texts(), ['这是工具发来的结果'])
        self.assertEqual(len(self.reviews), 1)
        self.assertEqual([s['type'] for s in self.sends[0]['message']], ['text', 'image', 'video'])
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(hasattr(runner.run_context, '_qqbot_tool_prelude'))

    async def test_direct_send_then_other_tool_effect_is_not_suppressed_or_replayed(self):
        effects = []
        async def direct(event):
            effects.append('direct')
            await event.send(MessageChain([Plain('直接发的结果')]))
            return None
        async def second(event): effects.append('second'); return '第二个结果'
        runner, event, _ = await self.make([response('本龙先查一下', ['direct', 'second'])],
                                          {'direct': direct, 'second': second})
        await self.drive(runner, event)
        self.assertEqual(effects, ['direct', 'second'])
        self.assertEqual(self.sent_texts(), ['直接发的结果'])
        self.assertEqual(len([m for m in runner.run_context.messages if m.role == 'tool']), 2)

    async def test_rich_intermediate_chain_remains_atomic_and_reviewed_before_tools(self):
        chain = MessageChain([Plain('本龙先查一下'), Image(file='https://example.invalid/pre.png')])
        runner, event, _ = await self.make([response('', ['read'], chain=chain), response('最终文字')])
        await self.drive(runner, event)
        self.assertEqual(self.sent_texts(), ['本龙先查一下', '最终文字'])
        self.assertEqual(self.sends[0]['message'][1]['type'], 'image')
        self.assertEqual(len(self.reviews), 2)

    async def test_final_composite_media_mentions_and_reply_survive_review_and_history(self):
        chain = MessageChain([Plain('有效结论'), Image(file='https://example.invalid/final.png'),
                              Plain('另一项有效信息'), At(qq='300'), Reply(id='original-message')])
        runner, event, _ = await self.make([response('先看看网页', ['read']), response('', chain=chain)])
        self.rewrite = '审过的结论和另一项有效信息'
        await self.drive(runner, event)
        self.assertIn('有效结论\n另一项有效信息', self.reviews[0][0])
        self.assertEqual(self.sent_texts(), [self.rewrite])
        self.assertEqual([s['type'] for s in self.sends[0]['message']], ['text', 'image', 'at', 'reply'])
        self.assertEqual(runner.get_final_llm_resp().completion_text, self.rewrite)
        stage = object.__new__(InternalAgentSubStage)
        stage.conv_manager = SimpleNamespace(update_conversation=AsyncMock())
        runner.req.conversation = SimpleNamespace(cid='offline-history', token_usage=0)
        await stage._save_to_history(event, runner.req, runner.get_final_llm_resp(),
                                     runner.run_context.messages, runner.stats)
        saved = stage.conv_manager.update_conversation.call_args.kwargs['history']
        self.assertEqual(saved[-1]['content'], [{'type': 'text', 'text': self.rewrite}])
        self.assertEqual(len(self.reviews), 1, 'final guard must consume exact merged-text approval')

    async def test_tool_error_is_preserved_and_final_failure_is_reviewed(self):
        effects = []
        async def broken(event): effects.append('started'); raise ValueError('fixture-tool-failure')
        runner, event, _ = await self.make([
            response('本龙先查一下', ['broken']), response('这次没读到网页，工具出错了')], {'broken': broken})
        await self.drive(runner, event)
        self.assertEqual(effects, ['started'])
        self.assertIn('fixture-tool-failure', next(m.content for m in runner.run_context.messages if m.role == 'tool'))
        self.assertIn('工具出错了', self.sent_texts()[0])
        self.assertEqual(len(self.reviews), 1)

    async def test_provider_error_discards_only_pending_plan_and_error_still_has_outbound_review(self):
        runner, event, _ = await self.make([
            response('本龙先查一下', ['read']), response('fixture unavailable', role='err')])
        await self.drive(runner, event)
        self.assertEqual(len(self.reviews), 1)
        self.assertNotIn('本龙先查一下', self.sent_texts()[0])
        self.assertIn('fixture unavailable', self.sent_texts()[0])
        self.assertFalse(hasattr(runner.run_context, '_qqbot_tool_prelude'))
        self.assertFalse(event.get_extra('qqbot_agent_final_done'))

    async def test_cancel_running_tool_cleans_pending_and_does_not_retry_side_effect(self):
        started, closed = asyncio.Event(), asyncio.Event()
        effects = []
        async def slow(event):
            effects.append('once')
            started.set()
            try: await asyncio.Event().wait()
            finally: closed.set()
        runner, event, _ = await self.make([response('本龙先查一下', ['slow'])], {'slow': slow})
        task = asyncio.create_task(self.drive(runner, event))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        await asyncio.wait_for(closed.wait(), 2)
        self.assertEqual(effects, ['once'])
        self.assertEqual(self.sends, [])
        self.assertFalse(hasattr(runner.run_context, '_qqbot_tool_prelude'))

    async def test_explicit_stop_does_not_flush_plan_as_a_completed_answer(self):
        started = asyncio.Event()
        async def slow(event): started.set(); await asyncio.Event().wait()
        runner, event, _ = await self.make([response('本龙先查一下', ['slow'])], {'slow': slow})
        task = asyncio.create_task(self.drive(runner, event))
        await asyncio.wait_for(started.wait(), 2)
        runner.request_stop()
        await asyncio.wait_for(task, 2)
        self.assertTrue(runner.was_aborted())
        self.assertEqual(self.sends, [])
        self.assertFalse(hasattr(runner.run_context, '_qqbot_tool_prelude'))
        self.assertFalse(event.get_extra('qqbot_agent_final_done'))

    async def test_final_review_failure_does_not_send_buffer_or_draft(self):
        runner, event, _ = await self.make([response('先看看网页', ['read']), response('有效结论')])
        self.reject = True
        await self.drive(runner, event)
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.sends, [])
        self.assertEqual(runner.get_final_llm_resp().completion_text, '')
        self.assertFalse(event.get_extra('qqbot_agent_final_done'))

    async def test_cancel_final_review_stops_event_clears_draft_and_pending(self):
        runner, event, _ = await self.make([response('先看看网页', ['read']), response('有效结论')])
        started = asyncio.Event()
        async def blocked_review(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.plugin._reviewer.review = blocked_review
        task = asyncio.create_task(self.drive(runner, event))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(event.is_stopped())
        self.assertEqual(self.sends, [])
        self.assertEqual(runner.get_final_llm_resp().completion_text, '')
        self.assertFalse(hasattr(runner.run_context, '_qqbot_tool_prelude'))

    async def test_edit_after_final_review_forces_outbound_review(self):
        runner, event, _ = await self.make([response('先看看网页', ['read']), response('有效结论')])
        await self.drive(runner, event, mutate=lambda chain: chain.chain.append(Plain('后来插件添加的文字')))
        self.assertEqual(len(self.reviews), 2)
        self.assertIn('后来插件添加的文字', self.reviews[-1][0])

    async def test_policy_change_after_final_review_forces_outbound_review(self):
        runner, event, _ = await self.make([response('先看看网页', ['read']), response('有效结论')])
        self.after_final = lambda *args: self.plugin.config.update(review_policy='speed_first')
        await self.drive(runner, event)
        self.assertEqual(len(self.reviews), 2)

    async def test_concurrent_sessions_do_not_merge_each_others_plans(self):
        first, e1, _ = await self.make([response('先看看网页', ['read']), response('甲的结论')], target='101')
        second, e2, _ = await self.make([response('再看看评论', ['read']), response('乙的结论')], target='102')
        await asyncio.gather(self.drive(first, e1), self.drive(second, e2))
        texts = dict(zip([p['group_id'] for p in self.sends], self.sent_texts()))
        self.assertEqual(texts['101'], '先看看网页\n甲的结论')
        self.assertEqual(texts['102'], '再看看评论\n乙的结论')

    async def test_streaming_retains_existing_chunks_and_outbound_review(self):
        runner, event, _ = await self.make([response('本龙先查一下', ['read']), response('有效结论')], streaming=True)
        await self.drive(runner, event, send_stream=True)
        self.assertEqual(self.sent_texts(), ['本龙先查一下', '有效结论'])
        # Streaming already reviews both outbound chunks and the final hook.
        # This compatibility adapter deliberately leaves that separate path alone.
        self.assertEqual(len(self.reviews), 3)
        self.assertFalse(any(row.args[1]['status'] == 'deferred' for row in self.plugin._audit.call_args_list
                             if row.args[0] == 'tool_prelude'))

    async def test_skills_like_requery_keeps_original_order(self):
        runner, event, _ = await self.make([
            response('本龙先查一下', ['read']), response('', ['read']), response('有效结论')], mode='skills_like')
        await self.drive(runner, event)
        self.assertEqual(self.sent_texts(), ['本龙先查一下', '有效结论'])
        self.assertEqual(len(self.reviews), 2)

    async def test_existing_framework_buffering_no_longer_invalidates_final_receipt(self):
        runner, event, _ = await self.make([response('本龙先查一下', ['read']), response('有效结论')])
        await self.drive(runner, event, buffering=True)
        self.assertEqual(len(self.sends), 1)
        self.assertEqual(len(self.reviews), 1)

    async def test_tool_status_option_remains_reviewed_and_does_not_consume_final_receipt(self):
        runner, event, _ = await self.make([response('本龙先查一下', ['read']), response('有效结论')])
        await self.drive(runner, event, show_tools=True)
        self.assertEqual(len(self.sends), 2)
        self.assertIn('调用工具', self.sent_texts()[0])
        self.assertEqual(self.sent_texts()[1], '本龙先查一下\n有效结论')
        self.assertEqual(len(self.reviews), 2)

    async def test_plain_final_without_tools_is_not_delayed_or_changed(self):
        runner, event, _ = await self.make([response('本龙先查一下')])
        await self.drive(runner, event)
        self.assertEqual(self.sent_texts(), ['本龙先查一下'])
        self.assertEqual(len(self.reviews), 1)

    async def test_final_media_only_does_not_create_placeholder_text(self):
        runner, event, _ = await self.make([response('本龙先查一下', ['read']),
            response('', chain=MessageChain([Image(file='https://example.invalid/final.png')]))])
        await self.drive(runner, event)
        self.assertEqual(self.sent_texts(), [''])
        self.assertEqual([s['type'] for s in self.sends[0]['message'] if s['type'] != 'text'], ['image'])
        self.assertFalse(any(text for text, _ in self.reviews))

    async def test_buffer_is_bounded_and_overflow_keeps_existing_send_path(self):
        runner, event, _ = await self.make([
            *[response('本龙先查一下', ['read'], ids=[f'round-{i}']) for i in range(MAX_PRELUDES + 1)],
            response('完整结论')])
        await self.drive(runner, event)
        self.assertEqual(self.sent_texts(), ['本龙先查一下', '本龙先查一下\n完整结论'])
        self.assertEqual(len(self.reviews), 2)
        self.assertEqual(len([m for m in runner.run_context.messages if m.role == 'tool']), MAX_PRELUDES + 1)

    async def test_generator_close_before_tool_execution_drops_only_unsent_plan(self):
        runner, event, _ = await self.make([response('本龙先查一下', ['read'])])
        iterator = runner.step()
        async for item in iterator:
            if item.type == 'tool_call': break
        await iterator.aclose()
        self.assertFalse(hasattr(runner.run_context, '_qqbot_tool_prelude'))
        self.assertFalse(any(row[0] == 'tool' for row in self.timeline))
        self.assertEqual(self.sends, [])

    async def test_multiple_plain_prelude_parts_not_misreported_as_delivered_history(self):
        runner, event, _ = await self.make([
            response('', ['read'], chain=MessageChain([Plain('先看看网页'), Plain('再查一下来源')])),
            response('完整结论')])
        await self.drive(runner, event)
        self.assertEqual(self.sent_texts(), ['先看看网页\n再查一下来源\n完整结论'])
        self.assertEqual(self.reviews[0][1]['prior_replies'], [])

    async def test_other_platform_is_not_intercepted(self):
        runner, event, _ = await self.make([response('先看看网页', ['read']), response('完整结论')], platform='other')
        await self.drive(runner, event)
        self.assertEqual(self.sent_texts(), ['先看看网页', '完整结论'])

    async def test_attachment_policy_sees_reviewed_text_removes_only_owned_image_before_send(self):
        self.rewrite = '审后换成了道歉语气'
        original = [{'type': 'text', 'data': {'text': '原先攻击性的文案'}},
                    {'type': 'image', 'data': {'file': 'owned-emoji'}},
                    {'type': 'image', 'data': {'file': 'requested-image'}},
                    {'type': 'at', 'data': {'qq': '300'}},
                    {'type': 'reply', 'data': {'id': 'incoming'}}]
        observed = []
        def check(action, params):
            self.assertEqual(len(self.sends), 0)
            observed.append((action, params['message'][0]['data']['text']))
            self.timeline.append(('attachment', action))
            params['message'][:] = [part for part in params['message']
                                   if part != {'type': 'image', 'data': {'file': 'owned-emoji'}}]
        bind_request('aiocqhttp:GroupMessage:100', ['test'], False)
        with attachment_policy(check):
            await self.bot.send_group_msg(group_id='100', message=original)
        self.assertEqual(observed, [('send_group_msg', self.rewrite)])
        self.assertEqual([row[0] for row in self.timeline], ['review', 'attachment', 'sent'])
        self.assertEqual([part['type'] for part in self.sends[0]['message']], ['text', 'image', 'at', 'reply'])
        self.assertEqual(self.sends[0]['message'][1]['data']['file'], 'requested-image')
        self.assertEqual(original[1]['data']['file'], 'owned-emoji', 'caller input must stay untouched')
        self.assertEqual(sent_text('aiocqhttp:GroupMessage:100', '原先攻击性的文案'), self.rewrite)
        self.assertTrue(confirmed_sent('aiocqhttp:GroupMessage:100', self.rewrite))

    async def test_attachment_policy_nested_and_concurrent_scopes_are_isolated(self):
        seen = []
        def callback(label):
            return lambda action, params: seen.append((label, params['group_id']))
        with attachment_policy(callback('outer')):
            await self.bot.send_group_msg(group_id='1', message='A')
            with attachment_policy(callback('inner')):
                await self.bot.send_group_msg(group_id='2', message='B')
            await self.bot.send_group_msg(group_id='3', message='C')
        async def send(target):
            with attachment_policy(callback(target)):
                await asyncio.sleep(0)
                await self.bot.send_group_msg(group_id=target, message='D')
        await asyncio.gather(send('4'), send('5'))
        await self.bot.send_group_msg(group_id='6', message='E')
        self.assertEqual(seen[:3], [('outer', '1'), ('inner', '2'), ('outer', '3')])
        self.assertCountEqual(seen[3:], [('4', '4'), ('5', '5')])

    async def test_attachment_check_failure_never_sends_or_records_success(self):
        bind_request('aiocqhttp:GroupMessage:100', ['test'], False)
        def broken(action, params): raise ValueError('attachment fixture rejected')
        with attachment_policy(broken):
            with self.assertRaisesRegex(ValueError, 'attachment fixture'):
                await self.bot.send_group_msg(group_id='100', message='已审文字')
        self.assertEqual(self.sends, [])
        self.assertFalse(confirmed_sent('aiocqhttp:GroupMessage:100', '已审文字'))
        await self.bot.send_group_msg(group_id='100', message='已审文字')
        self.assertEqual(len(self.sends), 1, 'callback scope must reset after an exception')

    async def test_attachment_check_not_called_when_review_fails(self):
        self.reject = True
        check = Mock()
        with attachment_policy(check):
            with self.assertRaises(ReviewUnavailable):
                await self.bot.send_group_msg(group_id='100', message='被拒绝的原稿')
        check.assert_not_called()
        self.assertEqual(self.sends, [])

    async def test_attachment_check_runs_before_failed_transport_without_success_receipt(self):
        bind_request('aiocqhttp:GroupMessage:100', ['test'], False)
        check = Mock()
        self.bot._api.call_action = AsyncMock(side_effect=OSError('transport offline'))
        with attachment_policy(check):
            with self.assertRaises(OSError):
                await self.bot.send_group_msg(group_id='100', message='已审文字')
        check.assert_called_once()
        self.assertFalse(confirmed_sent('aiocqhttp:GroupMessage:100', '已审文字'))

    async def test_attachment_policy_is_only_applied_to_this_plugins_platform_bot(self):
        other = object.__new__(CQHttp)
        other._api = SimpleNamespace(call_action=AsyncMock(return_value={'message_id': 'other'}))
        check = Mock()
        with attachment_policy(check):
            await other.send_group_msg(group_id='100', message='另一平台的内容')
        check.assert_not_called()

    async def test_restore_is_identity_safe_and_preserves_other_wrappers(self):
        added_step = ToolLoopAgentRunner.step
        async def newer(runner):
            async for item in added_step(runner): yield item
        with patch.object(ToolLoopAgentRunner, 'step', newer):
            self.restore()
            self.assertIs(ToolLoopAgentRunner.step, newer)
        ToolLoopAgentRunner.step = self.original_step
        self.assertIs(MainAgentHooks.on_agent_done, self.original_done)


if __name__ == '__main__':
    unittest.main()
