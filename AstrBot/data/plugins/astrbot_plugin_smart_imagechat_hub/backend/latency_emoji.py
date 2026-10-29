"""Opt-in, event-scoped emoji preparation and delivery; never await it in text hooks."""
import asyncio
import copy
import math
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from astrbot.api.message_components import Record

from .common import (
    Image, MessageEventResult, Plain, PROACTIVE_EMOJI_INTERNAL_LLM_EXTRA_KEY,
    SKIP_PROACTIVE_EMOJI_EXTRA_KEY, logger,
)
from .image_intent import is_generation_request, is_library_request
from .proactive_fast_retrieval import (
    _fallback_rank, _fast_query_profile, _item_tags, _score_fast_candidate,
)

LATENCY_EMOJI_KEY = "smart_image_latency_emoji"
_TURN_ROUTE_KEY = "smart_image_latency_turn_route"
LATE_GRACE_DEFAULT = 3.0
_COMPANION_FIELDS = {
    "latency_optimization_enabled": False, "meme_separate_probability": 50,
    "emoji_late_grace_seconds": LATE_GRACE_DEFAULT,
    "reply_review_enabled": True, "review_policy": "all",
    "reply_prediction_enabled": True, "prediction_provider_id": "",
    "father_qq": "", "father_address_probability": 20, "emoji_probability": 18,
}
_TONES = {
    "positive": ("开心", "快乐", "哈哈", "大笑", "笑死", "高兴", "得意", "庆祝"),
    "sad": ("难过", "伤心", "悲伤", "委屈", "流泪", "哭泣", "不开心", "不高兴"),
    "angry": ("生气", "愤怒", "气死", "火大", "恼火", "怒火"),
    "apology": ("对不起", "抱歉", "道歉", "认错", "错了", "认怂", "不敢", "别凶"),
    "agree": ("同意", "赞同", "支持", "点头", "没问题", "可以", "好呀"),
    "reject": ("拒绝", "不同意", "不赞同", "不支持", "不可以", "不行", "摇头"),
}
_OPPOSITES = {
    "positive": {"sad", "angry", "apology"}, "sad": {"positive", "angry"},
    "apology": {"positive", "angry"}, "angry": {"positive", "sad", "apology"},
    "agree": {"reject"}, "reject": {"agree"},
}


def _tone_text(text):
    # Keep full text for scoring, but don't turn a negated sentiment into evidence.
    text = str(text or "").lower()
    text = text.replace("不开心", "难过").replace("不高兴", "难过")
    text = text.replace("不同意", "拒绝").replace("不赞同", "拒绝").replace("不支持", "拒绝").replace("不可以", "拒绝")
    return re.sub(r"(?:并不|不是|没有|不再|不要|别|不)(?:是|再|很|太)?(?:开心|高兴|快乐|生气|愤怒|难过|伤心|笑|同意|支持)", "", text)


def _tones(text):
    text = _tone_text(text)
    return {key for key, cues in _TONES.items() if any(cue in text for cue in cues)}


def local_candidates(text, candidates, *, ambiguous=False):
    """Score the ENTIRE eligible index; only the model payload is capped at 10/30.

    Reuse the fast retriever's scoring primitives instead of its fixed top-10
    wrapper, so widening never mutates its global limit or truncates the library.
    """
    profile = _fast_query_profile(_tone_text(text), candidates)
    ranked = [_score_fast_candidate(profile, item) for item in candidates]
    ranked.sort(key=lambda item: (
        item.get("proactive_fast_score", 0), _fallback_rank(item),
        len(item.get("proactive_fast_hints", [])), str(item.get("id", "")),
    ), reverse=True)
    tones = _tones(text)
    conflicting = any(_OPPOSITES.get(tone, set()) & tones for tone in tones)
    weak = not ranked or ranked[0].get("proactive_fast_score", 0) < 6
    return ranked[:30 if ambiguous or conflicting or weak else 10]


