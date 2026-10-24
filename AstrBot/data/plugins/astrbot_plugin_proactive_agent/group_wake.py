"""Additional QQ group wake rules; preserve AstrBot's permission/filter pipeline."""
import random
import re
import time

from astrbot.api.message_components import At, Plain
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.star.session_plugin_manager import SessionPluginManager


def group_wake_reason(event, config, cfg, roll=random.random):
    if (not event.get_group_id() or str(event.get_sender_id()) == str(event.get_self_id())
            or event.get_platform_id() != cfg("platform_id", "aiocqhttp")):
        return None
    parts = event.get_messages()
    # Only the current message's plain text; quoted messages/URLs don't become commands.
    text = "".join(p.text for p in parts if isinstance(p, Plain))
    if any(isinstance(p, At) and str(p.qq) == str(event.get_self_id()) for p in parts):
        return "at"
    prefixes = config.get("wake_prefix", [])
    words = re.split(r"[,，\s]+", str(cfg("group_wake_words", "大肥龙,肥龙")))
    if cfg("group_wake_anywhere", True):
        words += [p for p in prefixes if isinstance(p, str) and any(c.isalnum() for c in p)]
        if any(word and word in text for word in words):
            return "keyword"
    elif any(word and text.lstrip().startswith(word) for word in words):
        return "keyword"
    # Existing explicit prefix/mention/reply rules remain AstrBot's responsibility.
    if any(text.lstrip().startswith(p) for p in prefixes):
        return None
    groups = set(re.split(r"[,，\s]+", str(cfg("group_chat_ids", ""))))
    if str(event.get_group_id()) not in groups or not text.strip():
        return None
    try:
        probability = max(0, min(100, int(cfg("group_chat_probability", 10))))
    except (TypeError, ValueError):
        probability = 0
    return "probability" if probability > 0 and roll() < probability / 100 else None


def install_group_wake(plugin):
    previous = WakingCheckStage.process

    async def process(stage, event):
        if event.get_platform_id() == plugin._cfg('platform_id', 'aiocqhttp') and event.get_extra('qqbot_received_at') is None:
            event.set_extra('qqbot_received_at', time.monotonic())
        enabled = stage.ctx.astrbot_config.get("plugin_set", ["*"])
        if (enabled == ["*"] or "astrbot_plugin_proactive_agent" in enabled) and await SessionPluginManager.is_plugin_enabled_for_session(event.unified_msg_origin, "astrbot_plugin_proactive_agent"):
            if hasattr(plugin, '_live_journal') and event.get_platform_id() == plugin._cfg('platform_id', 'aiocqhttp'):
                try:
                    plugin._live_journal.incoming(event)
                except Exception as exc:
                    plugin._live_journal.error = type(exc).__name__
            if hasattr(plugin, '_group_flow'):
                plugin._group_flow.observe(event)
            reason = group_wake_reason(event, stage.ctx.astrbot_config, plugin._cfg)
            if reason:
                event.is_at_or_wake_command = True
                event.is_wake = True
                event.set_extra("qqbot_group_wake", reason)
        # Role assignment, command parsing, deny rules and every plugin still run.
        return await previous(stage, event)

    WakingCheckStage.process = process

    def restore():
        if WakingCheckStage.process is process:
            WakingCheckStage.process = previous
    return restore
