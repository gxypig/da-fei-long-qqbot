"""Offline boundary tests for the main-chat reasoning lane; no live inputs or I/O."""

import asyncio
import copy
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# Reuse the existing temporary-directory bootstrap before importing AstrBot.
import test_maintenance  # noqa: F401
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from data.plugins.astrbot_plugin_proactive_agent.chat_lane import (
    MARKER,
    install_chat_lane,
)


class ParameterRejected(Exception):
    def __init__(self, status_code=400, message="unsupported reasoning_effort"):
        super().__init__(message)
        self.status_code = status_code


class OpaqueTools:
    """A registered tool set must retain its handlers and must never be copied."""

    def __init__(self):
        self.schemas = [
            {"type": "function", "function": {
                "name": "offline_search",
                "description": "Synthetic test tool; never executed.",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string"},
                }},
            }},
        ]
        self.handler = object()

    def __deepcopy__(self, memo):
        raise AssertionError("Registered tools must retain their original identity")


def provider(model="gpt-6.1-sol", source="offline-source"):
    result = object.__new__(ProviderOpenAIOfficial)
    result.provider_config = {
        "model": model,
        "provider_source_id": source,
        "api_base": "https://example.invalid/offline",
        "custom_extra_body": {"temperature": 0.7},
    }
    result.client = object()
    return result


def payload(current="Synthetic ordinary chat"):
    return {
        "model": "gpt-6.1-sol",
        "messages": [
            {"role": "system", "content": "Policy\n" + MARKER + "\nTail"},
            {"role": "user", "content": [
                {"type": "text", "text": "Historical visual input"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64,c3ludGhldGlj", "detail": "high",
                }},
                {"type": "image_url", "image_url": {
                    "url": "https://example.invalid/old.jpg", "detail": "auto",
                }},
            ]},
            {"role": "assistant", "content": [
                {"type": "think", "think": "Synthetic historical reasoning",
                 "encrypted": "opaque-test-signature"},
                {"type": "text", "text": "Synthetic historical response"},
            ], "tool_calls": [{
                "id": "offline-call", "type": "function", "function": {
                    "name": "offline_search", "arguments": '{"query":"fixture"}',
                },
            }]},
            {"role": "tool", "tool_call_id": "offline-call",
             "content": '{"result":"synthetic historical tool result"}'},
            {"role": "assistant", "content": "Historical tool task completed"},
            {"role": "user", "content": copy.deepcopy(current)},
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "extra_body": {"metadata": {"purpose": "offline-test"}},
    }


def expected_payload(original):
    """Expected fixture, independent of the production marker-removal helper."""
    result = copy.deepcopy(original)
    result["messages"][0]["content"] = "Policy\n\nTail"
    return result


@contextmanager
def installed(query=None, stream=None, *, enabled=True, audit=None):
    async def unused_query(*args, **kwargs):
        raise AssertionError("Unexpected non-stream request")

    async def unused_stream(*args, **kwargs):
        raise AssertionError("Unexpected stream request")
        yield  # pragma: no cover

    with patch.object(ProviderOpenAIOfficial, "_query", query or unused_query), \
            patch.object(ProviderOpenAIOfficial, "_query_stream", stream or unused_stream):
        restore = install_chat_lane({"chat_low_latency": enabled}.get, audit=audit)
        try:
            yield restore
        finally:
            restore()