def compatible_with_reply(text, character, item, *, semantic_match=False):
    """Conservative final check: full reply relevance plus its concluding tone."""
    mood = (character or {}).get("mood")
    if mood in {"serious", "stop"}:
        return False
    full_text = _tone_text(text)
    # A reversal at the end must override an earlier joke/quoted sentiment.
    clauses = re.split(r"但是|但其实|不过|其实|然而|反而|但", full_text)
    tones = _tones(clauses[-1]) or _tones(full_text)
    tags = " ".join(_item_tags(item))
    image_tones = _tones(tags)
    if any(_OPPOSITES.get(tone, set()) & image_tones for tone in tones):
        return False
    if (tones & {"sad", "apology"}) and re.search(
        r"嘲讽|挑衅|幸灾乐祸|嘲笑|得意|大笑", tags,
    ):
        return False
    # A model-selected library ID already has semantic evidence. Requiring a
    # second literal keyword match rejects paraphrases like “眼皮开始打架”.
    # Final tone/character contradictions above still veto it unconditionally.
    if semantic_match:
        return True
    ranked = local_candidates(full_text, [item])
    return bool(ranked and ranked[0].get("proactive_fast_score", 0) >= 6)


def _plain(chain):
    return "".join(part.text for part in getattr(chain, "chain", ()) if isinstance(part, Plain))


def _identity(item):
    return {key: item.get(key) for key in (
        "id", "rel_path", "filename", "tags", "auto_tags", "manual_tags", "selected_global_tags",
    )}


@dataclass(eq=False)
class EmojiTurn:
    event: object
    companion: object
    origin: str
    route: tuple
    snapshot: object
    cfg: dict
    settings: dict
    style: object
    candidates: list
    prediction: object = None
    prepare: object = None
    delivery: object = None
    conversion: object = None
    item: object = None
    path: object = None
    signature: object = None
    cleanup: list = field(default_factory=list)
    text: str = ""
    character: dict = field(default_factory=dict)
    sent_parts: list = field(default_factory=list)
    sent_original_parts: list = field(default_factory=list)
    sent_at: float = 0.0
    failed: bool = False
    decorated: bool = False
    confirmed: bool = False
    embedded: bool = False
    ready_at_decoration: bool = False
    result: object = None
    attachment: object = None
    sending: int = 0
    closed: bool = False
    restore_send: object = None
    owner: object = None
    owner_done: object = None
    cancel_callback: object = None
    previous_cancel: object = None
    pool: list = field(default_factory=list)
    preview_text: str = ""
    preview_ready: object = field(default_factory=asyncio.Event)
    final_ready: object = field(default_factory=asyncio.Event)
    selection_source: str = ""


