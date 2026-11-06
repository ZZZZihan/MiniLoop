"""Unit tests for OpenAICompatibleAdapter, FakeModelAdapter, and protocol validation."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from miniloop.adapter import (
    FakeModelAdapter,
    ModelAdapter,
    ModelAdapterError,
    OpenAICompatibleAdapter,
    ProtocolViolationError,
    build_adapter,
    make_text_turn,
    make_tool_call_turn,
    serialize_messages,
)
from miniloop.config import ConfigError, ModelConfig
from miniloop.secrets import SecretValue
from miniloop.types import Message, ModelTurnResponse, TokenUsage, ToolCall

SENTINEL_SECRET = "test-miniloop-fake-key-adapter-secret"


def _make_mock_adapter(
    handler: Any,
    *,
    api_key: str = SENTINEL_SECRET,
    model_name: str = "claude-sonnet-5-5",
) -> tuple[OpenAICompatibleAdapter, httpx.Client]:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    adapter = OpenAICompatibleAdapter(
        base_url="http://127.0.0.1:3425/v1/",
        model_name=model_name,
        api_key=SecretValue(api_key),
        client=client,
    )
    return adapter, client


def test_text_response_and_token_usage_parsing() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-01",
                "model": "claude-sonnet-5-5-20261001",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "The answer is 42.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 18,
                    "completion_tokens": 7,
                    "total_tokens": 25,
                },
            },
        )

    adapter, client = _make_mock_adapter(handler)
    try:
        assert isinstance(adapter, ModelAdapter)
        assert adapter.base_url == "http://127.0.0.1:3425/v1"

        messages = [
            Message(role="system", content="You are a concise assistant."),
            Message(role="user", content="What is the answer?"),
        ]
        turn = adapter.generate(messages, [], timeout_sec=5.0)

        assert turn.content == "The answer is 42."
        assert turn.tool_calls == ()
        assert turn.usage == TokenUsage(
            prompt_tokens=18,
            completion_tokens=7,
            total_tokens=25,
        )
        assert turn.model_id == "claude-sonnet-5-5-20261001"
        assert turn.attempts == 1
        assert turn.duration_ms >= 0.0

        assert len(captured_requests) == 1
        req = captured_requests[0]
        assert str(req.url) == "http://127.0.0.1:3425/v1/chat/completions"
        assert req.headers["Authorization"] == f"Bearer {SENTINEL_SECRET}"
        body = json.loads(req.content.decode("utf-8"))
        assert body == {
            "model": "claude-sonnet-5-5",
            "messages": [
                {"role": "system", "content": "You are a concise assistant."},
                {"role": "user", "content": "What is the answer?"},
            ],
            "temperature": 0.0,
        }
    finally:
        adapter.close()
        client.close()


def test_single_and_multi_tool_calls_and_tool_message_serialization() -> None:
    captured_payloads: list[dict[str, Any]] = []
    turn_idx = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal turn_idx
        turn_idx += 1
        captured_payloads.append(json.loads(request.content.decode("utf-8")))
        if turn_idx == 1:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-turn-1",
                    "model": "claude-sonnet-5-5",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_read_1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path": "README_DEMO.md"}',
                                        },
                                    },
                                    {
                                        "id": "call_read_2",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path": "config_sample.ini"}',
                                        },
                                    },
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-turn-2",
                "model": "claude-sonnet-5-5",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Both files inspected.",
                        },
                    }
                ],
            },
        )

    adapter, client = _make_mock_adapter(handler)
    try:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file from workspace",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        history: list[Message] = [Message(role="user", content="Read both demo files.")]
        turn_1 = adapter.generate(history, tools, timeout_sec=5.0)

        assert turn_1.content is None
        assert len(turn_1.tool_calls) == 2
        assert turn_1.tool_calls[0] == ToolCall(
            id="call_read_1",
            name="read_file",
            arguments={"path": "README_DEMO.md"},
            raw_arguments='{"path": "README_DEMO.md"}',
        )
        assert turn_1.tool_calls[1] == ToolCall(
            id="call_read_2",
            name="read_file",
            arguments={"path": "config_sample.ini"},
            raw_arguments='{"path": "config_sample.ini"}',
        )
        assert captured_payloads[0]["tool_choice"] == "auto"
        assert captured_payloads[0]["tools"] == tools

        history.append(
            Message(
                role="assistant",
                content=turn_1.content,
                tool_calls=turn_1.tool_calls,
            )
        )
        history.append(
            Message(
                role="tool",
                tool_call_id="call_read_1",
                name="read_file",
                content="demo readme content",
            )
        )
        history.append(
            Message(
                role="tool",
                tool_call_id="call_read_2",
                name="read_file",
                content="port=8080",
            )
        )

        turn_2 = adapter.generate(history, tools, timeout_sec=5.0)
        assert turn_2.content == "Both files inspected."
        assert turn_2.tool_calls == ()
        assert turn_2.usage == TokenUsage()

        second_messages = captured_payloads[1]["messages"]
        assert second_messages[1] == {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_read_1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "README_DEMO.md"}',
                    },
                },
                {
                    "id": "call_read_2",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "config_sample.ini"}',
                    },
                },
            ],
        }
        assert second_messages[2] == {
            "role": "tool",
            "tool_call_id": "call_read_1",
            "name": "read_file",
            "content": "demo readme content",
        }
        assert second_messages[3] == {
            "role": "tool",
            "tool_call_id": "call_read_2",
            "name": "read_file",
            "content": "port=8080",
        }
    finally:
        client.close()


def test_serialize_messages_rejects_tool_message_missing_tool_call_id() -> None:
    with pytest.raises(ProtocolViolationError, match="tool_call_id"):
        serialize_messages([Message(role="tool", content="result without id")])

    with pytest.raises(ProtocolViolationError, match="tool_call_id"):
        serialize_messages([Message(role="tool", tool_call_id="   ", content="blank id")])


def test_retries_on_http_502_and_timeout_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(502, text="Bad Gateway")
        if calls == 2:
            raise httpx.ReadTimeout("timed out waiting for gateway", request=request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-recovered",
                "model": "claude-sonnet-5-5",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Recovered"}}],
            },
        )

    adapter, client = _make_mock_adapter(handler)
    try:
        turn = adapter.generate(
            [Message(role="user", content="ping")],
            [],
            timeout_sec=2.0,
            max_retries=2,
        )
        assert turn.content == "Recovered"
        assert turn.attempts == 3
        assert calls == 3
    finally:
        client.close()


def test_retry_exhaustion_and_non_retryable_401() -> None:
    # 1. Exhausting retries on HTTP 502 raises ModelAdapterError with attempts == 1 + max_retries
    attempts_502 = 0

    def handler_502(request: httpx.Request) -> httpx.Response:
        nonlocal attempts_502
        attempts_502 += 1
        return httpx.Response(502, text="Bad Gateway")

    adapter_502, client_502 = _make_mock_adapter(handler_502)
    try:
        with pytest.raises(ModelAdapterError) as exc_info:
            adapter_502.generate(
                [Message(role="user", content="ping")],
                [],
                timeout_sec=1.0,
                max_retries=2,
            )
        assert exc_info.value.status_code == 502
        assert exc_info.value.attempts == 3
        assert attempts_502 == 3
    finally:
        client_502.close()

    # 2. Exhausting retries on TimeoutException sets attempts == 1 + max_retries
    attempts_timeout = 0

    def handler_timeout(request: httpx.Request) -> httpx.Response:
        nonlocal attempts_timeout
        attempts_timeout += 1
        raise httpx.ConnectTimeout("connection timed out", request=request)

    adapter_timeout, client_timeout = _make_mock_adapter(handler_timeout)
    try:
        with pytest.raises(ModelAdapterError) as exc_info:
            adapter_timeout.generate(
                [Message(role="user", content="ping")],
                [],
                timeout_sec=1.0,
                max_retries=1,
            )
        assert exc_info.value.status_code is None
        assert exc_info.value.attempts == 2
        assert attempts_timeout == 2
    finally:
        client_timeout.close()

    # 3. HTTP 401 never retries even when max_retries > 0
    attempts_401 = 0

    def handler_401(request: httpx.Request) -> httpx.Response:
        nonlocal attempts_401
        attempts_401 += 1
        return httpx.Response(401, json={"error": {"message": "Unauthorized"}})

    adapter_401, client_401 = _make_mock_adapter(handler_401)
    try:
        with pytest.raises(ModelAdapterError) as exc_info:
            adapter_401.generate(
                [Message(role="user", content="ping")],
                [],
                timeout_sec=1.0,
                max_retries=3,
            )
        assert exc_info.value.status_code == 401
        assert exc_info.value.attempts == 1
        assert attempts_401 == 1
    finally:
        client_401.close()


def test_sentinel_secret_never_leaks_in_exceptions_or_raw_response() -> None:
    def leaking_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-leak-check",
                "model": "claude-sonnet-5-5",
                "api_key": SENTINEL_SECRET,
                "authorization": f"Bearer {SENTINEL_SECRET}",
                "debug_echo": f"Header was Bearer {SENTINEL_SECRET} and raw {SENTINEL_SECRET}",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Safe reply",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    adapter, client = _make_mock_adapter(leaking_handler, api_key=SENTINEL_SECRET)
    try:
        turn = adapter.generate([Message(role="user", content="hi")], [], timeout_sec=2.0)
        raw_dump = json.dumps(turn.raw_response)
        assert SENTINEL_SECRET not in raw_dump
        assert turn.raw_response["api_key"] == "[REDACTED]"
        assert turn.raw_response["authorization"] == "[REDACTED]"
        assert SENTINEL_SECRET not in turn.raw_response["debug_echo"]
        # Ensure token usage fields are preserved and not falsely redacted
        assert turn.usage.total_tokens == 7
    finally:
        client.close()

    def leaking_transport_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"Failed with Authorization: Bearer {SENTINEL_SECRET} ({SENTINEL_SECRET})",
            request=request,
        )

    err_adapter, err_client = _make_mock_adapter(leaking_transport_error, api_key=SENTINEL_SECRET)
    try:
        with pytest.raises(ModelAdapterError) as exc_info:
            err_adapter.generate([Message(role="user", content="hi")], [], timeout_sec=1.0)
        assert SENTINEL_SECRET not in str(exc_info.value)
        assert SENTINEL_SECRET not in repr(exc_info.value)
        assert exc_info.value.__cause__ is None
    finally:
        err_client.close()


@pytest.mark.parametrize(
    ("bad_payload", "expected_match"),
    [
        (
            {"choices": []},
            "choices",
        ),
        (
            {"choices": [{"index": 0, "message": "not-a-dict"}]},
            "message",
        ),
        (
            {"choices": [{"index": 0, "message": {"role": "assistant", "tool_calls": "not-list"}}]},
            "tool_calls",
        ),
        (
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ]
            },
            "id",
        ),
        (
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_dup",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": "{}"},
                                },
                                {
                                    "id": "call_dup",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": "{}"},
                                },
                            ],
                        },
                    }
                ]
            },
            "Duplicate tool_call id",
        ),
        (
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ]
            },
            "function.name",
        ),
        (
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": "{not-valid-json",
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
            "not valid JSON",
        ),
        (
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": "[1, 2]",
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
            "JSON object",
        ),
    ],
)
def test_protocol_violation_errors_on_malformed_tool_calls(
    bad_payload: dict[str, Any],
    expected_match: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bad_payload)

    adapter, client = _make_mock_adapter(handler)
    try:
        with pytest.raises(ProtocolViolationError, match=expected_match):
            adapter.generate([Message(role="user", content="test")], [], timeout_sec=2.0)
    finally:
        client.close()


def test_fake_model_adapter_recording_exhaustion_and_factory() -> None:
    def dynamic_step(
        msgs: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> ModelTurnResponse:
        assert msgs[-1].role == "tool"
        assert msgs[-1].tool_call_id == "call_01"
        return make_text_turn(f"Observed: {msgs[-1].content}")

    fake = FakeModelAdapter(
        steps=[
            make_tool_call_turn([("call_01", "read_file", {"path": "README_DEMO.md"})]),
            dynamic_step,
        ],
        model_name="fake-unit-test",
    )
    assert isinstance(fake, ModelAdapter)
    assert fake.call_count == 0
    assert fake.recorded_requests == []

    tool_schema = [{"type": "function", "function": {"name": "read_file"}}]
    msg_turn_1 = [Message(role="user", content="Read demo")]
    resp_1 = fake.generate(msg_turn_1, tool_schema, timeout_sec=5.0)
    assert len(resp_1.tool_calls) == 1
    assert resp_1.tool_calls[0].id == "call_01"
    assert resp_1.tool_calls[0].arguments == {"path": "README_DEMO.md"}
    assert fake.call_count == 1
    assert len(fake.recorded_requests) == 1
    assert fake.recorded_requests[0] == (tuple(msg_turn_1), tuple(tool_schema))

    msg_turn_2 = [
        *msg_turn_1,
        Message(role="assistant", content=None, tool_calls=resp_1.tool_calls),
        Message(role="tool", tool_call_id="call_01", name="read_file", content="hello world"),
    ]
    resp_2 = fake.generate(msg_turn_2, tool_schema, timeout_sec=5.0)
    assert resp_2.content == "Observed: hello world"
    assert fake.call_count == 2
    assert len(fake.recorded_requests) == 2
    assert fake.recorded_requests[1] == (tuple(msg_turn_2), tuple(tool_schema))

    # Exhausted scripted steps raise ModelAdapterError
    with pytest.raises(ModelAdapterError, match="exhausted scripted responses"):
        fake.generate(msg_turn_2, tool_schema, timeout_sec=5.0)

    # Factory function build_adapter for fake and openai_compatible
    fake_cfg = ModelConfig(provider="fake", model_name="fake-from-cfg")
    built_fake = build_adapter(fake_cfg, fake_steps=[make_text_turn("ok")])
    assert isinstance(built_fake, FakeModelAdapter)
    assert built_fake.model_name == "fake-from-cfg"

    missing_key_cfg = ModelConfig(
        provider="openai_compatible",
        model_name="claude-sonnet-5-5",
        api_key=None,
    )
    with pytest.raises(ConfigError, match="API key"):
        build_adapter(missing_key_cfg)

    real_cfg = ModelConfig(
        provider="openai_compatible",
        base_url="http://127.0.0.1:3425/v1",
        model_name="claude-sonnet-5-5",
        api_key=SecretValue(SENTINEL_SECRET),
    )
    built_real = build_adapter(real_cfg)
    try:
        assert isinstance(built_real, OpenAICompatibleAdapter)
    finally:
        built_real.close()


def test_fake_model_adapter_timeout_and_retry_behavior() -> None:
    slow_fake = FakeModelAdapter(
        steps=[make_text_turn("too slow")],
        delay_sec=0.05,
    )
    with pytest.raises(ModelAdapterError, match="timed out") as exc_info:
        slow_fake.generate(
            [Message(role="user", content="hi")],
            [],
            timeout_sec=0.005,
            max_retries=1,
        )
    assert exc_info.value.attempts == 2
    assert slow_fake.call_count == 2
