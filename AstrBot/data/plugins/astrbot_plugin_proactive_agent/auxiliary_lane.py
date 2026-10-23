"""Per-call auxiliary reasoning hint; never mutate the conversation provider.

All detection, learning, review and independent rewrite rechecks still execute.
Unknown providers/models and explicit user overrides retain their original path.
"""
import copy
import re


class AuxiliaryLane:
    def __init__(self):
        self.unsupported = set()
        self.stats = {'optimized': 0, 'original': 0, 'compatibility_fallback': 0}

    async def generate(self, provider, **kwargs):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        config = getattr(provider, 'provider_config', {})
        extra = config.get('custom_extra_body') or {}
        model = str(config.get('model') or '')
        key = (id(provider), model)
        supported = (isinstance(provider, ProviderOpenAIOfficial)
                     and re.fullmatch(r'gpt-(?:5\.[1-9]|6(?:\.[0-9]+)?)(?:[-.][a-zA-Z0-9.-]+)?', model)
                     and not any(k in extra for k in ('reasoning', 'reasoning_effort', 'think', 'thinking'))
                     and key not in self.unsupported)
        if not supported:
            self.stats['original'] += 1
            return await provider.text_chat(**kwargs)
        # A shallow facade shares the established HTTP pool and image compatibility
        # implementation. Only its config is isolated; no provider/global config edit.
        lane = copy.copy(provider)
        lane.provider_config = {**config, 'custom_extra_body': {**extra, 'reasoning_effort': 'low'}}
        try:
            result = await lane.text_chat(**kwargs)
            self.stats['optimized'] += 1
            return result
        except Exception as exc:
            # Only a definitive unsupported-parameter rejection is retried. Never
            # double-submit timeouts, rate limits or uncertain upstream failures.
            status = getattr(exc, 'status_code', None)
            if status not in (400, 422) or not re.search(r'reasoning[_ .-]?effort', str(exc), re.I):
                raise
            self.unsupported.add(key)
            self.stats['compatibility_fallback'] += 1
            return await provider.text_chat(**kwargs)
