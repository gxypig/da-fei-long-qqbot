"""Defer only plain, prospective tool preambles until the final review.

AstrBot 4.29's full-schema, non-streaming runner yields an ``llm_result``
before executing tools.  Only the final response invokes MainAgentHooks.
This adapter suppresses that *yield*, not the model response or tool executor:
the model's tool messages, arguments, permissions and results remain intact.
Pending text is merged before final hooks, so the final review receipt covers
the exact outgoing text.  Streaming, skills-like re-query, rich intermediate
chains and factual/permission-bearing text retain the original path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
import re


_STATE = '_qqbot_tool_prelude'
MAX_PRELUDES = 8
MAX_CHARACTERS = 1200

# Intentionally a small grammar, not a general-purpose semantic classifier.
# Uncertain text is sent through the existing mandatory outbound reviewer.
_ACK = re.compile(r'^(?:嗯[～~]?|好(?:的|呀|嘞)?|行|收到|稍等(?:一下)?|等(?:我|本龙)(?:一下|会儿)?)$')
_PLAN = re.compile(
    r'^(?:我|本龙|咱|让我|让本龙)?'
    r'(?:(?:先|再|这就|马上|现在|接着|然后|准备|打算|来|去|帮你|给你)){0,4}'
    r'(?:查(?:找|询|一查|一下|查)?|搜(?:索|一搜|一下|搜)?|找(?:找|一下)?|'
    r'看(?:看|一眼|一下)?|读(?:一读|一下)?|读取|打开|点(?:开|进去)|浏览|核实|确认一下)'
    r'(?:(?:一下|一眼|看看|下|这个|这条|这篇|这段|那个|那条|那篇|那段|相关的?|你发的|你说的|'
    r'刚发的|刚刚发的|刚才发的|刚贴的|刚发过来的|'
    r'网页|网站|页面|链接|详情|原文|内容|视频|评论|资料|来源|出处|结果|乐子|'
    r'再回你|再告诉你|给你看看|哈|嗷|哦|吧|呀)){0,12}$'
)
_EN_PLAN = re.compile(
    r"^(?:i(?:'ll| will)|let me) (?:first )?(?:check|read|open|search|look up|browse)"
    r"(?: (?:this|that|the|web|page|website|link|details|source|video|comments|it|now|first))*$",
    re.I,
)
_CAUTION = re.compile(
    r'^(?:不能|不想|免得|省得)(?:光凭|只凭|仅凭|凭|靠)?'
    r'(?:标题|简介|安利文案|文案|封面|截图)?'
    r'(?:瞎认|乱认|瞎说|乱说|瞎猜|乱猜|下结论)$'
)


def is_plan_only(text: str) -> bool:
    """Only short acknowledgements plus planned read/search actions qualify."""
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 240:
        return False
    # URLs, identifiers, quoted claims and questions can contain real results or
    # consent requests.  Never hold those based on a keyword like "先看看".
    if re.search(r'[0-9?？:："“”‘’`<>\[\]【】]', text):
        return False
    clauses = [part.strip() for part in re.split(r'[，,。！!；;\n…]+', text.strip(' .')) if part.strip()]
    if not 1 <= len(clauses) <= 4:
        return False
    planned = False
    for clause in clauses:
        if _PLAN.fullmatch(clause) or _EN_PLAN.fullmatch(clause):
            planned = True
        elif not (_ACK.fullmatch(clause) or _CAUTION.fullmatch(clause)):
            return False
    return planned


@dataclass(eq=False)
class _Pending:
    runner: object
    context: object
    request: object
    event: object
    prior_ids: set[int]
    texts: list[str] = field(default_factory=list)
    merged: bool = False


def has_deferred_prelude(run_context) -> bool:
    state = getattr(run_context, _STATE, None)
    return isinstance(state, _Pending) and bool(state.texts)


def deferred_tool_message(run_context, message) -> bool:
    """Unsent planning text is not a previous *delivered* assistant reply."""
    state = getattr(run_context, _STATE, None)
    if not isinstance(state, _Pending) or id(message) in state.prior_ids:
        return False
    if not getattr(message, 'tool_calls', None):
        return False
    content = getattr(message, 'content', None)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        from astrbot.core.agent.message import TextPart
        text = '\n'.join(p.text for p in content if isinstance(p, TextPart))
    else:
        return False
    return any(' '.join(text.split()) == ' '.join(value.split()) for value in state.texts)


def install_tool_prelude(plugin):
    """Install reversible, per-run adapters; no framework files/config changes."""
    from astrbot.core.agent.runners.base import AgentState
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.astr_agent_hooks import MainAgentHooks
    from astrbot.core.message.components import Plain

    previous_step = ToolLoopAgentRunner.step
    previous_done = MainAgentHooks.on_agent_done
    enabled = True

    def audit(status, **details):
        plugin._audit('tool_prelude', {'status': status, **details})

    def release(state, status):
        if getattr(state.context, _STATE, None) is state:
            delattr(state.context, _STATE)
        if state.texts and not state.merged:
            audit(status, count=len(state.texts))
        state.texts.clear()

    def eligible(runner, event):
        return (enabled and plugin._cfg('latency_optimization_enabled', False)
                and plugin._cfg('reply_review_enabled', True)
                and not runner.streaming and runner.tool_schema_mode == 'full'
                and isinstance(runner.agent_hooks, MainAgentHooks)
                and event is not None
                and event.get_platform_id() == plugin._cfg('platform_id', 'aiocqhttp'))

    @wraps(previous_step)
    async def step(runner):
        context = runner.run_context
        event = getattr(context.context, 'event', None)
        if event is not None and runner.state == AgentState.IDLE:
            event.set_extra('qqbot_agent_final_done', False)
        state = getattr(context, _STATE, None)
        if isinstance(state, _Pending) and (state.runner is not runner or state.request is not runner.req):
            release(state, 'request_changed')
            state = None
        if not isinstance(state, _Pending) and eligible(runner, event):
            state = _Pending(runner, context, runner.req, event,
                             {id(message) for message in context.messages})
            setattr(context, _STATE, state)
        iterator = previous_step(runner)
        finished = False
        try:
            async for response in iterator:
                if event is not None and response.type in {'aborted', 'err'}:
                    event.set_extra('qqbot_agent_final_done', False)
                if isinstance(state, _Pending) and response.type in {'aborted', 'err'}:
                    # run_agent returns immediately on `aborted` without waiting
                    # for this generator to be finalized; release before yielding.
                    release(state, 'aborted' if response.type == 'aborted' else 'error')
                    state = None
                if (isinstance(state, _Pending) and eligible(runner, event)
                        and not state.merged and runner.state == AgentState.RUNNING
                        and response.type == 'llm_result' and not event.is_stopped()):
                    chain = response.data.get('chain')
                    # In full/non-streaming mode a final result is DONE before
                    # this yield; only a tool-calling response is still RUNNING.
                    # Never split a text+media chain or intercept tool_direct_result.
                    if (chain is not None and chain.type is None and chain.chain
                            and all(isinstance(part, Plain) for part in chain.chain)):
                        text = '\n'.join(part.text for part in chain.chain if part.text)
                        if (is_plan_only(text) and len(state.texts) < MAX_PRELUDES
                                and sum(map(len, state.texts)) + len(text) <= MAX_CHARACTERS):
                            state.texts.append(text)
                            audit('deferred', count=len(state.texts), characters=len(text))
                            continue
                yield response
            finished = True
        finally:
            # Closing/cancelling must close the original generator too, including
            # its running tool executor.  Never retry a tool after partial effects.
            try:
                await iterator.aclose()
            finally:
                if isinstance(state, _Pending) and (not finished or runner.done() or event.is_stopped()):
                    release(state, 'terminal_without_text' if finished else 'interrupted')

    @wraps(previous_done)
    async def done(hooks, run_context, response):
        state = getattr(run_context, _STATE, None)
        if isinstance(state, _Pending) and not state.merged and state.texts:
            event = state.event
            cancelled = (event.is_stopped() or state.runner.was_aborted()
                         or state.runner._is_stop_requested())
            if (enabled and not cancelled and response is not None
                    and response.role == 'assistant' and not response.tools_call_name):
                chain = response.result_chain
                plains = [part for part in chain.chain if isinstance(part, Plain)] if chain else []
                final = '\n'.join(part.text for part in plains if part.text) if plains else response.completion_text or ''
                # A tool that sends directly (or produces only media) must not
                # acquire a bogus "final answer" consisting solely of a plan.
                if final.strip():
                    pending = list(dict.fromkeys(state.texts))
                    merged = '\n'.join([*pending, final])
                    response.completion_text = merged
                    # Real LLMResponse's setter rewrites Plain components while
                    # preserving media; also support compatible response objects.
                    if chain:
                        current = [part for part in chain.chain if isinstance(part, Plain)]
                        if current:
                            for index, part in enumerate(current):
                                part.text = merged if index == 0 else ''
                        else:
                            chain.chain.insert(0, Plain(merged))
                    state.merged = True
                    audit('merged_before_review', count=len(pending))
        # All existing on_llm_response/on_agent_done handlers, including the
        # reviewer and its exact-text receipt, run after the final assembly.
        return await previous_done(hooks, run_context, response)

    ToolLoopAgentRunner.step = step
    MainAgentHooks.on_agent_done = done

    def restore():
        nonlocal enabled
        enabled = False
        if MainAgentHooks.on_agent_done is done:
            MainAgentHooks.on_agent_done = previous_done
        if ToolLoopAgentRunner.step is step:
            ToolLoopAgentRunner.step = previous_step

    return restore
