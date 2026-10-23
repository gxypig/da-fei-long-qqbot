"""Adapt proxy image completions before AstrBot's empty-output validation.

Images stay in their original LLMResponse: no log scraping, extra generation,
global recipient, or out-of-band delivery. Uninstall restores the provider.
"""
from __future__ import annotations

import base64
from functools import wraps


def image_refs(message) -> list[str]:
    raw = getattr(message, "images", None) or []
    if isinstance(raw, dict):
        raw = [raw]
    content = getattr(message, "content", None)
    if isinstance(content, list):
        raw = list(raw) + [p for p in content if isinstance(p, dict) and p.get("type") in {"image_url", "image"}]
    refs = []
    for item in raw:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if isinstance(item, str):
            ref = item
        elif isinstance(item, dict):
            value = item.get("image_url") or item.get("url") or ""
            ref = value.get("url", "") if isinstance(value, dict) else value
            if not ref and item.get("b64_json"):
                ref = "data:image/png;base64," + item["b64_json"]
        else:
            continue
        if not isinstance(ref, str):
            continue
        if ref.startswith("data:image/"):
            try:
                header, payload = ref.split(",", 1)
                if not header.endswith(";base64") or not base64.b64decode(payload, validate=True):
                    continue
            except (ValueError, TypeError):
                continue
        elif not ref.startswith(("https://", "http://")):
            continue
        if ref not in refs:
            refs.append(ref)
    return refs[:4]


def install_image_compat():
    from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image

    original = ProviderOpenAIOfficial._parse_openai_completion

    @wraps(original)
    async def parse(provider, completion, tools):
        choices = getattr(completion, "choices", None) or []
        if not choices:
            return await original(provider, completion, tools)
        choice = choices[0]
        message = choice.message
        # Preserve refusal/content-filter behavior and all text/tool parsing.
        refs = image_refs(message)
        if not refs or choice.finish_reason == "content_filter" or getattr(message, "refusal", None):
            return await original(provider, completion, tools)
        compatible = completion.model_copy(deep=True)
        if not provider._normalize_content(message.content):
            compatible.choices[0].message.content = "图片已生成。"
        response = await original(provider, compatible, tools)
        if response.result_chain is None:
            response.result_chain = MessageChain()
        response.result_chain.chain.extend(Image(file=ref) for ref in refs)
        response.raw_completion = completion
        response.qqbot_generated_image = True
        return response

    ProviderOpenAIOfficial._parse_openai_completion = parse

    def restore():
        if ProviderOpenAIOfficial._parse_openai_completion is parse:
            ProviderOpenAIOfficial._parse_openai_completion = original

    return restore
