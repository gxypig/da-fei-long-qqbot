"""Task-local recipient invariants, independent of optional model review."""
from contextlib import contextmanager
from contextvars import ContextVar
from aiocqhttp import CQHttp, Message

_route = ContextVar('qqbot_recipient', default=None)
SEND_ACTIONS = {'send_msg', 'send_private_msg', 'send_group_msg', 'send_group_forward_msg',
                'send_private_forward_msg', 'send_forward_msg'}


def bind_route(origin, sender='', message_id='', group=False):
    _route.set({'origin': origin, 'sender': str(sender), 'message_id': str(message_id or ''), 'group': group})


@contextmanager
def explicit_route(origin):
    """Only trusted code (scheduler/admin command), never an LLM parameter."""
    token = _route.set({'origin': origin, 'sender': '', 'message_id': '', 'group': False})
    try:
        yield
    finally:
        _route.reset(token)


def destination(action, params, platform):
    group = params.get('message_type') == 'group' or 'group' in action or bool(params.get('group_id'))
    target = params.get('group_id') if group else params.get('user_id')
    return f"{platform}:{'GroupMessage' if group else 'FriendMessage'}:{target}"


def canonical(origin):
    platform, kind, target = origin.split(':', 2)
    # AstrBot's unique group sessions encode sender_group. The physical transport
    # must still be the same group, never another private chat.
    if kind == 'GroupMessage':
        target = target.rsplit('_', 1)[-1]
    return platform, kind, target


def install_route_guard(plugin):
    previous = CQHttp.call_action

    async def guarded(bot, action, **params):
        platform = plugin._platform()
        scope = _route.get()
        if (scope and platform is not None and getattr(platform, 'bot', None) is bot
                and action.removesuffix('_async') in SEND_ACTIONS):
            actual = destination(action, params, plugin._cfg('platform_id', 'aiocqhttp'))
            if canonical(actual) != canonical(scope['origin']):
                raise RuntimeError('会话收件人与发送目标不一致，已阻止误投')
            if scope['group'] and scope['message_id'] and action.removesuffix('_async') in {'send_msg', 'send_group_msg'}:
                message = params.get('message', [])
                original_message = message
                if isinstance(message, str):
                    message = ([{'type': 'text', 'data': {'text': message}}] if params.get('auto_escape') else list(Message(message)))
                if isinstance(message, list):
                    enabled = plugin._cfg('auto_quote_enabled', False)
                    # This switch owns only the automatic quote of the triggering
                    # message; retain deliberate references to other messages.
                    message = [p for p in message if not (isinstance(p, dict) and p.get('type') == 'reply'
                               and str(p.get('data', {}).get('id')) == scope['message_id'])]
                    if (enabled and not scope.get('quoted') and not any(p.get('type') == 'reply' for p in message)
                            and any(p.get('type') == 'text' and p.get('data', {}).get('text', '').strip() for p in message)):
                        message = [{'type': 'reply', 'data': {'id': scope['message_id']}}] + message
                    if enabled or any(p.get('type') == 'reply' for p in (list(Message(original_message)) if isinstance(original_message, str) else original_message)):
                        params = {**params, 'message': message}
                    result = await previous(bot, action, **params)
                    if any(p.get('type') == 'reply' for p in message):
                        scope['quoted'] = True
                    return result
        return await previous(bot, action, **params)

    CQHttp.call_action = guarded

    def restore():
        if CQHttp.call_action is guarded:
            CQHttp.call_action = previous
    return restore
