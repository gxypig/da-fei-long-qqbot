"""Model admission and receipt-based reply timing, without owning business tasks.

Install LAST (even when disabled), restore FIRST, then await
``plugin._work.close()``. Set event extra ``qqbot_final_text`` to the reviewed
body and call ``mark(event, 'final_ready')`` when it is ready for transport.
Other marks are diagnostic stages, never delivery receipts. ``model_work`` is a
synchronous, task-local context manager; mandatory review defaults to interactive.

The pure ModelScheduler accepts operation/iterator factories for offline tests.
Only those factories may do model I/O: never put tools or sends inside them.
BackgroundPaused means the caller should requeue its model work, not replay an
already committed tool. The original session/group generators run in their caller
task, including across yield and history persistence.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import re
import time
import uuid
from collections import deque
from contextlib import contextmanager,asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from urllib.parse import urlsplit, urlunsplit


KINDS = ('interactive', 'review', 'scheduled', 'auxiliary', 'background')
TAIL_GUARD_MARKER = '[QQBOT_CASUAL_TAIL_GUARD_V1]'
_kind = ContextVar('qqbot_model_work_kind', default=None)
_lease = ContextVar('qqbot_model_work_lease', default=None)
_reply = ContextVar('qqbot_reply_work', default=None)
_prediction_turn = ContextVar('qqbot_prediction_admission', default=None)
_provider_observation = ContextVar('qqbot_provider_observation', default=None)
_review_verdict_observer = ContextVar('qqbot_review_verdict_observer', default=None)
_DONE = object()
_HEDGE_SKIPPED = object()
_STATE_KEY = '_qqbot_work_state'
_SEND_ACTIONS = {'send_group_msg', 'send_private_msg', 'send_msg'}


class BackgroundPaused(Exception):
    """An uncommitted background model request yielded to foreground work."""


class ModelNotReady(BackgroundPaused, RuntimeError):
    """Startup/configuration has not exposed a usable provider yet."""


class HedgePreempted(Exception):
    """Only the duplicate model worker yielded its slot to actual interactive work."""


class WorkClosed(RuntimeError):
    """The runtime is shutting down; do not submit or retry model work here."""


class InvalidModelResponse(RuntimeError):
    """A hedged candidate did not contain a complete usable model response."""


@contextmanager
def model_work(kind):
    if kind not in KINDS:
        raise ValueError(f'Unknown model work kind: {kind!r}')
    token = _kind.set(kind)
    try:
        yield
    finally:
        _kind.reset(token)


@contextmanager
def review_verdict_observer(callback):
    """Notify once per review attempt as soon as raw model output arrives."""
    seen=False
    def arrived():
        nonlocal seen
        if not seen:
            seen=True
            if callback is not None:callback()
    token=_review_verdict_observer.set(arrived)
    try:yield arrived
    finally:_review_verdict_observer.reset(token)


def _config_get(cfg, name, default):
    return cfg(name, default) if callable(cfg) else cfg.get(name, default)


def provider_key(provider):
    """Group independent clients/facades by source, never by object address/key.

    Source IDs survive config/model overrides. Without one, equivalent public
    configuration identities share a bucket. Unknown configurations deliberately
    share a conservative fallback bucket. Credentials never enter the identity.
    """
    config = getattr(provider, 'provider_config', {}) or {}
    source = config.get('provider_source_id') or getattr(provider, 'provider_source_id', None)
    if source is not None and str(source):
        identity = ('source', str(source))
    else:
        base = str(config.get('api_base') or '').rstrip('/')
        try:
            url = urlsplit(base)
            # Strip URL credentials/query as well as never reading config keys.
            host = url.hostname or ''
            if ':' in host:
                host = f'[{host}]'
            netloc = host + (f':{url.port}' if url.port else '')
            base = urlunsplit((url.scheme.lower(), netloc, url.path.rstrip('/'), '', ''))
        except ValueError:
            base = ''
        identity = ('config', base, str(config.get('model') or ''), str(config.get('id') or ''))
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
    return f'{identity[0]}:{digest}'


def _copy_fields(value, **updates):
    if isinstance(value, dict):
        return {**value, **updates}
    model_copy = getattr(value, 'model_copy', None)
    if callable(model_copy):
        return model_copy(update=updates)
    copied = copy.copy(value)
    for key, item in updates.items():
        setattr(copied, key, item)
    return copied


def _strip_system_content(content):
    """Copy only marked system text; retain typed parts and their metadata."""
    if isinstance(content, str):
        return content.replace(TAIL_GUARD_MARKER, ''), TAIL_GUARD_MARKER in content
    if not isinstance(content, (list, tuple)):
        return content, False
    result, changed = [], False
    for part in content:
        body = _field(part, 'text')
        if (_field(part, 'type') in {'text', 'input_text', 'output_text'}
                and isinstance(body, str) and TAIL_GUARD_MARKER in body):
            part = _copy_fields(part, text=body.replace(TAIL_GUARD_MARKER, ''))
            changed = True
        result.append(part)
    return (tuple(result) if isinstance(content, tuple) else result, True) if changed else (content, False)


def _strip_context_guard(contexts):
    # The real ToolLoopAgentRunner embeds the system prompt in Message objects.
    # Never consume arbitrary iterators, mutate history, or interpret a marker
    # in user/tool/assistant content as trusted admission to the optional race.
    if not isinstance(contexts, (list, tuple)):
        return contexts, False
    result, changed = [], False
    for message in contexts:
        if _field(message, 'role') in {'system', 'developer'}:
            content, marked = _strip_system_content(_field(message, 'content'))
            if marked:
                message = _copy_fields(message, content=content)
                changed = True
        result.append(message)
    return (tuple(result) if isinstance(contexts, tuple) else result, True) if changed else (contexts, False)


def _strip_tail_guard(signature, provider, args, kwargs):
    """Remove our local marker without changing other arguments or defaults.

    Bind the saved original signature, including positional system_prompt and
    wrappers exposing **kwargs. Unmarked calls keep their original call shape.
    """
    bound = signature.bind(provider, *args, **kwargs)
    extras = next((name for name, param in signature.parameters.items()
                   if param.kind == inspect.Parameter.VAR_KEYWORD), None)
    values = dict(bound.arguments.get(extras, {})) if extras else {}
    values.update({name: value for name, value in bound.arguments.items()
                   if name != extras and name != next(iter(signature.parameters))})
    replacements = {}
    system, marked = _strip_system_content(values.get('system_prompt'))
    if marked:
        replacements['system_prompt'] = system
    contexts, marked = _strip_context_guard(values.get('contexts'))
    if marked:
        replacements['contexts'] = contexts
    if not replacements:
        return args, kwargs, None
    for key, value in replacements.items():
        if key in bound.arguments:
            bound.arguments[key] = value
        else:
            bound.arguments[extras] = {**bound.arguments[extras], key: value}
        values[key] = value
    return bound.args[1:], bound.kwargs, values


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _request_metrics(signature, provider, args, kwargs):
    """Count the bound provider input, not an estimated/serialized wire payload.

    Read text content only: roles, tool schemas/arguments, URLs and identifiers
    are neither serialized nor counted as text. Unknown iterables stay unconsumed
    and produce null counts. The Runner normally supplies typed contexts rather
    than prompt/system_prompt. Defaults are applied only to this diagnostic copy.
    """
    bound = signature.bind(provider, *args, **kwargs)
    bound.apply_defaults()
    values = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            values.update(bound.arguments.get(name, {}))
        elif name != next(iter(signature.parameters)):
            values[name] = bound.arguments.get(name)
    media = dict.fromkeys(('image_count', 'audio_count', 'video_count', 'file_count', 'other_part_count'), 0)
    complete = True

    def count_sequence(value):
        nonlocal complete
        if value is None:
            return 0
        if isinstance(value, (list, tuple)):
            return len(value)
        complete = False
        return None

    def content_chars(content):
        nonlocal complete
        if content is None:
            return 0
        if isinstance(content, str):
            return len(content)
        if not isinstance(content, (list, tuple)):
            complete = False
            return None
        chars = 0
        for part in content:
            kind = _field(part, 'type')
            if kind in ('text', 'input_text', 'output_text', 'think', 'reasoning'):
                text = _field(part, 'think' if kind == 'think' else 'text')
                if isinstance(text, str):
                    chars += len(text)
                else:
                    complete = False
            else:
                key = {'image_url': 'image_count', 'input_image': 'image_count', 'image': 'image_count',
                       'audio_url': 'audio_count', 'input_audio': 'audio_count', 'audio': 'audio_count',
                       'video_url': 'video_count', 'input_video': 'video_count', 'video': 'video_count',
                       'file': 'file_count', 'input_file': 'file_count'}.get(kind, 'other_part_count')
                media[key] += 1
        return chars

    contexts = values.get('contexts')
    context_count = count_sequence(contexts)
    context_sizes = [content_chars(_field(message, 'content')) for message in contexts] if context_count is not None and contexts else []
    context_chars = (sum(context_sizes) if context_count is not None and all(n is not None for n in context_sizes) else None)
    sizes = {name + '_chars': content_chars(values.get(name)) for name in ('prompt', 'system_prompt', 'extra_user_content_parts')}
    for name, key in (('image_urls', 'image_count'), ('audio_urls', 'audio_count')):
        count = count_sequence(values.get(name))
        media[key] = media[key] + count if count is not None else None
    tool = values.get('func_tool')
    tools = tool if isinstance(tool, (list, tuple)) else _field(tool, 'tools')
    tools_count = count_sequence(tools) if tools is not None or tool is None else None
    selected = values.get('model')
    if not selected:
        get_model = getattr(provider, 'get_model', None)
        selected = get_model() if callable(get_model) else getattr(provider, 'model_name', None)
    if not selected:
        selected = _field(getattr(provider, 'provider_config', None), 'model')
    return {'context_count': context_count, 'context_chars': context_chars, **sizes, **media,
            'tools_count': tools_count, 'model': selected if isinstance(selected, str) else None,
            'request_shape_complete': complete and tools_count is not None}


def _usage_metrics(response):
    """Provider-reported numeric counts only; missing usage is not zero usage."""
    result = dict.fromkeys(('input_tokens', 'cached_input_tokens', 'output_tokens', 'total_tokens'))
    usage = _field(response, 'usage')
    if usage is None:
        return result
    for target, source in (('input_tokens', 'input'), ('cached_input_tokens', 'input_cached'),
                           ('output_tokens', 'output'), ('total_tokens', 'total')):
        value = _field(usage, source)
        if type(value) is int and value >= 0:
            result[target] = value
    return result


def _has_current_tools(contexts):
    """Ignore past turns, but never duplicate a continuation of current tools."""
    if contexts is None:
        return False
    if not isinstance(contexts, (list, tuple)):
        return True  # Do not consume arbitrary iterators or guess their history.
    for message in reversed(contexts):
        role = _field(message, 'role')
        if role == 'user':
            return False
        if role == 'tool' or (role == 'assistant' and _field(message, 'tool_calls')):
            return True
    return False


def _has_current_media(values):
    def media(content):
        if content is None or isinstance(content, str):
            return False
        if not isinstance(content, (list, tuple)):
            return True
        return any(_field(part, 'type') not in {'text', 'input_text', 'output_text', 'think', 'reasoning'}
                   for part in content)

    if media(values.get('extra_user_content_parts')):
        return True
    contexts = values.get('contexts')
    if contexts is None:
        return False
    if not isinstance(contexts, (list, tuple)):
        return True
    for message in reversed(contexts):
        if media(_field(message, 'content')):
            return True
        if _field(message, 'role') == 'user':
            break
    return False


def _valid_model_response(response, response_type):
    """Keep the returned object intact, especially role='tool' and image chains."""
    if not isinstance(response, response_type) or getattr(response, 'is_chunk', False):
        return False
    if response.role not in {'assistant', 'tool'}:
        return False
    names = getattr(response, 'tools_call_name', None)
    if names:
        args = getattr(response, 'tools_call_args', None)
        ids = getattr(response, 'tools_call_ids', None)
        return (isinstance(names, (list, tuple)) and isinstance(args, (list, tuple))
                and isinstance(ids, (list, tuple)) and len(names) == len(args) == len(ids)
                and all(isinstance(name, str) and name.strip() for name in names)
                and all(isinstance(arg, dict) for arg in args)
                and all(isinstance(identifier, str) and identifier for identifier in ids))
    if response.role != 'assistant':
        return False
    body = getattr(response, 'completion_text', None)
    if isinstance(body, str) and body.strip():
        return True
    # Image compatibility may produce an image-only response. Do not require
    # prose, synthesize a caption, or discard the original image/tool metadata.
    chain = getattr(getattr(response, 'result_chain', None), 'chain', ()) or ()
    return any(type(part).__name__ in {'Image', 'Record', 'Video', 'File'} for part in chain)


@dataclass
class _HedgeControl:
    primary_done: bool = False
    primary_succeeded: bool = False
    primary_failed: bool = False

    @property
    def redundant(self):
        # Once the primary has failed, its backup is the remaining answer path.
        return not self.primary_done or self.primary_succeeded


@dataclass(eq=False)
class _Job:
    key: str
    kind: str
    queued: float
    sequence: int
    ready: asyncio.Future
    trace_id: str | None = None
    trace: object = None
    after_primary: bool = False
    started: float | None = None
    worker: asyncio.Task | None = None
    paused: bool = False
    released: bool = False
    hedge: _HedgeControl | None = None
    preempted: bool = False
    status_code:int|None=None
    timed_out:bool=False
    rate_limit_reported:bool=False


class ModelScheduler:
    """Single-event-loop admission; no locks around sessions or tool execution.

    Each source gets model_concurrency slots (default 2), at most one auxiliary.
    Scheduled work jumps ahead of interactive work after 60 seconds waiting.
    Background needs five uninterrupted seconds without registered replies or
    any non-background model waiters/calls. All timings use a monotonic clock.
    """

    def __init__(self, cfg, audit=None, *, clock=time.monotonic,
                 idle_seconds=5.0, promotion_seconds=60.0):
        self.cfg, self.audit, self.clock = cfg, audit, clock
        self.idle_seconds, self.promotion_seconds = idle_seconds, promotion_seconds
        self._waiting, self._active = [], set()
        self._foreground = 0
        self._idle_since = clock()
        self._sequence = 0
        self._timer = None
        self._closed = False
        self._sources = set()
        self._last_lane={};self._since_scheduled={}
        from .adaptive_limits import AdaptiveLimits
        self.adaptive=AdaptiveLimits(clock)

    def _limit(self, key):
        value = _config_get(self.cfg, 'model_concurrency', 2)
        if isinstance(value, dict):
            value = value.get(key, value.get('default', 2))
        try:
            initial=max(1,min(3,int(value))) if not isinstance(value,bool) else 2
            return self.adaptive.limit(key,initial) if _config_get(self.cfg,'model_adaptive_concurrency',True) else initial
        except (TypeError, ValueError, OverflowError):
            return 2

    def busy(self):
        return not self._closed and bool(self._foreground or any(
            job.kind != 'background' for job in (*self._waiting, *self._active)))

    @contextmanager
    def foreground(self):
        """Register the whole reply BEFORE acquiring group/session locks."""
        if self._closed:
            raise WorkClosed('Model scheduler is closed')
        self._foreground += 1
        self._pump()
        try:
            yield
        finally:
            self._foreground -= 1
            self._pump()

    def _priority(self, job, now):
        aged=job.kind=='scheduled' and now-job.queued>=self.promotion_seconds
        allowed=aged and self._since_scheduled.get(job.key,0)>=9
        if allowed:return (-1,job.sequence)
        if job.kind in {'interactive','review'}:
            return (0 if job.kind!=self._last_lane.get(job.key) else 1,job.sequence)
        return (KINDS.index(job.kind)+1,job.sequence)

    def _pump(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._closed:
            return
        now = self.clock()
        for key in self._sources:
            self.adaptive.touch(key,sum(j.key==key for j in self._active),self._limit(key))
        if self.busy():
            self._idle_since = None
            for job in tuple(self._active):
                if job.kind == 'background' and not job.paused:
                    job.paused = True
                    # The worker contains model I/O only. Never cancel its caller.
                    if job.worker is not None and not job.worker.done():
                        job.worker.cancel()
        elif self._idle_since is None:
            self._idle_since = now
        idle = self._idle_since is not None and now - self._idle_since >= self.idle_seconds
        candidates=sorted(self._waiting,key=lambda item:self._priority(item,now))
        while candidates:
            job=candidates.pop(0)
            if job.ready.cancelled():
                self._waiting.remove(job)
                continue
            if job.after_primary and not job.trace.primary_admitted:
                if not job.trace.live:
                    # The parent stopped before submitting its model request.
                    # Release the orphan waiter; never start a stale prediction.
                    self._waiting.remove(job)
                    job.ready.cancel()
                continue
            if job.kind == 'background' and not idle:
                continue
            running = [item for item in self._active if item.key == job.key]
            necessary=any(w.key==job.key and w.kind in {'interactive','review'} and not w.ready.cancelled() for w in self._waiting)
            if job.kind=='scheduled':
                if any(item.kind=='scheduled' for item in self._active):continue
                if necessary and (now-job.queued<self.promotion_seconds or self._since_scheduled.get(job.key,0)<9):continue
            if job.kind=='auxiliary' and necessary:continue
            # Speculative idle learning must not occupy every upstream slot.
            # Keep all work queued; real replies retain the configured concurrency.
            if job.kind=='background' and any(item.kind=='background' for item in running):
                continue
            if len(running) >= self._limit(job.key):
                if job.kind in {'interactive','review','scheduled'}:
                    for duplicate in running:
                        if (duplicate.after_primary and duplicate.kind=='auxiliary'
                                and not duplicate.paused
                                and duplicate.worker is not None and not duplicate.worker.done()):
                            # Only the optional shared risk/image prediction is
                            # disposable. Its caller falls back to local image
                            # matching and the normal final review contract.
                            duplicate.paused=True
                            duplicate.worker.cancel()
                            break
                        if (duplicate.hedge is not None and duplicate.hedge.redundant
                                and not duplicate.preempted
                                and (duplicate.worker is None or not duplicate.worker.done())):
                            duplicate.preempted = True
                            if duplicate.worker is not None:
                                duplicate.worker.cancel()
                            # Hold the slot until worker cleanup actually finishes.
                            # Never cancel the caller, primary or tools.
                            break
                continue
            if job.kind == 'auxiliary' and any(item.kind == 'auxiliary' for item in running):
                continue
            self._waiting.remove(job)
            job.started = now
            self._active.add(job)
            if job.kind in {'interactive','review'}:
                self._last_lane[job.key]=job.kind
                self._since_scheduled[job.key]=min(9,self._since_scheduled.get(job.key,0)+1)
            elif job.kind=='scheduled':self._since_scheduled[job.key]=0
            if job.kind == 'interactive' and job.trace is not None:
                job.trace.primary_admitted = True
            job.ready.set_result(None)
            self.adaptive.touch(job.key,sum(j.key==job.key for j in self._active),self._limit(job.key))
            candidates=sorted(self._waiting,key=lambda item:self._priority(item,now))
        if (self._idle_since is not None and not idle
                and any(job.kind == 'background' for job in self._waiting)):
            self._timer = asyncio.get_running_loop().call_later(
                max(0, self.idle_seconds - (now - self._idle_since)), self._pump)

    def wake(self):
        """Re-evaluate admission (also useful with a manually advanced test clock)."""
        self._pump()

    def _reentrant(self, key):
        lease = _lease.get()
        return bool(lease and lease[0] is self and lease[1].key == key
                    and lease[1] in self._active and lease[1].worker is asyncio.current_task())

    async def _acquire(self, key, kind, *, hedge=None):
        if self._closed:
            raise WorkClosed('Model scheduler is closed')
        if kind not in KINDS:
            raise ValueError(f'Unknown model work kind: {kind!r}')
        self._sequence += 1
        job = _Job(key, kind, self.clock(), self._sequence, asyncio.get_running_loop().create_future())
        job.hedge = hedge
        scope = _reply.get()
        if scope and scope.trace.live and scope.trace.runtime.scheduler is self:
            job.trace_id = scope.trace.trace_id
            job.trace = scope.trace
        prediction = _prediction_turn.get()
        if (kind == 'auxiliary' and prediction is not None
                and prediction.runtime.scheduler is self):
            job.trace = prediction
            job.trace_id = prediction.trace_id
            job.after_primary = True
        self._sources.add(key)
        self._waiting.append(job)
        self._pump()
        try:
            await job.ready
            if self._closed:
                raise WorkClosed('Model scheduler is closed')
            if job.preempted:
                raise HedgePreempted('Interactive work needs this source slot')
            if job.paused:
                raise BackgroundPaused('Foreground work arrived')
            return job
        except BaseException:
            if job in self._waiting:
                self._waiting.remove(job)
            if job.ready.done() and not job.ready.cancelled():
                job.ready.exception()
            self._release(job)
            raise

    def _release(self, job):
        if job.released:
            return
        job.released = True
        self._active.discard(job)
        self._pump()

    def _cancel_reason(self, job):
        if self._closed:
            return WorkClosed('Model scheduler is closed')
        if job.preempted:
            return HedgePreempted('Interactive work needs this source slot')
        if job.paused:
            return BackgroundPaused('Foreground work arrived')
        return asyncio.CancelledError()

    def _report(self, job, outcome):
        if job.started is not None:
            from .wire_admission import _frame
            frame=_frame.get()
            if frame and frame.scheduler is self:
                from .chain_metrics import model_completed
                model_completed(self.clock()-job.started,(job.started-job.queued)*1000,job.kind)
            self.adaptive.finish(job.key,job.started-job.queued,self.clock()-job.started,
                status=job.status_code,timed_out=job.timed_out,
                necessary=job.kind in {'interactive','review','scheduled'} and outcome in {'ok','failed'},already_down=job.rate_limit_reported)
        if self.audit:
            try:
                fields = {
                    'provider': job.key, 'kind': job.kind, 'status': outcome,
                    'queue_ms': round(((job.started if job.started is not None else self.clock()) - job.queued) * 1000),
                    'elapsed_ms': round((self.clock() - job.queued) * 1000),
                }
                if job.trace_id:
                    fields['trace_id'] = job.trace_id
                self.audit('model_work', fields)
            except Exception:
                pass  # A diagnostic sink must never cause a retry of successful I/O.

    def _wire_error(self,job,exc):
        job.status_code=getattr(exc,'status_code',None) or getattr(getattr(exc,'response',None),'status_code',None)
        job.timed_out=isinstance(exc,TimeoutError) or type(exc).__name__=='APITimeoutError'
        if job.status_code==429 and not job.rate_limit_reported:
            self.adaptive.rate_limit(job.key);job.rate_limit_reported=True

    async def run(self, provider, operation, *, kind=None, hedge=None):
        """Await a model-only factory; preserve its result/exception and arguments."""
        key = provider_key(provider)
        if self._reentrant(key):
            return await operation()
        job = await self._acquire(key, kind or _kind.get() or 'interactive', hedge=hedge)
        outcome = 'failed'

        async def worker():
            token = _lease.set((self, job))
            try:
                return await operation()
            except Exception as exc:
                self._wire_error(job,exc);raise
            finally:
                _lease.reset(token)
                self._release(job)

        job.worker = asyncio.create_task(worker(), name='qqbot-model-' + job.kind)
        try:
            result = await job.worker
            if job.preempted:
                raise HedgePreempted('Interactive work needs this source slot')
            if job.paused:
                raise BackgroundPaused('Foreground work arrived')
            if self._closed:
                raise WorkClosed('Model scheduler is closed')
            outcome = 'ok'
            return result
        except asyncio.CancelledError:
            outcome = 'cancelled'
            if not asyncio.current_task().cancelling():
                reason = self._cancel_reason(job)
                outcome = ('preempted' if isinstance(reason, HedgePreempted) else
                           'paused' if isinstance(reason, BackgroundPaused) else 'closed')
                raise reason from None
            raise
        except BackgroundPaused:
            outcome = 'paused'
            raise
        except HedgePreempted:
            outcome = 'preempted'
            raise
        except Exception as exc:
            # Some adapters translate cancellation/cleanup into an ordinary
            # exception. Requested preemption must still preserve the caller's
            # recovery path instead of becoming a model failure.
            if (job.preempted or job.paused or self._closed) and not asyncio.current_task().cancelling():
                reason = self._cancel_reason(job)
                outcome = 'closed' if isinstance(reason, WorkClosed) else 'paused' if isinstance(reason, BackgroundPaused) else 'preempted'
                raise reason from None
            job.status_code=getattr(exc,'status_code',None) or getattr(getattr(exc,'response',None),'status_code',None)
            job.timed_out=isinstance(exc,TimeoutError) or type(exc).__name__=='APITimeoutError'
            raise
        finally:
            self._release(job)
            self._report(job, outcome)

    async def stream(self, provider, factory, *, kind=None, is_final=None):
        """Pull-driven stream: no buffering/prefetch, tools remain in the consumer.

        A dedicated model worker can be stopped even between yields without
        cancelling a consumer currently committing a tool or transport action.
        """
        key = provider_key(provider)
        if self._reentrant(key):
            iterator = factory()
            try:
                async for item in iterator:
                    if is_final and is_final(item):
                        await _aclose(iterator)
                        yield item
                        return
                    yield item
            finally:
                await _aclose(iterator)
            return
        job = await self._acquire(key, kind or _kind.get() or 'interactive')
        requests = asyncio.Queue(maxsize=1)
        pending = None
        outcome = 'cancelled'

        async def worker():
            token = _lease.set((self, job))
            iterator = None
            try:
                iterator = factory()
                while True:
                    reply = await requests.get()
                    try:
                        item = await anext(iterator)
                    except StopAsyncIteration:
                        if not reply.done():
                            reply.set_result(_DONE)
                        return
                    complete = bool(is_final and is_final(item))
                    if complete:
                        # The real Runner stops pulling after the final response.
                        # Finish model I/O now rather than retaining its slot until
                        # async-generator GC, potentially across review/tool work.
                        await _aclose(iterator)
                        iterator = None
                    if not reply.done():
                        reply.set_result(item)
                    if complete:
                        return
            except Exception as exc:
                self._wire_error(job,exc);raise
            finally:
                try:
                    if iterator is not None:
                        await _aclose(iterator)
                finally:
                    _lease.reset(token)
                    self._release(job)

        def finished(task):
            # Retrieve exceptions even when the consumer is suspended at yield.
            error = self._cancel_reason(job) if task.cancelled() else task.exception()
            if pending is not None and not pending.done():
                if error is not None:
                    pending.set_exception(error)
                else:
                    pending.set_result(_DONE)

        job.worker = asyncio.create_task(worker(), name='qqbot-model-stream-' + job.kind)
        job.worker.add_done_callback(finished)
        try:
            while True:
                if job.paused or self._closed:
                    raise self._cancel_reason(job)
                if job.worker.done():
                    if job.worker.cancelled():
                        raise self._cancel_reason(job)
                    job.worker.result()
                    outcome = 'ok'
                    return
                pending = asyncio.get_running_loop().create_future()
                requests.put_nowait(pending)
                item = await pending
                if job.paused or self._closed:
                    raise self._cancel_reason(job)
                if item is _DONE:
                    outcome = 'ok'
                    return
                yield item
        except BackgroundPaused:
            outcome = 'paused'
            raise
        except Exception as exc:
            if (job.paused or self._closed) and not asyncio.current_task().cancelling():
                reason = self._cancel_reason(job)
                outcome = 'closed' if isinstance(reason, WorkClosed) else 'paused'
                raise reason from None
            outcome = 'failed'
            job.status_code=getattr(exc,'status_code',None) or getattr(getattr(exc,'response',None),'status_code',None)
            job.timed_out=isinstance(exc,TimeoutError) or type(exc).__name__=='APITimeoutError'
            raise
        finally:
            if not job.worker.done():
                job.worker.cancel()
            await asyncio.gather(job.worker, return_exceptions=True)
            self._release(job)
            self._report(job, outcome)

    def status(self):
        now = self.clock()
        providers = {}
        for key in sorted(self._sources):
            running = [job for job in self._active if job.key == key]
            waiting = [job for job in self._waiting if job.key == key]
            providers[key] = {
                'limit': self._limit(key), 'auxiliary_limit': 1,
                'adaptive':self.adaptive.public(key),
                'running': {kind: sum(job.kind == kind for job in running) for kind in KINDS},
                'waiting': {kind: sum(job.kind == kind for job in waiting) for kind in KINDS},
                'oldest_wait_ms': round(max((now - job.queued for job in waiting), default=0) * 1000),
            }
        return {'closed': self._closed, 'busy': self.busy(), 'foreground': self._foreground,
                'idle_ms': None if self._idle_since is None else round((now - self._idle_since) * 1000),
                'running': len(self._active), 'waiting': len(self._waiting), 'providers': providers,
                'predictions_awaiting_primary': sum(job.after_primary and not job.trace.primary_admitted
                                                   for job in self._waiting)}

    async def close(self):
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        for job in self._waiting:
            if not job.ready.done():
                job.ready.set_exception(WorkClosed('Model scheduler is closed'))
        self._waiting.clear()
        jobs = tuple(self._active)
        tasks = [job.worker for job in jobs if job.worker is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for job in jobs:
            self._release(job)


async def _aclose(iterator):
    close = getattr(iterator, 'aclose', None)
    if close is not None:
        await close()


def _canonical(origin):
    try:
        platform, kind, target = str(origin).split(':', 2)
    except ValueError:
        return None
    if kind == 'GroupMessage':
        target = target.rsplit('_', 1)[-1]
    return (platform, kind, target) if target else None


def _destination(action, params, platform):
    if action == 'send_private_msg' or (action == 'send_msg' and params.get('message_type') == 'private'):
        group = False  # private temporary sessions may also carry group_id
    else:
        group = action == 'send_group_msg' or params.get('message_type') == 'group' or bool(params.get('group_id'))
    target = params.get('group_id') if group else params.get('user_id')
    if target is None:
        return None
    return _canonical(f"{platform}:{'GroupMessage' if group else 'FriendMessage'}:{target}")


def _text(message, auto_escape=False):
    if isinstance(message, str):
        # CQ control codes (images/replies/mentions) are not reply prose. Literal
        # escaped CQ strings remain text, exactly as the OneBot transport sees them.
        if auto_escape:
            return message
        return (re.sub(r'\[CQ:[^\]]*\]', '', message).replace('&#44;', ',')
                .replace('&#91;', '[').replace('&#93;', ']').replace('&amp;', '&'))
    if hasattr(message, 'chain'):
        message = message.chain
    parts = []
    for part in message or ():
        if isinstance(part, dict):
            if part.get('type') == 'text':
                value = (part.get('data') or {}).get('text', '')
                if isinstance(value, str):
                    parts.append(value)
        elif type(part).__name__ == 'Plain':
            parts.append(str(part.text))
    return '\n'.join(part for part in parts if part)


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _success(result):
    # CQHttp normally unwraps data and raises on errors. Also handle raw fake/
    # alternative transport envelopes; asynchronous acknowledgement is not delivery.
    if not isinstance(result, dict):
        return False
    if result.get('status', 'ok') != 'ok' or result.get('retcode', 0) != 0:
        return False
    data = result.get('data', result)
    return isinstance(data, dict) and not isinstance(data.get('message_id'),bool) and data.get('message_id') not in (None,'',0,'0')


def _extra(event, key):
    return event.get_extra(key)


@dataclass(eq=False)
class _Trace:
    runtime: object
    event: object
    trace_id: str
    target: tuple | None
    message_id: str
    received: float | None
    initial_receipt: object
    reply_owner: asyncio.Task | None = None
    final_owner: asyncio.Task | None = None
    final_expected: str | None = None
    pipeline_depth: int = 0
    reply_depth: int = 0
    foreground_scope: object = None
    hedge_used: bool = False
    primary_admitted: bool = False
    live: bool = True
    delivered: bool = False
    stages: dict = field(default_factory=dict)
    sends: deque = field(default_factory=lambda: deque(maxlen=32))


@dataclass(frozen=True)
class _Scope:
    trace: _Trace
    owner: asyncio.Task
    final_ready: bool = False
    expected: str | None = None


class WorkRuntime:
    def __init__(self, cfg, audit, *, clock=time.monotonic):
        self.cfg, self.audit, self.clock = cfg, audit, clock
        self.scheduler = ModelScheduler(cfg, self._audit, clock=clock)
        self._audit_errors = 0
        self._deliveries = 0
        self._incomplete = 0
        # hedged: admitted backup attempts; won: selected backup responses;
        # failed: backup exceptions/invalid responses (not losing cancellations).
        self._hedge_stats = {'hedged': 0, 'won': 0, 'failed': 0}
        self._hedge_tasks = set()
        self._hedge_sleep = asyncio.sleep

    @property
    def enabled(self):
        # Absent configuration preserves the pre-optimization behaviour too.
        return bool(_config_get(self.cfg, 'latency_optimization_enabled', False))

    def _audit(self, name, fields):
        try:
            self.audit(name, fields)
        except Exception:
            self._audit_errors += 1

    @contextmanager
    def _observe_provider(self, signature, provider, args, kwargs, *, stream=False, hedging=False):
        """Observe admitted calls only, independently of scheduler queue timing.

        provider_elapsed_ms covers provider preparation, retries, parsing and
        cleanup; it is NOT socket-only latency. Stream consumer/yield time is
        excluded by timing individual pulls. A nested chat-to-stream adapter is
        one call, but a separate task keeps its own observation.
        """
        owner = (self, asyncio.current_task())
        if _provider_observation.get() == owner:
            yield None
            return
        lease = _lease.get()
        job = lease[1] if lease and lease[0] is self.scheduler and lease[1].worker is owner[1] else None
        fields = {'mode': 'stream' if stream else 'nonstream',
                  'work_kind': job.kind if job else _kind.get() or 'interactive',
                  'call_role': ('hedge' if job and job.kind == 'auxiliary' else 'primary') if hedging else 'single'}
        if job is not None and job.hedge is not None and job.kind in {'interactive','review'}:
            fields['call_role'] = 'hedge_recovery'
        if job is not None:
            fields['queue_ms'] = round((job.started - job.queued) * 1000)
            if job.trace_id:
                fields['trace_id'] = job.trace_id
        try:
            fields.update(_request_metrics(signature, provider, args, kwargs))
        except Exception:
            # A diagnostic getter must not break/retry a valid model request.
            fields['request_shape_complete'] = False
        observation = {'status': 'cancelled', 'seconds': 0.0, 'usage': _usage_metrics(None)}
        from .chain_metrics import stage,phase,model_completed
        if fields['work_kind']=='interactive':stage('review' if phase.get()=='review' else 'model')
        token = _provider_observation.set(owner)
        self._audit('model_request', dict(fields))
        try:
            yield observation
        except asyncio.CancelledError:
            observation['status'] = 'preempted' if job is not None and job.preempted else 'cancelled'
            raise
        except GeneratorExit:
            raise
        except BaseException:
            observation['status'] = 'preempted' if job is not None and job.preempted else 'failed'
            raise
        finally:
            _provider_observation.reset(token)
            from .wire_admission import _frame
            if _frame.get():
                from .chain_metrics import add
                add('provider_total_ms',observation['seconds']*1000)
            else:model_completed(observation['seconds'],fields.get('queue_ms',0),fields['work_kind'])
            self._audit('model_provider_call', {**fields, **observation['usage'],
                'status': observation['status'], 'provider_elapsed_ms': round(observation['seconds'] * 1000)})

    @contextmanager
    def _provider_time(self, observation):
        started = self.clock()
        try:
            yield
        finally:
            if observation is not None:
                observation['seconds'] += self.clock() - started

    @staticmethod
    def _observe_usage(observation, response):
        if observation is not None:
            try:
                usage = _usage_metrics(response)
                if any(value is not None for value in usage.values()):
                    # Streaming usage is a snapshot, not an additive delta.
                    observation['usage'] = usage
            except Exception:
                pass

    def busy(self):
        return self.enabled and self.scheduler.busy()

    async def model_call(self,provider,operation,*,kind=None,hedge=None,maximum=5,on_start=None):
        from .wire_admission import native,scope
        lane=kind or _kind.get() or 'interactive'
        if getattr(self,'_wire_installed',False) and native(provider):
            async with scope(self.scheduler,provider,lane,maximum,hedge,on_start=on_start):
                return await operation()
        async def run():
            if on_start:on_start()
            return await operation()
        return await self.scheduler.run(provider,run,kind=lane,hedge=hedge)

    @asynccontextmanager
    async def parked_model(self):
        if getattr(self,'reply_queue',None) is None:
            yield;return
        from .reply_jobs import current_job
        queue=getattr(self,'reply_queue',None);job=current_job.get()
        if queue and job and job.owner is asyncio.current_task():
            async with queue.parked(job,'model_or_retry_wait'):yield
        else:yield

    def status(self):
        return {**self.scheduler.status(), 'enabled': self.enabled, 'busy': self.busy(),
                'reply_text_deliveries': self._deliveries,
                'incomplete_deliveries': self._incomplete, 'audit_errors': self._audit_errors,
                'hedge': dict(self._hedge_stats)}

    @contextmanager
    def prediction_work(self, event):
        """Hold only speculative prediction until this turn's foreground starts.

        This wait owns no model slot. Once the foreground is admitted, the
        existing shared limit permits prediction in parallel in spare capacity.
        Detached previews and other auxiliary work retain their existing path.
        """
        state = _extra(event, _STATE_KEY)
        if (not self.enabled or not isinstance(state, _Trace) or state.runtime is not self
                or not state.live or state.reply_owner is None):
            yield
            return
        token = _prediction_turn.set(state)
        try:
            yield
        finally:
            _prediction_turn.reset(token)

    def _hedge_seconds(self):
        value = _config_get(self.cfg, 'reply_hedge_after_seconds', 15)
        try:
            seconds = float(value)
            return seconds if not isinstance(value, bool) and math.isfinite(seconds) and seconds > 0 else 0
        except (TypeError, ValueError, OverflowError):
            return 0

    def _hedge_trace(self, values):
        scope = self._scope(owner_required=False)
        if (not self.enabled or not values or not self._hedge_seconds()
                or (_kind.get() or 'interactive') not in {'interactive','review'} or _lease.get() is not None
                or not scope or scope.trace.reply_owner is None or scope.trace.hedge_used
                or scope.trace.delivered or values.get('image_urls') or values.get('audio_urls')
                or values.get('tool_calls_result') or _has_current_tools(values.get('contexts'))
                or _has_current_media(values)
                or _kind.get()=='review' and values.get('func_tool') is not None):
            return None
        return scope.trace

    def _hedge_stat(self, name, state, provider):
        self._hedge_stats[name] += 1
        self._audit('reply_hedge', {'status': name, 'trace_id': state.trace_id,
                                    'provider': provider_key(provider)})

    def _hedge_task(self, coroutine, name):
        task = asyncio.create_task(coroutine, name='qqbot-hedge-' + name)
        self._hedge_tasks.add(task)
        task.add_done_callback(self._hedge_tasks.discard)
        return task

    async def _hedged_chat(self, provider, previous, args, kwargs, values, state):
        """Race one identical model-only backup, yielding to actual queued work.

        No new global patches, generation limits, tools, transports, or cheaper
        model choices. Existing SDK/provider retries stay unchanged; cancelling
        local I/O cannot guarantee that an accepted upstream call stops billing.
        After preemption, wait without a slot for the primary. Only if it fails,
        allow one non-speculative recovery with the original frozen payload.
        """
        from astrbot.core.provider.entities import LLMResponse

        delay = self._hedge_seconds()
        primary_kind = _kind.get() or 'interactive'
        try:
            # Freeze both requests BEFORE either can mutate its context/payload.
            # ToolSet holds bound application handlers: keep it as the original
            # read-only schema source, never deepcopy or call those handlers.
            tool = values.get('func_tool')
            memo = {id(tool): tool} if tool is not None else {}
            # The runner's live cancellation event may already contain a waiter.
            # Share this control signal, not a deep-copied event/asyncio Future.
            abort_signal = values.get('abort_signal')
            if isinstance(abort_signal, asyncio.Event):
                memo[id(abort_signal)] = abort_signal
            first_args, first_kwargs = copy.deepcopy((args, kwargs), memo.copy())
            second_args, second_kwargs = copy.deepcopy((args, kwargs), memo.copy())
            recovery_args, recovery_kwargs = copy.deepcopy((args, kwargs), memo.copy())
            template = _isolated_provider(provider)
            if template is provider:
                template = copy.copy(provider)
                template.provider_config = copy.deepcopy(getattr(provider, 'provider_config', {}))
        except Exception:
            # Unknown request objects may not be copyable. The optional hedge
            # must never break the established single-call path.
            from .wire_admission import attempts
            return await self.model_call(provider, lambda: previous(_isolated_provider(provider), *args, **kwargs),maximum=attempts(kwargs))

        started, primary_success, primary_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        hedge_control = _HedgeControl()
        children = []
        first_review = None
        first_review_origin = None

        def validate(response, source):
            nonlocal first_review, first_review_origin
            if not _valid_model_response(response, LLMResponse):
                raise InvalidModelResponse('Model returned no complete usable response')
            # Capture before releasing admission, not in completion-callback
            # order. A rejected/malformed first verdict must never lose to a
            # later pass. The reviewer still parses, rewrites and rechecks it.
            if primary_kind=='review' and first_review is None:
                first_review=response;first_review_origin=source
            return response

        def selected(response):
            return first_review if first_review is not None else response

        async def primary_operation():
            lane = _isolated_provider(template)
            try:
                result = validate(await previous(lane, *first_args, **first_kwargs),'primary')
                primary_success.set()  # Set BEFORE releasing the source's slot.
                hedge_control.primary_succeeded = True
                return result
            except Exception:
                hedge_control.primary_failed = True
                raise
            finally:
                hedge_control.primary_done = True
                primary_finished.set()

        async def timer_operation():
            await started.wait()
            await self._hedge_sleep(delay)

        async def backup_operation(call_args, call_kwargs):
            # Admission may take time. Do not send a queued duplicate after the
            # primary succeeded, or after the user disabled this feature.
            if (primary_success.is_set() or not self.enabled or not self._hedge_seconds()
                    or not state.live or state.delivered):
                return _HEDGE_SKIPPED
            self._hedge_stat('hedged', state, template)
            try:
                return validate(await previous(_isolated_provider(template), *call_args, **call_kwargs),'backup')
            except Exception:
                lease = _lease.get()
                if not (lease and lease[0] is self.scheduler and lease[1].preempted):
                    self._hedge_stat('failed', state, template)
                raise

        async def backup_request():
            with model_work('auxiliary'):
                try:
                    from .wire_admission import attempts
                    return await self.model_call(template, lambda: backup_operation(second_args, second_kwargs),
                                                    kind='auxiliary', hedge=hedge_control,maximum=attempts(second_kwargs))
                except HedgePreempted:
                    self._audit('reply_hedge', {'status': 'preempted', 'trace_id': state.trace_id,
                                               'provider': provider_key(template)})
            # Do not repeatedly restart speculative work or retain a model slot.
            # Waiting on an Event also cannot propagate cancellation to primary.
            await primary_finished.wait()
            if not hedge_control.primary_failed:
                return _HEDGE_SKIPPED
            with model_work(primary_kind):
                from .wire_admission import attempts
                return await self.model_call(template, lambda: backup_operation(recovery_args, recovery_kwargs),
                                                kind=primary_kind, hedge=hedge_control,maximum=attempts(recovery_kwargs))

        try:
            from .wire_admission import attempts
            primary = self._hedge_task(self.model_call(template, primary_operation, kind=primary_kind,maximum=attempts(first_kwargs),on_start=started.set), 'primary')
            children.append(primary)
            timer = self._hedge_task(timer_operation(), 'timer')
            children.append(timer)
            await asyncio.wait((primary, timer), return_when=asyncio.FIRST_COMPLETED)
            if primary.done():
                from openai import APIConnectionError
                try:
                    return selected(primary.result())
                except APIConnectionError:
                    # A disconnected review has no verdict to preserve. Use
                    # the same turn's one backup budget even when it failed
                    # before the slow-request timer; never retry a verdict.
                    if (primary_kind != 'review' or first_review is not None
                            or self.scheduler._closed or not self.enabled
                            or not self._hedge_seconds() or state.hedge_used
                            or not state.live or state.delivered):
                        raise
                    state.hedge_used = True
                    result = await self.model_call(
                        template, lambda: backup_operation(recovery_args, recovery_kwargs),
                        kind=primary_kind, hedge=hedge_control, maximum=attempts(recovery_kwargs))
                    if result is _HEDGE_SKIPPED:
                        raise
                    self._hedge_stat('won', state, template)
                    return selected(result)
            if self.scheduler._closed:
                raise WorkClosed('Model scheduler is closed')
            timer.result()
            if (primary_success.is_set() or not self.enabled or not self._hedge_seconds()
                    or state.hedge_used or not state.live or state.delivered):
                return selected(await primary)
            # This shared event state also covers sibling tasks, retries and
            # provider fallbacks in the SAME turn. Claim synchronously, once.
            state.hedge_used = True
            backup = self._hedge_task(backup_request(), 'backup')
            children.append(backup)
            completed = asyncio.Queue()
            primary.add_done_callback(completed.put_nowait)
            backup.add_done_callback(completed.put_nowait)
            errors = {}
            for _ in range(2):
                candidate = await completed.get()
                try:
                    result = candidate.result()
                except (Exception, asyncio.CancelledError) as error:
                    if self.scheduler._closed:
                        raise WorkClosed('Model scheduler is closed') from None
                    errors[candidate] = error
                    continue
                if result is _HEDGE_SKIPPED:
                    continue
                if (first_review_origin=='backup' if primary_kind=='review' else candidate is backup):
                    self._hedge_stat('won', state, template)
                return selected(result)
            # Preserve the primary failure when neither supplied a valid result.
            raise errors.get(primary) or errors.get(backup) or InvalidModelResponse('No usable model response')
        finally:
            for child in children:
                if not child.done():
                    child.cancel()
            # Never leave the losing request, a queued admission, or a timer
            # running after returning a response or cancelling the parent.
            if children:
                await asyncio.gather(*children, return_exceptions=True)

    def _trace(self, event):
        state = _extra(event, _STATE_KEY)
        received = _extra(event, 'qqbot_received_at')
        if (isinstance(received, bool) or not isinstance(received, (float, int))
                or not math.isfinite(received) or not 0 <= received <= self.clock()):
            received = None
        if isinstance(state, _Trace) and state.runtime is self:
            # execute() starts before track_session supplies the arrival stamp.
            # Capture it once available and retain it after old send hooks clear it.
            if state.received is None and received is not None:
                state.received = received
            return state
        state = _Trace(self, event, uuid.uuid4().hex, _canonical(event.unified_msg_origin),
                       str(getattr(getattr(event, 'message_obj', None), 'message_id', '') or ''),
                       received, _extra(event, 'qqbot_bili_sent'))
        event.set_extra(_STATE_KEY, state)
        event.set_extra('qqbot_work_trace_id', state.trace_id)
        return state

    def mark(self, event, stage):
        """Record a stage; final_ready explicitly authorizes this task's final send.

        A final marker never retrospectively promotes arbitrary tool messages.
        Prefer the explicit qqbot_final_text extra; get_result() is only a fallback
        because on_agent_done may still see an intermediate tool result.
        Calling from another task requires its own explicit event marker.
        """
        if not self.enabled or self.scheduler._closed:
            return
        state = self._trace(event)
        if not state.live:
            return
        now = self.clock()
        state.stages[str(stage)] = now
        fields = {'trace_id': state.trace_id, 'stage': str(stage)}
        if state.received is not None:
            fields['elapsed_ms'] = round((now - state.received) * 1000)
        self._audit('reply_stage', fields)
        current = self._scope()
        if stage == 'final_ready':
            final_text = _extra(event, 'qqbot_final_text')
            if isinstance(final_text, str):
                expected = _digest(final_text)  # Explicit empty text authorizes no prose.
            else:
                result = getattr(event, 'get_result', lambda: None)()
                text = _text(getattr(result, 'chain', None))
                expected = _digest(text) if text.strip() else None
            # Framework hooks may execute in a helper task. An explicit event
            # marker authorizes the original reply task as well as this caller;
            # an arbitrary descendant still cannot claim a final delivery.
            state.final_owner = state.reply_owner or asyncio.current_task()
            state.final_expected = expected
            _reply.set(_Scope(state, asyncio.current_task(), True, expected))
        elif current is None or current.trace is not state:
            _reply.set(_Scope(state, asyncio.current_task()))
        self._confirmed_tool(state)

    def _scope(self, *, owner_required=True):
        scope = _reply.get()
        if (scope and scope.trace.runtime is self and scope.trace.live
                and (not owner_required or scope.owner is asyncio.current_task())):
            return scope
        return None

    @contextmanager
    def pipeline_work(self, event):
        """Keep delivery identity through the outer processor's fallback sends.

        Normal nested yields cover respond already. A generator that leaves a
        result WITHOUT yielding is exhausted before the processor runs respond;
        its trace must survive until execute() returns. This envelope itself does
        not count passive, non-reply incoming events as foreground work.
        """
        if not self.enabled:
            yield
            return
        state = self._trace(event)
        state.pipeline_depth += 1
        token = _reply.set(_Scope(state, asyncio.current_task()))
        try:
            yield
        finally:
            state.pipeline_depth -= 1
            if not state.pipeline_depth:
                self._finish_trace(state)
            self._reset_scope(token)

    def _finish_trace(self, state):
        self._confirmed_tool(state)
        state.live = False
        foreground, state.foreground_scope = state.foreground_scope, None
        if foreground is not None:
            foreground.__exit__(None, None, None)

    @staticmethod
    def _reset_scope(token):
        try:
            _reply.reset(token)
        except ValueError:
            # Async-generator finalization can run in a separate context.
            # The originating scope is inert once its trace is no longer live.
            pass

    @contextmanager
    def reply_work(self, event):
        if not self.enabled:
            yield
            return
        state = self._trace(event)
        state.reply_owner = asyncio.current_task()
        state.reply_depth += 1
        token = _reply.set(_Scope(state, asyncio.current_task()))
        try:
            if state.foreground_scope is None:
                foreground = self.scheduler.foreground()
                foreground.__enter__()
                state.foreground_scope = foreground
            self.mark(event, 'reply_queued')
            yield
        finally:
            self._confirmed_tool(state)
            state.reply_depth -= 1
            if not state.pipeline_depth and not state.reply_depth:
                self._finish_trace(state)
            self._reset_scope(token)

    def _delivered(self, state, at, via):
        if state.delivered:
            return
        state.delivered = True
        if state.received is None:
            self._incomplete += 1
            self._audit('reply_delivery_incomplete', {'trace_id': state.trace_id, 'reason': 'missing_received_at'})
            return
        elapsed = round((at - state.received) * 1000)
        self._deliveries += 1
        self._audit('reply_text_delivery', {'elapsed_ms': elapsed, 'trace_id': state.trace_id,
                                          'within_30s': elapsed <= 30000, 'via': via})

    def _confirmed_tool(self, state):
        receipt = _extra(state.event, 'qqbot_bili_sent')
        if (state.delivered or not isinstance(receipt, dict) or receipt is state.initial_receipt
                or not getattr(state.event, 'is_stopped', lambda: False)()):
            return
        # Existing Bili receipt is written AFTER transport success. Only a stopped
        # single-purpose request is final; composite-tool sends are intermediate.
        text = receipt.get('text')
        if not isinstance(text, str) or not text.strip():
            return
        for at, digest in state.sends:
            if digest == _digest(text):
                self._delivered(state, at, 'tool_receipt')
                return

    async def transport(self, previous, bot, action, params, *, platform='aiocqhttp', receipt=None):
        """Testable boundary around the already installed CQHttp guards/journal."""
        # Descendant tool tasks may contribute confirmed component receipts, but
        # inheriting a ContextVar is not authorization to send the final reply.
        scope = self._scope(owner_required=False) if self.enabled and not self.scheduler._closed else None
        eligible = bool(scope and action in _SEND_ACTIONS and scope.trace.target is not None
                        and scope.trace.target == _destination(action, params, platform)
                        and scope.trace.target == _canonical(scope.trace.event.unified_msg_origin)
                        and scope.trace.message_id == str(getattr(
                            getattr(scope.trace.event, 'message_obj', None), 'message_id', '') or ''))
        before = receipt() if eligible and receipt else None
        original = _text(params.get('message'), params.get('auto_escape', False)) if eligible else ''
        voice=''
        if eligible and not original.strip():
            from .voice_memory import spoken_text
            parts=params.get('message')
            if isinstance(parts,list):voice=spoken_text(parts,bot,scope.trace.event.unified_msg_origin)
        # Snapshot authority BEFORE awaiting guards/transport. A marker arriving
        # during an unrelated send cannot turn that in-flight send into the final.
        own_scope = eligible and scope.owner is asyncio.current_task()
        final_ready = own_scope and (scope.final_ready or scope.trace.final_owner is asyncio.current_task())
        expected = (scope.expected if scope.final_ready else scope.trace.final_expected) if eligible else None
        started = self.clock()
        result = await previous(bot, action, **params)
        at = self.clock()
        if not eligible or not scope.trace.live or not _success(result):
            return result
        actual = original
        checked = receipt() if receipt else None
        if isinstance(checked, dict) and checked is not before:
            if (_canonical(checked.get('origin')) != scope.trace.target
                    or checked.get('original') != original):
                return result  # A changed, unmatched guard receipt is ambiguous.
            actual = checked.get('text', '')
        if not actual and voice:actual=voice
        if not isinstance(actual, str) or not actual.strip():
            return result
        state = scope.trace
        state.sends.append((at, _digest(actual)))
        self._audit('reply_text_send', {'elapsed_ms': round((at - started) * 1000), 'trace_id': state.trace_id})
        if final_ready and (expected is None or expected == _digest(original or voice)):
            self._delivered(state, at, 'voice_final_ready' if voice else 'final_ready')
        return result

    async def close(self):
        await self.scheduler.close()
        tasks = tuple(self._hedge_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _isolated_provider(provider):
    """Copy SDK resources through its supported API, not copy.copy(client).

    with_options() shares the established HTTP pool but builds independent SDK
    resources and mutable authentication fields. Do NOT close the shared pool,
    choose a key, or write a key: the original provider owns rotation as usual.
    """
    client = getattr(provider, 'client', None)
    if client is None:
        return provider
    clone = getattr(client, 'with_options', None)
    if not callable(clone):
        raise RuntimeError('Provider client cannot isolate concurrent model requests')
    lane = copy.copy(provider)
    lane.client = clone()
    if lane.client is client:
        raise RuntimeError('Provider client cloning returned shared mutable state')
    if hasattr(provider, 'provider_config'):
        lane.provider_config = copy.deepcopy(provider.provider_config)
    if hasattr(provider, 'api_keys'):
        lane.api_keys = list(provider.api_keys)
    return lane


def install_work_runtime(plugin):
    """Wrap existing implementations, preserving signatures and async generators."""
    from .hook_lifecycle import HookLease
    lease=HookLease('work_runtime',plugin)
    runtime = getattr(plugin, '_work', None)
    if not isinstance(runtime, WorkRuntime):
        runtime = plugin._work = WorkRuntime(plugin._cfg, plugin._audit)
    runtime.reply_queue=getattr(plugin,'_reply_queue',None)
    from .wire_admission import install as install_wire,attempts,native as native_wire,scope as wire_scope
    restore_wire=install_wire(runtime);runtime._wire_installed=restore_wire is not None
    from aiocqhttp import CQHttp
    from astrbot.core.pipeline.scheduler import PipelineScheduler
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import InternalAgentSubStage
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from astrbot.core.provider.entities import LLMResponse
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from .conversation_review import _receipt

    previous_process = InternalAgentSubStage.process
    previous_execute = PipelineScheduler.execute
    previous_chat = ProviderOpenAIOfficial.text_chat
    previous_stream = ProviderOpenAIOfficial.text_chat_stream
    previous_send = CQHttp.call_action
    previous_await = ToolLoopAgentRunner._await_or_stop
    chat_signature = inspect.signature(previous_chat)
    stream_signature = inspect.signature(previous_stream)

    @wraps(previous_await)
    async def await_or_stop(runner, awaitable):
        if not lease.active or not runtime.enabled:
            return await previous_await(runner,awaitable)
        # Native Runner creates a child task for model/context operations. The
        # provider hook in that child cannot release its parent's execution
        # capacity. Park the real owner before spawning; native cancellation
        # still joins the operation before execution is reacquired.
        async with runtime.parked_model():
            return await previous_await(runner,awaitable)

    @wraps(previous_execute)
    async def execute(scheduler, event):
        if not lease.active:return await previous_execute(scheduler,event)
        with runtime.pipeline_work(event):
            return await previous_execute(scheduler, event)

    @wraps(previous_process)
    async def process(stage, event, provider_wake_prefix):
        iterator = previous_process(stage, event, provider_wake_prefix)
        from .reply_admission import has_agent_content
        if not lease.active or not has_agent_content(event):
            try:
                async for item in iterator:yield item
            finally:await _aclose(iterator)
            return
        # The original admission and locks stay authoritative. Only events that
        # reached this reply stage count, never every observed group message.
        with runtime.reply_work(event):
            try:
                async for item in iterator:
                    if runtime.enabled:
                        runtime._confirmed_tool(runtime._trace(event))
                    yield item
            finally:
                await _aclose(iterator)

    @wraps(previous_chat)
    async def text_chat(provider, *args, **kwargs):
        if not lease.active:return await previous_chat(provider,*args,**kwargs)
        args, kwargs, values = _strip_tail_guard(chat_signature, provider, args, kwargs)
        if not runtime.enabled:
            return await previous_chat(provider, *args, **kwargs)
        state = runtime._hedge_trace(values)

        async def observed_chat(lane, *call_args, **call_kwargs):
            with runtime._observe_provider(chat_signature, lane, call_args, call_kwargs, hedging=state is not None) as observation:
                with runtime._provider_time(observation):
                    result = await previous_chat(lane, *call_args, **call_kwargs)
                verdict_observer=_review_verdict_observer.get()
                if verdict_observer is not None:verdict_observer()
                runtime._observe_usage(observation, result)
                if observation is not None:
                    observation['status'] = 'ok'
                return result

        async with runtime.parked_model():
            if state is not None:
                return await runtime._hedged_chat(provider, observed_chat, args, kwargs, values, state)
            return await runtime.model_call(provider, lambda: observed_chat(_isolated_provider(provider), *args, **kwargs),maximum=attempts(kwargs))

    @wraps(previous_stream)
    async def text_chat_stream(provider, *args, **kwargs):
        if not lease.active:
            iterator=previous_stream(provider,*args,**kwargs)
            try:
                async for item in iterator:yield item
            finally:await _aclose(iterator)
            return
        args, kwargs, _ = _strip_tail_guard(stream_signature, provider, args, kwargs)
        if not runtime.enabled:
            iterator = previous_stream(provider, *args, **kwargs)
        else:
            async def observed_stream():
                lane = _isolated_provider(provider)
                with runtime._observe_provider(stream_signature, lane, args, kwargs, stream=True) as observation:
                    original = previous_stream(lane, *args, **kwargs)
                    try:
                        while True:
                            try:
                                with runtime._provider_time(observation):
                                    item = await anext(original)
                            except StopAsyncIteration:
                                if observation is not None:
                                    observation['status'] = 'ok'
                                return
                            runtime._observe_usage(observation, item)
                            if isinstance(item, LLMResponse) and not item.is_chunk and observation is not None:
                                observation['status'] = 'ok'
                            yield item
                    finally:
                        with runtime._provider_time(observation):
                            await _aclose(original)

            if native_wire(provider):
                async def admitted_stream():
                    async with wire_scope(runtime.scheduler,provider,_kind.get() or 'interactive',attempts(kwargs),streaming=True):
                        inner=observed_stream()
                        try:
                            async for item in inner:yield item
                        finally:await _aclose(inner)
                from .stream_context import owned_stream
                iterator=owned_stream(admitted_stream,
                    is_final=lambda item:isinstance(item,LLMResponse) and not item.is_chunk)
            else:
                iterator = runtime.scheduler.stream(provider, observed_stream,
                    is_final=lambda item: isinstance(item, LLMResponse) and not item.is_chunk)
        try:
            async with runtime.parked_model():
                async for item in iterator:yield item
        finally:
            await _aclose(iterator)

    @wraps(previous_send)
    async def send(bot, action, **params):
        if not lease.active or not runtime.enabled:
            return await previous_send(bot, action, **params)
        platform = plugin._platform()
        if platform is None or getattr(platform, 'bot', None) is not bot:
            return await previous_send(bot, action, **params)
        return await runtime.transport(previous_send, bot, action, params,
                                       platform=_config_get(runtime.cfg, 'platform_id', 'aiocqhttp'), receipt=_receipt.get)

    patches = [(PipelineScheduler, 'execute', execute, previous_execute),
               (ToolLoopAgentRunner, '_await_or_stop', await_or_stop, previous_await),
               (InternalAgentSubStage, 'process', process, previous_process),
               (ProviderOpenAIOfficial, 'text_chat', text_chat, previous_chat),
               (ProviderOpenAIOfficial, 'text_chat_stream', text_chat_stream, previous_stream),
               (CQHttp, 'call_action', send, previous_send)]
    for cls, name, wrapper, previous in patches:
        # wraps exposes __wrapped__; the explicit signature also serves tools
        # that inspect __signature__ directly rather than unwrapping decorators.
        wrapper.__signature__ = inspect.signature(previous)
        setattr(cls, name, wrapper)

    def restore():
        lease.close()
        if restore_wire:restore_wire()
        runtime._wire_installed=False
        for cls, name, wrapper, previous in reversed(patches):
            if getattr(cls, name) is wrapper:
                setattr(cls, name, previous)
    return restore
