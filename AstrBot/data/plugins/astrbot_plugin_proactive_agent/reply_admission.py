"""Cheap empty-input admission checks; observers may still create real requests.

Mirror the framework's content gate, not its wake/permission policy. Unknown
plugin handlers remain conservative: they may reply to non-text input.
"""
from astrbot.api.message_components import At, File, Image, Plain, Record, Reply, Video
from astrbot.core.star.filter.command import CommandFilter


def has_agent_content(event):
    if not callable(getattr(event,'get_message_str',None)) or not callable(getattr(event,'get_messages',None)):
        # Third-party event shims with unknown content semantics stay admitted.
        return True
    return (event.get_extra('provider_request') is not None
            or bool((event.get_message_str() or '').strip())
            or any(isinstance(p, (Image, File, Record, Video, Reply)) for p in event.get_messages()))


def is_command(event):
    return any(isinstance(f, CommandFilter)
               for h in event.get_extra('activated_handlers') or [] for f in h.event_filters)


def initial_admission(stage, event):
    if is_command(event):
        return 'command'
    if event.get_extra('provider_request') is not None:
        return 'provider_request'
    if not getattr(event, 'is_at_or_wake_command', False):
        return None
    if has_agent_content(event):
        return 'wake_content'
    config = getattr(getattr(stage, 'ctx', None), 'astrbot_config', {})
    for handler in event.get_extra('activated_handlers') or []:
        module = getattr(handler, 'handler_module_path', '')
        name = getattr(handler, 'handler_name', '')
        if module == 'data.plugins.astrbot_plugin_proactive_agent.main' and name == 'track_session':
            continue
        if module == 'astrbot.builtin_stars.astrbot.main':
            if name in {'handle_session_control_agent', 'persist_group_message', 'on_message'}:
                continue
            if name == 'handle_empty_mention':
                parts = event.get_messages()
                settings = config.get('platform_settings', {})
                empty_mention = (len(parts) == 1 and isinstance(parts[0], At)
                                 and str(parts[0].qq) == str(event.get_self_id())
                                 and settings.get('empty_mention_waiting', True))
                prefix_only = (len(parts) == 1 and isinstance(parts[0], Plain)
                               and parts[0].text.strip() in config.get('wake_prefix', []))
                if (empty_mention or prefix_only) and settings.get('empty_mention_waiting_need_reply', True):
                    return 'empty_mention_reply'
                continue
        return 'plugin_handler'
    return None