class LatencyEmojiMixin:
    def _init_latency_emoji(self):
        self._latency_emoji_tasks = set()
        self._latency_emoji_conversions = set()
        self._latency_emoji_turns = {}
        self._latency_emoji_closed = False
        self._latency_emoji_stats = {}

    def _record_latency_emoji(self, status, companion=None):
        stats = self._latency_emoji_stats
        stats[status] = stats.get(status, 0) + 1
        audit = getattr(companion, "_audit", None)
        if callable(audit):
            audit("emoji_pipeline", {"status": status})

    def _latency_companion(self):
        lookup = getattr(getattr(self, "context", None), "get_registered_star", None)
        meta = lookup("astrbot_plugin_proactive_agent") if callable(lookup) else None
        companion = meta.star_cls if meta and meta.activated else None
        return companion if companion and companion._cfg("latency_optimization_enabled", False) else None

    def _uses_latency_emoji(self, event):
        return event.get_extra(LATENCY_EMOJI_KEY) is not None or self._latency_companion() is not None

    def _latency_settings(self, companion):
        return copy.deepcopy({key: companion._cfg(key, default) for key, default in _COMPANION_FIELDS.items()})

    def _latency_style(self):
        getter = getattr(self, "_send_image_style_config", None)
        return copy.deepcopy(getter()) if callable(getter) else None

    def _advance_latency_turn(self, event):
        if event.get_extra(PROACTIVE_EMOJI_INTERNAL_LLM_EXTRA_KEY, False):
            return
        previous_route = event.get_extra(_TURN_ROUTE_KEY)
        if previous_route is not None:
            return previous_route
        origin = str(getattr(event, "unified_msg_origin", ""))
        # The transport group, not a per-member unique session, owns the window.
        parts = origin.split(":", 2)
        if len(parts) == 3 and parts[1] == "GroupMessage":
            parts[2] = parts[2].rsplit("_", 1)[-1]
        route = tuple(parts)
        event.set_extra(_TURN_ROUTE_KEY, route)
        previous = getattr(self, "_latency_emoji_turns", {}).get(route)
        if previous and previous.event is not event:
            self._finish_latency_turn(previous, "superseded")
        return route

    def _own_latency_task(self, coroutine, name):
        task = asyncio.create_task(coroutine, name=name)
        self._latency_emoji_tasks.add(task)
        def done(task):
            self._latency_emoji_tasks.discard(task)
            if not task.cancelled() and task.exception():
                logger.warning("smart_image latency emoji task failed: %s", type(task.exception()).__name__)
        task.add_done_callback(done)
        return task

    def _start_latency_emoji(self, event, user_message):
        if event.get_extra(PROACTIVE_EMOJI_INTERNAL_LLM_EXTRA_KEY, False):
            return False
        route = self._advance_latency_turn(event)
        if event.get_extra(LATENCY_EMOJI_KEY) is not None:
            return True
        companion = self._latency_companion()
        if companion is None:
            return False
        # A marker prevents re-entering the old serial path after a config edit.
        event.set_extra(LATENCY_EMOJI_KEY, False)
        if not hasattr(self, "_latency_emoji_tasks"):
            self._init_latency_emoji()
        cfg = self._proactive_emoji_config()
        if (self._latency_emoji_closed or not cfg.get("enabled") or not str(user_message).strip()
                or event.get_extra(SKIP_PROACTIVE_EMOJI_EXTRA_KEY, False)
                or is_library_request(user_message) or is_generation_request(user_message)):
            return True
        if not all(callable(getattr(companion, name, None)) for name in ("prepare_reply_prediction", "reply_prediction")):
            return True
        if not self._proactive_emoji_probability_hit(cfg):
            self._record_latency_emoji("probability_skipped", companion)
            return True
        self._record_latency_emoji("triggered", companion)
        pool = self._proactive_emoji_candidates(bool(cfg["meme_only"]))
        candidates = local_candidates(user_message, pool)
        if not candidates:
            self._record_latency_emoji("empty_library", companion)
            return True
        origin = str(event.unified_msg_origin)
        group = str(event.get_group_id() or "")
        sender = str(event.get_sender_id() or "")
        original_message = getattr(event, "message_obj", None)
        # AstrBot's raw message can carry a reply job, Future and an event backref.
        # Copy only the transport identity we need; deepcopy of the graph fails
        # before preparation starts and can also traverse unrelated runtime state.
        message_obj = SimpleNamespace(
            message_id=str(getattr(original_message, "message_id", "") or ""),
            sender=SimpleNamespace(user_id=sender), group_id=group,
            self_id=str(getattr(original_message, "self_id", "") or ""),
        )
        # Never give a background send the live mutable message/session object.
        snapshot = SimpleNamespace(
            unified_msg_origin=origin, message_obj=message_obj,
            get_group_id=lambda: group, get_sender_id=lambda: sender,
            get_message_str=lambda: user_message,
        )
        turn = EmojiTurn(event, companion, origin, route, snapshot, copy.deepcopy(cfg),
                         self._latency_settings(companion), self._latency_style(), copy.deepcopy(candidates))
        turn.pool = copy.deepcopy(pool)
        event.set_extra(LATENCY_EMOJI_KEY, turn)
        self._latency_emoji_turns[route] = turn
        turn.previous_cancel = event.get_extra("qqbot_cancel_reply_preparation")
        async def cancel():
            self._finish_latency_turn(turn)
            if callable(turn.previous_cancel):
                await turn.previous_cancel()
        turn.cancel_callback = cancel
        event.set_extra("qqbot_cancel_reply_preparation", cancel)
        turn.owner = asyncio.current_task()
        def owner_done(_):
            if not turn.confirmed:
                self._finish_latency_turn(turn)
        turn.owner_done = owner_done
        if turn.owner:
            turn.owner.add_done_callback(owner_done)
        try:
            # This synchronous call runs at priority 20, before the companion's
            # priority -100 hook, and supplies candidates to the ONE shared task.
            turn.prediction = companion.prepare_reply_prediction(event, candidates=copy.deepcopy(candidates))
            if turn.prediction is None:
                self._finish_latency_turn(turn)
            else:
                turn.prepare = self._own_latency_task(self._prepare_latency_emoji(turn), "gallery-emoji-prepare")
        except Exception as exc:
            logger.warning("smart_image latency emoji start failed: %s", type(exc).__name__)
            self._finish_latency_turn(turn)
        return True

    def _valid_latency_turn(self, turn):
        return (not turn.closed and not self._latency_emoji_closed
                and self._latency_emoji_turns.get(turn.route) is turn
                and self._latency_companion() is turn.companion
                and self._proactive_emoji_config() == turn.cfg
                and self._latency_settings(turn.companion) == turn.settings
                and self._latency_style() == turn.style
                and not turn.event.get_extra(SKIP_PROACTIVE_EMOJI_EXTRA_KEY, False)
                and not getattr(turn.event, "is_stopped", lambda: False)())

    def _current_latency_image(self, turn):
        if not turn.item or not self._valid_latency_turn(turn):
            return False
        current = next((item for item in self._proactive_emoji_candidates(bool(turn.cfg["meme_only"]))
                        if str(item.get("id")) == str(turn.item.get("id"))), None)
        if current is None or _identity(current) != _identity(turn.item):
            return False
        try:
            path = self._abs_plugin_data_path(current["rel_path"])
            stat = path.stat()
            return path.is_file() and (stat.st_mtime_ns, stat.st_size) == turn.signature
        except OSError:
            return False

    def _latency_choice_fits(self, turn, text=None):
        return bool(turn.item and compatible_with_reply(
            turn.text if text is None else text, turn.character, turn.item,
            semantic_match=turn.selection_source == "prediction"))

    def _latency_reply_preview(self, event, response):
        turn = event.get_extra(LATENCY_EMOJI_KEY)
        if (not isinstance(turn, EmojiTurn) or turn.closed or response is None
                or getattr(response, "tools_call_name", None)):
            return
        chain = getattr(response, "result_chain", None)
        if chain and any(isinstance(part, Image) for part in chain.chain):
            return
        turn.preview_text = (_plain(chain) or str(getattr(response, "completion_text", "") or "")).strip()
        turn.character = copy.deepcopy(event.get_extra("qqbot_character") or {})
        if turn.preview_text:
            turn.preview_ready.set()

    def _choose_latency_image(self, turn):
        prediction = turn.companion.reply_prediction(turn.event)
        text = turn.text or turn.preview_text
        if isinstance(prediction, dict) and prediction.get("risk") in {"low", "high", "unknown"}:
            ids = prediction.get("image_ids", [])
            if isinstance(ids, list):
                for image_id in ids:
                    selected = next((v for v in turn.candidates if isinstance(image_id, str)
                                     and str(v["id"]) == image_id), None)
                    if selected and (not text or compatible_with_reply(
                            text, turn.character, selected, semantic_match=True)):
                        return selected, "prediction"
        if text:
            # Rank against what the dragon actually says, across the full local
            # library. This also recovers a slow/unavailable input prediction.
            for selected in local_candidates(text, turn.pool):
                if (selected.get("proactive_fast_fallback")
                        and compatible_with_reply(text, turn.character, selected)):
                    return selected, "local_reply"
        return None, ""

    async def _prepare_latency_emoji(self, turn):
        # Shield the shared review prediction; only our waiter is cancellable.
        prediction_wait = asyncio.shield(turn.prediction)
        preview_wait = asyncio.create_task(turn.preview_ready.wait())
        final_wait = asyncio.create_task(turn.final_ready.wait())
        try:
            while self._valid_latency_turn(turn):
                selected, source = self._choose_latency_image(turn)
                if selected:
                    turn.item, turn.selection_source = selected, source
                    path = self._abs_plugin_data_path(selected["rel_path"])
                    stat = path.stat()
                    turn.signature = stat.st_mtime_ns, stat.st_size
                    if not self._current_latency_image(turn):
                        return
                    turn.conversion = self._own_latency_task(
                        self._prepare_send_image_path(path, self._tags_from_item(selected)), "gallery-emoji-convert",
                    )
                    self._latency_emoji_conversions.add(turn.conversion)
                    def converted(task):
                        self._latency_emoji_conversions.discard(task)
                        if turn.closed:
                            self._cleanup_latency_paths(turn)
                    turn.conversion.add_done_callback(converted)
                    turn.path, turn.cleanup = await asyncio.shield(turn.conversion)
                    if not self._current_latency_image(turn):
                        turn.path = None
                        return
                    if turn.text and not self._latency_choice_fits(turn):
                        self._clear_latency_choice(turn)
                        continue
                    self._record_latency_emoji("selected_" + source, turn.companion)
                    return
                waits = [task for task in (prediction_wait, preview_wait, final_wait) if not task.done()]
                if not waits or (turn.final_ready.is_set() and prediction_wait.done()):
                    self._record_latency_emoji("no_match", turn.companion)
                    return
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            turn.path = None
            self._record_latency_emoji("preparation_failed", turn.companion)
            logger.debug("smart_image latency emoji preparation unavailable: %s", type(exc).__name__)
        finally:
            for task in (prediction_wait, preview_wait, final_wait):
                if not task.done(): task.cancel()
            await asyncio.gather(prediction_wait, preview_wait, final_wait, return_exceptions=True)
            if turn.prepare is asyncio.current_task() and (turn.closed or turn.path is None):
                self._finish_latency_turn(turn)

    def _clear_latency_choice(self, turn):
        self._cleanup_latency_paths(turn)
        turn.item = turn.path = turn.signature = turn.conversion = None
        turn.selection_source = ""

    def _reselect_latency_choice(self, turn):
        self._clear_latency_choice(turn)
        if turn.prepare and not turn.prepare.done():
            turn.prepare.cancel()
        turn.prepare = self._own_latency_task(self._prepare_latency_emoji(turn), "gallery-emoji-final-choice")

    def _observe_latency_text_send(self, turn):
        event = turn.event
        original = event.send
        own_send = getattr(event, "__dict__", {}).get("send")
        async def observed(message, *args, **kwargs):
            from ...astrbot_plugin_proactive_agent.conversation_review import attachment_policy, sent_text, text_slots
            from ...astrbot_plugin_proactive_agent.route_guard import canonical, destination

            expected=event.get_extra('qqbot_voice_result_text') or _plain(event.get_result())
            if turn.embedded and (turn.failed or not self._current_latency_image(turn)
                    or not self._latency_choice_fits(turn, expected)):
                self._drop_latency_attachment(turn, message)
                self._finish_latency_turn(turn, "changed_before_send")
            from ...astrbot_plugin_proactive_agent.voice_memory import component_body
            text = component_body(message)
            has_voice=any(isinstance(p,Record) and p.text for p in message.chain)
            receipt_original = "\n".join(part.text for part in message.chain if isinstance(part, Plain) and part.text)
            images = [part for part in message.chain if isinstance(part, Image)]
            attachment_index = next((i for i, part in enumerate(images) if part is turn.attachment), None)

            def check_attachment(action, payload):
                # The transport reviewer may rewrite formatted text after the
                # result hook. Validate the actual payload before sending the
                # combined image too. The adapter preserves image order.
                if (not turn.embedded or attachment_index is None
                        or action.removesuffix("_async") not in {"send_msg", "send_private_msg", "send_group_msg"}
                        or canonical(destination(action, payload, turn.origin.split(":", 1)[0])) != canonical(turn.origin)):
                    return
                parts = payload.get("message", [])
                if not isinstance(parts, list):
                    return
                checked = "\n".join(container[key] for container, key in text_slots(parts) if container[key])
                if not checked and event.get_extra('qqbot_voice_result_text'):
                    checked=''.join(turn.sent_parts)
                if self._current_latency_image(turn) and self._latency_choice_fits(turn, checked):
                    return
                indices = [i for i, part in enumerate(parts) if isinstance(part, dict) and part.get("type") == "image"]
                if len(indices) == len(images):
                    del parts[indices[attachment_index]]
                    self._drop_latency_attachment(turn, message)
                    turn.embedded = False

            turn.sending += 1
            try:
                with attachment_policy(check_attachment):
                    outcome = await original(message, *args, **kwargs)
            except BaseException:
                if text:
                    turn.failed = True
                    self._finish_latency_turn(turn, "body_failed")
                raise
            finally:
                turn.sending -= 1
                if turn.closed:
                    self._cleanup_latency_paths(turn)
            if text:
                if outcome is False:
                    turn.failed = True
                    self._finish_latency_turn(turn, "body_failed")
                else:
                    actual = text if has_voice else sent_text(":".join(canonical(turn.origin)), receipt_original)
                    # Without an outbound receipt use the original component
                    # text, not the newline-normalized review lookup string.
                    turn.sent_parts.append(actual if actual != receipt_original else text)
                    turn.sent_original_parts.append(text)
                    turn.sent_at = asyncio.get_running_loop().time()
            return outcome
        def restore():
            if event.send is observed:
                if own_send is None:
                    delattr(event, "send")
                else:
                    event.send = own_send
        turn.restore_send = restore
        event.send = observed

    def _decorate_latency_emoji(self, event):
        # Commands and direct plugin replies may never enter on_llm_request.
        self._advance_latency_turn(event)
        turn = event.get_extra(LATENCY_EMOJI_KEY)
        if not isinstance(turn, EmojiTurn):
            return turn is not None or self._latency_companion() is not None
        if turn.closed or turn.decorated:
            return True
        if event.get_extra("qqbot_agent_final_done", False) is not True:
            return True  # A tool-loop LLM_RESULT can still be an intermediate sentence.
        result = event.get_result()
        if (not self._valid_latency_turn(turn) or not isinstance(result, MessageEventResult)
                or not result.is_llm_result() or not _plain(result).strip()
                or any(isinstance(part, Image) for part in result.chain)):
            self._finish_latency_turn(turn)
            return True
        turn.text = _plain(result)
        turn.result = result
        turn.character = copy.deepcopy(event.get_extra("qqbot_character") or {})
        turn.decorated = True
        turn.final_ready.set()
        self._observe_latency_text_send(turn)
        # No yield, await, file conversion or model call on the decoration path.
        if turn.path is not None and self._current_latency_image(turn):
            if not self._latency_choice_fits(turn):
                # The reviewed reply changed emotion: reselect locally from the
                # final text instead of losing the entire probability hit.
                self._reselect_latency_choice(turn)
            else:
                turn.ready_at_decoration = True
                if self._proactive_emoji_embed(turn.cfg):
                    turn.attachment = Image.fromFileSystem(str(turn.path))
                    result.chain.append(turn.attachment)
                    turn.embedded = True
        return True

    def _after_latency_text_sent(self, event):
        turn = event.get_extra(LATENCY_EMOJI_KEY)
        if not isinstance(turn, EmojiTurn):
            return turn is not None
        if turn.closed or turn.confirmed:
            return True
        if not turn.decorated:
            return True  # Intermediate sends do not consume the final emoji slot.
        if turn.restore_send:
            turn.restore_send()
        # AstrBot may call this hook even after swallowing a send exception.
        sent_text = "".join(turn.sent_parts)
        if (turn.failed or not turn.decorated or not sent_text.strip()
                or "".join(turn.sent_original_parts).strip() != (event.get_extra('qqbot_voice_result_text') or _plain(event.get_result())).strip()
                or not self._valid_latency_turn(turn)):
            self._finish_latency_turn(turn)
            return True
        turn.confirmed = True
        # Formatting may split or clean punctuation after decorating. Validate
        # the complete text actually sent, rather than an earlier draft.
        turn.text = sent_text
        if turn.path is not None and not turn.embedded and not self._latency_choice_fits(turn):
            self._reselect_latency_choice(turn)
        turn.delivery = self._own_latency_task(self._deliver_latency_emoji(turn), "gallery-emoji-delivery")
        return True

    async def _deliver_latency_emoji(self, turn):
        try:
            if not turn.embedded:
                grace = float(turn.settings["emoji_late_grace_seconds"])
                if not math.isfinite(grace):
                    grace = LATE_GRACE_DEFAULT
                if turn.ready_at_decoration:
                    # Setting late grace to zero disables waiting, not the
                    # original ready-image separate-send probability branch.
                    grace = max(grace, LATE_GRACE_DEFAULT)
                deadline = turn.sent_at + max(0.0, min(grace, 12.0))
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self._record_latency_emoji("expired", turn.companion)
                    return
                async with asyncio.timeout(remaining):
                    await asyncio.shield(turn.prepare)
                if (not turn.path or not self._current_latency_image(turn)
                        or not self._latency_choice_fits(turn)
                        or asyncio.get_running_loop().time() >= deadline):
                    return
                # The grace deadline bounds preparation, never a submitted platform receipt.
                from ...astrbot_plugin_proactive_agent.route_guard import bind_route
                bind_route(turn.origin, turn.snapshot.get_sender_id(),
                           getattr(turn.snapshot.message_obj, "message_id", ""),bool(turn.snapshot.get_group_id()))
                turn.sending += 1
                sending=getattr(self,'_latency_emoji_sending',None)
                if sending is None:sending=self._latency_emoji_sending=set()
                task=asyncio.current_task();sending.add(task)
                try:
                    from data.plugins.astrbot_plugin_proactive_agent.delivery_ledger import optional_attachment
                    with optional_attachment():
                        sent = await self.context.send_message(turn.origin, MessageEventResult().file_image(str(turn.path)))
                except asyncio.CancelledError:raise
                except Exception:
                    self._record_latency_emoji("image_send_failed", turn.companion)
                    return
                finally:
                    sending.discard(task);turn.sending -= 1
                    if turn.closed:self._cleanup_latency_paths(turn)
                if sent is False:
                    self._record_latency_emoji("image_send_failed", turn.companion)
                    return
            elif not self._current_latency_image(turn):
                return
            self._record_latency_emoji("sent_combined" if turn.embedded else "sent_separate", turn.companion)
            await self._after_plugin_sent_image_for_meme_combat(
                turn.snapshot, str(self._abs_plugin_data_path(turn.item["rel_path"])),
                source="proactive_emoji", allow_burst=False,
            )
        except TimeoutError:
            self._record_latency_emoji("expired", turn.companion)
        except ValueError:
            self._record_latency_emoji("preparation_failed", turn.companion)
        finally:
            self._finish_latency_turn(turn)

    def _finish_latency_turn(self, turn, reason=None):
        if turn.sending and reason=='superseded':
            # A newer reply invalidates unsent preparation, not an HTTP send in flight.
            # The receipt collector owns final cleanup and delivery truth.
            return
        if reason and not turn.closed:
            self._record_latency_emoji(reason, turn.companion)
        turn.closed = True
        current = asyncio.current_task()
        for task in (turn.prepare, turn.delivery):
            if task and task is not current and not task.done():
                task.cancel()
        if turn.restore_send:
            turn.restore_send()
        if turn.owner and turn.owner_done:
            turn.owner.remove_done_callback(turn.owner_done)
        if turn.event.get_extra("qqbot_cancel_reply_preparation") is turn.cancel_callback:
            turn.event.set_extra("qqbot_cancel_reply_preparation", turn.previous_cancel)
        if self._latency_emoji_turns.get(turn.route) is turn:
            self._latency_emoji_turns.pop(turn.route, None)
        if not turn.confirmed:
            self._drop_latency_attachment(turn)
        self._cleanup_latency_paths(turn)

    def _drop_latency_attachment(self, turn, outgoing=None):
        if turn.attachment is not None:
            for result in (turn.result, outgoing):
                if result is not None:
                    result.chain[:] = [part for part in result.chain if part is not turn.attachment]

    def _cleanup_latency_paths(self, turn):
        if turn.sending:
            return  # A transport may still be reading the prepared file.
        ledger=getattr(turn.companion,'_delivery',None)
        job=turn.event.get_extra('qqbot_reply_job')
        pending=[task for task in getattr(ledger,'receipt_tasks',()) if not task.done()
                 and job and task.get_name() in {'receipt-'+id_ for id_ in job.sends}]
        if pending:
            watched=getattr(turn,'watched_receipts',set())
            for task in pending:
                if task not in watched:
                    watched.add(task);task.add_done_callback(lambda _,turn=turn:self._cleanup_latency_paths(turn))
            turn.watched_receipts=watched
            return
        paths = list(turn.cleanup)
        if turn.conversion and turn.conversion.done() and not turn.conversion.cancelled():
            try:
                paths.extend(turn.conversion.result()[1])
            except Exception:
                pass
        self._cleanup_temp_paths(list(dict.fromkeys(paths)))
        turn.cleanup = []

    async def _terminate_latency_emoji(self):
        sending=[t for t in getattr(self,'_latency_emoji_sending',()) if not t.done()]
        if sending:
            _,pending=await asyncio.wait(sending,timeout=8)
            if pending:raise RuntimeError('表情包发送回执仍在途中，已中止卸载')
        self._latency_emoji_closed = True
        for turn in list(getattr(self, "_latency_emoji_turns", {}).values()):
            self._finish_latency_turn(turn)
        tasks = list(getattr(self, "_latency_emoji_tasks", ()))
        for task in tasks:
            # to_thread conversion cannot be stopped halfway. Let it return so
            # its temporary paths can be cleaned, even during plugin shutdown.
            if task not in getattr(self, "_latency_emoji_conversions", ()):
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
