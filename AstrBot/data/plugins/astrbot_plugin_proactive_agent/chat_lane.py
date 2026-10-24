"""Fast reasoning only for explicitly tagged simple chat; preserve provider state/tools."""
import copy
import re

MARKER = '[QQBOT_SIMPLE_CHAT_LOW_LATENCY_V1]'


def simple_chat(text, state):
    return (0 < len(text) <= 160 and state.get('mood') not in {'serious', 'stop'}
            and state.get('visual_intent', 'none') == 'none'
            and not re.search(r'搜索|查询|查一下|找.*视频|画|生成|拍|代码|编程|数学|计算|证明|分析|解释|比较|部署|设置|开启|关闭|发给|记住|忘记|提醒|定时|https?://', text))


_REASONING_KEYS = {'reasoning', 'reasoning_effort', 'think', 'thinking'}
_SUPPORTED = re.compile(r'gpt-(?:5\.[1-9]|6(?:\.[0-9]+)?)(?:[-.][a-zA-Z0-9.-]+)?')


def _marked_payload(payloads):
    """Consume only our trusted system marker, without touching stored history."""
    marked = False
    messages = []
    for message in payloads.get('messages', []):
        message = dict(message)
        if message.get('role') in {'system', 'developer'}:
            content = message.get('content')
            if isinstance(content, str):
                marked = marked or MARKER in content
                message['content'] = content.replace(MARKER, '')
            elif isinstance(content, list):
                parts = []
                for part in content:
                    part = dict(part)
                    if part.get('type') in {'text', 'input_text'} and isinstance(part.get('text'), str):
                        marked = marked or MARKER in part['text']
                        part['text'] = part['text'].replace(MARKER, '')
                    parts.append(part)
                message['content'] = parts
        messages.append(message)
    return {**payloads, 'messages': messages}, marked


def _current_text_turn(messages):
    """Old images remain available; a new image/tool task keeps full reasoning."""
    index = next((i for i in range(len(messages)-1, -1, -1)
                  if messages[i].get('role') == 'user'), None)
    if index is None:
        return False
    if any(m.get('role') == 'tool' or m.get('tool_calls') for m in messages[index+1:]):
        return False
    content = messages[index].get('content')
    return (isinstance(content, str) or (isinstance(content, list) and bool(content)
            and all(isinstance(p, dict) and p.get('type') in {'text', 'input_text'} for p in content)))


def install_chat_lane(cfg, audit=None):
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    previous = ProviderOpenAIOfficial._query
    previous_stream = ProviderOpenAIOfficial._query_stream
    unsupported = set()

    def prepare(provider, payloads):
        config = provider.provider_config
        extra = config.get('custom_extra_body') or {}
        cleaned, marked = _marked_payload(payloads)
        model = str(payloads.get('model') or config.get('model') or '')
        # Scheduler facades are copied per call, so object identity cannot cache
        # a provider's definitive unsupported-parameter response.
        key = (str(config.get('provider_source_id') or config.get('api_base') or ''), model)
        nested = payloads.get('extra_body') or {}
        eligible = (cfg('chat_low_latency', False) and marked
                    and _SUPPORTED.fullmatch(model)
                    and isinstance(extra, dict)
                    and not _REASONING_KEYS.intersection(extra)
                    and not _REASONING_KEYS.intersection(payloads)
                    and isinstance(nested, dict) and not _REASONING_KEYS.intersection(nested)
                    and _current_text_turn(cleaned.get('messages', []))
                    and key not in unsupported)
        if not eligible:
            return provider, cleaned, key
        lane = copy.copy(provider)
        lane.provider_config = {**config, 'custom_extra_body': {**extra, 'reasoning_effort': 'low'}}
        if audit:
            try:
                audit('chat_reasoning_policy', {'mode': 'low', 'model': model})
            except Exception:
                pass
        return lane, cleaned, key

    def rejected(exc):
        return (getattr(exc, 'status_code', None) in (400, 422)
                and re.search(r'reasoning[_ .-]?effort', str(exc), re.I))

    async def query(provider, payloads, tools, **kwargs):
        lane, cleaned, key = prepare(provider, payloads)
        try:
            return await previous(lane, copy.deepcopy(cleaned), tools, **kwargs)
        except Exception as exc:
            if lane is provider or not rejected(exc):
                raise
            unsupported.add(key)
            return await previous(provider, copy.deepcopy(cleaned), tools, **kwargs)

    async def query_stream(provider, payloads, tools, **kwargs):
        lane, cleaned, key = prepare(provider, payloads)
        emitted = False
        try:
            async for response in previous_stream(lane, copy.deepcopy(cleaned), tools, **kwargs):
                emitted = True
                yield response
        except Exception as exc:
            if emitted or lane is provider or not rejected(exc):
                raise
            unsupported.add(key)
            async for response in previous_stream(provider, copy.deepcopy(cleaned), tools, **kwargs):
                yield response

    ProviderOpenAIOfficial._query = query
    ProviderOpenAIOfficial._query_stream = query_stream
    def restore():
        if ProviderOpenAIOfficial._query is query:
            ProviderOpenAIOfficial._query = previous
        if ProviderOpenAIOfficial._query_stream is query_stream:
            ProviderOpenAIOfficial._query_stream = previous_stream
    return restore
