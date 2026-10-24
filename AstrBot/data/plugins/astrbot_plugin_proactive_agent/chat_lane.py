"""Fast reasoning only for explicitly tagged simple chat; preserve provider state/tools."""
import copy
import re

MARKER = '[QQBOT_SIMPLE_CHAT_LOW_LATENCY_V1]'


def simple_chat(text, state):
    return (0 < len(text) <= 160 and state.get('mood') not in {'serious', 'stop'}
            and state.get('visual_intent', 'none') == 'none'
            and not re.search(r'搜索|查询|查一下|找.*视频|画|生成|拍|代码|编程|数学|计算|证明|分析|解释|比较|部署|设置|开启|关闭|发给|记住|忘记|提醒|定时|https?://', text))


def install_chat_lane(cfg):
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    previous = ProviderOpenAIOfficial._query
    unsupported = set()

    async def query(provider, payloads, tools, **kwargs):
        config = provider.provider_config
        extra = config.get('custom_extra_body') or {}
        messages = payloads.get('messages', [])
        system = '\n'.join(m.get('content', '') for m in messages
                           if m.get('role') in {'system', 'developer'} and isinstance(m.get('content'), str))
        model = str(payloads.get('model') or config.get('model') or '')
        key = (id(provider), model)
        eligible = (cfg('chat_low_latency', False) and MARKER in system
                    and re.fullmatch(r'gpt-5\.[1-9](?:[-.][a-zA-Z0-9.-]+)?', model)
                    and isinstance(extra, dict)
                    and not any(k in extra for k in ('reasoning', 'reasoning_effort', 'think', 'thinking'))
                    and not any(k in payloads for k in ('reasoning', 'reasoning_effort'))
                    and not any(isinstance(m.get('content'), list) and any(p.get('type') not in {'text', 'input_text'} for p in m['content'] if isinstance(p, dict)) for m in messages)
                    and key not in unsupported)
        if not eligible:
            return await previous(provider, payloads, tools, **kwargs)
        lane = copy.copy(provider)
        lane.provider_config = {**config, 'custom_extra_body': {**extra, 'reasoning_effort': 'low'}}
        try:
            return await previous(lane, payloads, tools, **kwargs)
        except Exception as exc:
            if getattr(exc, 'status_code', None) not in (400, 422) or not re.search(r'reasoning[_ .-]?effort', str(exc), re.I):
                raise
            unsupported.add(key)
            return await previous(provider, payloads, tools, **kwargs)

    ProviderOpenAIOfficial._query = query
    def restore():
        if ProviderOpenAIOfficial._query is query:
            ProviderOpenAIOfficial._query = previous
    return restore