class ChatLaneSafety(unittest.IsolatedAsyncioTestCase):
    async def test_history_media_reasoning_tools_and_request_options_are_preserved(self):
        for current in ("Ordinary chat", [{"type": "text", "text": "Ordinary chat"}],
                        [{"type": "input_text", "text": "Ordinary chat"}]):
            with self.subTest(current_type=type(current).__name__):
                original = provider()
                data = payload(current)
                snapshot = copy.deepcopy(data)
                config_snapshot = copy.deepcopy(original.provider_config)
                tools = OpaqueTools()
                tool_snapshot = copy.deepcopy(tools.schemas)
                abort = object()
                options = {"conversation_id": "offline-conversation",
                           "request_max_retries": 5, "abort_signal": abort}
                response = object()

                async def call(lane, forwarded, registered, **kwargs):
                    self.assertIsNot(lane, original)
                    self.assertIs(lane.client, original.client)
                    expected_config = copy.deepcopy(config_snapshot)
                    expected_config["custom_extra_body"]["reasoning_effort"] = "low"
                    self.assertEqual(lane.provider_config, expected_config)
                    self.assertEqual(forwarded, expected_payload(snapshot))
                    self.assertIs(registered, tools)
                    self.assertEqual(registered.schemas, tool_snapshot)
                    self.assertEqual(kwargs, options)
                    self.assertIs(kwargs["abort_signal"], abort)
                    return response

                with installed(call):
                    self.assertIs(await original._query(data, tools, **options), response)
                self.assertEqual(data, snapshot)
                self.assertEqual(original.provider_config, config_snapshot)
                self.assertEqual(tools.schemas, tool_snapshot)

    async def test_current_media_and_tool_followups_keep_original_reasoning(self):
        variants = []
        for media in (
            {"type": "image_url", "image_url": {"url": "https://example.invalid/new.png"}},
            {"type": "input_audio", "input_audio": {"data": "synthetic", "format": "wav"}},
            {"type": "video_url", "video_url": {"url": "https://example.invalid/new.mp4"}},
            {"type": "file", "file": {"file_id": "offline-file"}},
        ):
            variants.append(payload([{"type": "text", "text": "Current media"}, media]))
        for following in (
            [{"role": "tool", "tool_call_id": "new-call", "content": "New result"}],
            [{"role": "assistant", "content": None, "tool_calls": [{
                "id": "new-call", "type": "function",
                "function": {"name": "offline_search", "arguments": "{}"},
            }]}],
        ):
            item = payload()
            item["messages"].extend(following)
            variants.append(item)
        for index, data in enumerate(variants):
            with self.subTest(variant=index):
                original = provider()
                snapshot = copy.deepcopy(data)

                async def call(lane, forwarded, tools, **kwargs):
                    self.assertIs(lane, original)
                    self.assertEqual(forwarded, expected_payload(snapshot))
                    return "unchanged"

                with installed(call):
                    self.assertEqual(await original._query(data, OpaqueTools()), "unchanged")
                self.assertEqual(data, snapshot)

    async def test_all_explicit_reasoning_fields_override_policy_even_when_false_or_empty(self):
        overrides = {"reasoning_effort": "high", "reasoning": {"effort": "high"},
                     "think": False, "thinking": {}}
        for location in ("provider", "payload", "extra_body"):
            for key, value in overrides.items():
                for explicit in (value, None):
                    with self.subTest(location=location, field=key, value_type=type(explicit).__name__):
                        original, data = provider(), payload()
                        target = (original.provider_config["custom_extra_body"] if location == "provider"
                                  else data if location == "payload" else data["extra_body"])
                        target[key] = copy.deepcopy(explicit)
                        snapshot = copy.deepcopy(data)
                        config_snapshot = copy.deepcopy(original.provider_config)

                        async def call(lane, forwarded, tools, **kwargs):
                            self.assertIs(lane, original)
                            self.assertEqual(forwarded, expected_payload(snapshot))
                            return "original"

                        with installed(call):
                            self.assertEqual(await original._query(data, None), "original")
                        self.assertEqual(original.provider_config, config_snapshot)
                        self.assertEqual(data, snapshot)

    async def test_rejection_fallback_restores_payload_after_provider_mutates_it(self):
        for status in (400, 422):
            with self.subTest(status=status):
                original, data, tools = provider(), payload(), OpaqueTools()
                snapshot = copy.deepcopy(data)
                seen = []

                async def call(lane, forwarded, registered, **kwargs):
                    seen.append(lane)
                    self.assertIs(registered, tools)
                    self.assertEqual(kwargs, {"request_max_retries": 5})
                    self.assertEqual(forwarded, expected_payload(snapshot))
                    if lane is not original:
                        forwarded["messages"][1]["content"][1]["image_url"]["url"] = "mutated"
                        forwarded["extra_body"]["metadata"].clear()
                        forwarded.pop("model")
                        forwarded["tools"] = copy.deepcopy(tools.schemas)
                        forwarded["tool_choice"] = "required"
                        raise ParameterRejected(status)
                    return "original-result"

                with installed(call):
                    self.assertEqual(await original._query(data, tools, request_max_retries=5),
                                     "original-result")
                self.assertEqual(len(seen), 2)
                self.assertIs(seen[1], original)
                self.assertEqual(data, snapshot)
                self.assertNotIn("reasoning_effort", original.provider_config["custom_extra_body"])

    async def test_unsupported_cache_survives_copied_facades_and_is_scoped_to_source_and_model(self):
        for use_source_id in (True, False):
            with self.subTest(source_id=use_source_id):
                base = provider()
                if not use_source_id:
                    base.provider_config.pop("provider_source_id")
                first, second = copy.copy(base), copy.copy(base)
                other_source = provider(source="other-offline-source")
                if not use_source_id:
                    other_source.provider_config.pop("provider_source_id")
                    other_source.provider_config["api_base"] = "https://example.invalid/other"
                other_model = copy.copy(base)
                other_data = payload()
                other_data["model"] = "gpt-6"
                seen = []

                async def call(lane, forwarded, tools, **kwargs):
                    low = lane.provider_config["custom_extra_body"].get("reasoning_effort") == "low"
                    seen.append((lane, low))
                    if low:
                        raise ParameterRejected()
                    return "ok"

                with installed(call):
                    for facade, data in ((first, payload()), (second, payload()),
                                         (other_source, payload()), (other_model, other_data)):
                        self.assertEqual(await facade._query(data, None), "ok")
                self.assertEqual([low for _, low in seen], [True, False, False, True, False, True, False])
                self.assertIs(seen[2][0], second)

    async def test_original_rejection_is_not_retried_repeatedly(self):
        original = provider()
        calls = []
        failure = ParameterRejected(422)

        async def call(lane, data, tools, **kwargs):
            calls.append(lane)
            raise failure

        with installed(call):
            with self.assertRaises(ParameterRejected) as caught:
                await original._query(payload(), None)
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[-1], original)

    async def test_network_errors_and_unrelated_parameter_errors_never_trigger_fallback(self):
        failures = [TimeoutError("reasoning_effort timed out"), ConnectionError("offline"),
                    ParameterRejected(400, "invalid tool schema"),
                    ParameterRejected(422, "invalid image input")]
        failures.extend(ParameterRejected(status) for status in (401, 403, 408, 429, 500, 502, 503, 504))
        for failure in failures:
            for streaming in (False, True):
                with self.subTest(error=type(failure).__name__, status=getattr(failure, "status_code", None),
                                  streaming=streaming):
                    original = provider()
                    calls = []

                    async def call(lane, data, tools, **kwargs):
                        calls.append(lane)
                        raise failure

                    async def stream(lane, data, tools, **kwargs):
                        calls.append(lane)
                        raise failure
                        yield  # pragma: no cover

                    with installed(call, stream):
                        with self.assertRaises(type(failure)) as caught:
                            if streaming:
                                _ = [r async for r in original._query_stream(payload(), None)]
                            else:
                                await original._query(payload(), None)
                    self.assertIs(caught.exception, failure)
                    self.assertEqual(len(calls), 1)

    async def test_stream_rejection_before_output_falls_back_with_clean_payload_and_caches(self):
        original, data, tools = provider(), payload(), OpaqueTools()
        snapshot = copy.deepcopy(data)
        chunks = [object(), object()]
        seen = []

        async def stream(lane, forwarded, registered, **kwargs):
            seen.append(lane)
            self.assertEqual(forwarded, expected_payload(snapshot))
            self.assertIs(registered, tools)
            self.assertEqual(kwargs, {"conversation_id": "offline-conversation"})
            if lane is not original:
                forwarded["messages"].clear()
                forwarded["extra_body"].clear()
                raise ParameterRejected(422)
            for chunk in chunks:
                yield chunk

        async def query(lane, forwarded, registered, **kwargs):
            self.assertIs(lane, original)
            return "cached-original"

        with installed(query, stream):
            result = [r async for r in original._query_stream(
                data, tools, conversation_id="offline-conversation")]
            self.assertEqual(result, chunks)
            self.assertEqual(await original._query(data, tools), "cached-original")
        self.assertEqual(len(seen), 2)
        self.assertEqual(data, snapshot)

    async def test_stream_never_falls_back_after_any_emission_including_empty_or_reasoning(self):
        for chunk in (SimpleNamespace(completion_text="text"),
                      SimpleNamespace(completion_text="", reasoning_content="synthetic reasoning"),
                      SimpleNamespace(completion_text="", reasoning_content="")):
            with self.subTest(chunk_type=type(chunk).__name__):
                original = provider()
                failure = ParameterRejected(400)
                calls = []
                received = []
                closed = []

                async def stream(lane, data, tools, **kwargs):
                    calls.append(lane)
                    try:
                        yield chunk
                        raise failure
                    finally:
                        closed.append(True)

                with installed(stream=stream):
                    with self.assertRaises(ParameterRejected) as caught:
                        async for result in original._query_stream(payload(), OpaqueTools()):
                            received.append(result)
                self.assertIs(caught.exception, failure)
                self.assertEqual(received, [chunk])
                self.assertEqual(len(calls), 1)
                self.assertEqual(closed, [True])

    async def test_stream_current_media_and_nested_explicit_reasoning_keep_original(self):
        image_data = payload([{"type": "image_url", "image_url": {
            "url": "https://example.invalid/current.png"}}])
        explicit_data = payload()
        explicit_data["extra_body"]["reasoning"] = {"effort": "high"}
        for data in (image_data, explicit_data):
            with self.subTest(current_type=type(data["messages"][-1]["content"]).__name__):
                original = provider()
                snapshot = copy.deepcopy(data)
                tools = OpaqueTools()

                async def stream(lane, forwarded, registered, **kwargs):
                    self.assertIs(lane, original)
                    self.assertEqual(forwarded, expected_payload(snapshot))
                    self.assertIs(registered, tools)
                    yield "unchanged"

                with installed(stream=stream):
                    self.assertEqual([r async for r in original._query_stream(data, tools)], ["unchanged"])
                self.assertEqual(data, snapshot)

    async def test_cancellation_propagates_without_fallback(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                original = provider()
                calls = []

                async def call(lane, data, tools, **kwargs):
                    calls.append(lane)
                    raise asyncio.CancelledError()

                async def stream(lane, data, tools, **kwargs):
                    calls.append(lane)
                    raise asyncio.CancelledError()
                    yield  # pragma: no cover

                with installed(call, stream):
                    with self.assertRaises(asyncio.CancelledError):
                        if streaming:
                            _ = [r async for r in original._query_stream(payload(), None)]
                        else:
                            await original._query(payload(), None)
                self.assertEqual(len(calls), 1)

    async def test_disabled_unsupported_or_unmarked_requests_do_not_change_reasoning(self):
        variants = [(False, "gpt-6.1-sol", True), (True, "gpt-6.1-sol", False)]
        variants.extend((True, model, True) for model in (
            "other-model", "gpt-4.1", "gpt-5.0", "gpt-6-not/a-model", "gpt-6oops"))
        for enabled, model, marked in variants:
            with self.subTest(enabled=enabled, model=model, marked=marked):
                original, data = provider(model=model), payload()
                data["model"] = model
                if not marked:
                    data["messages"][0]["content"] = "Policy\n\nTail"
                snapshot = copy.deepcopy(data)

                async def call(lane, forwarded, tools, **kwargs):
                    self.assertIs(lane, original)
                    self.assertEqual(forwarded, expected_payload(snapshot))
                    return "original"

                with installed(call, enabled=enabled):
                    self.assertEqual(await original._query(data, None), "original")
                self.assertEqual(data, snapshot)

    async def test_request_model_takes_precedence_over_provider_default(self):
        for configured, requested, low in (("other-model", "gpt-6.1-sol", True),
                                            ("gpt-6.1-sol", "other-model", False)):
            with self.subTest(configured=configured, requested=requested):
                original, data = provider(configured), payload()
                data["model"] = requested

                async def call(lane, forwarded, tools, **kwargs):
                    self.assertEqual(lane is not original, low)
                    self.assertEqual(forwarded["model"], requested)
                    return "ok"

                with installed(call):
                    self.assertEqual(await original._query(data, None), "ok")

    async def test_user_assistant_and_tool_markers_are_untrusted_and_preserved(self):
        for role in ("user", "assistant", "tool"):
            for structured in (False, True):
                with self.subTest(role=role, structured=structured):
                    original, data = provider(), payload()
                    data["messages"][0]["content"] = "No trusted marker"
                    content = [{"type": "text", "text": MARKER}] if structured else MARKER
                    data["messages"].insert(1, {"role": role, "content": content})
                    data["messages"][-1]["content"] = "User asks: " + MARKER
                    snapshot = copy.deepcopy(data)

                    async def call(lane, forwarded, tools, **kwargs):
                        self.assertIs(lane, original)
                        self.assertEqual(forwarded, snapshot)
                        return "original"

                    with installed(call):
                        self.assertEqual(await original._query(data, None), "original")
                    self.assertEqual(data, snapshot)

    async def test_trusted_structured_markers_are_stripped_without_touching_other_content(self):
        for role in ("system", "developer"):
            with self.subTest(role=role):
                original, data = provider(), payload("User literal " + MARKER)
                data["messages"][0] = {"role": role, "content": [
                    {"type": "text", "text": "Before" + MARKER + "After"},
                    {"type": "input_text", "text": MARKER + MARKER},
                    {"type": "text", "text": "Preserved policy"},
                ]}
                snapshot = copy.deepcopy(data)
                expected = copy.deepcopy(data)
                expected["messages"][0]["content"][0]["text"] = "BeforeAfter"
                expected["messages"][0]["content"][1]["text"] = ""

                async def call(lane, forwarded, tools, **kwargs):
                    self.assertIsNot(lane, original)
                    self.assertEqual(forwarded, expected)
                    return "low"

                with installed(call):
                    self.assertEqual(await original._query(data, None), "low")
                self.assertEqual(data, snapshot)

    async def test_audit_failure_cannot_break_generation(self):
        original = provider()
        recorded = []

        def audit(kind, fields):
            recorded.append((kind, fields))
            raise RuntimeError("Synthetic unavailable audit sink")

        async def call(lane, data, tools, **kwargs):
            return "completed"

        with installed(call, audit=audit):
            self.assertEqual(await original._query(payload(), None), "completed")
        self.assertEqual(recorded, [("chat_reasoning_policy", {
            "mode": "low", "model": "gpt-6.1-sol",
        })])


if __name__ == "__main__":
    unittest.main()
