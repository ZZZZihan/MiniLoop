"""Model adapter protocol, OpenAI-compatible HTTP client, and deterministic fake adapter."""

from __future__ import annotations

import dataclasses
import json
import re
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

import httpx

from miniloop.config import ConfigError, ModelConfig
from miniloop.secrets import SecretValue
from miniloop.types import Message, ModelTurnResponse, TokenUsage, ToolCall

_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "auth_token",
        "access_token",
        "refresh_token",
        "secret",
        "password",
        "bearer",
    }
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s'\"]+")


class ModelAdapterError(RuntimeError):
    """Raised when a model call fails after exhausting allowed retries."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.status_code: int | None = status_code
        self.attempts: int = attempts


class ProtocolViolationError(RuntimeError):
    """Raised when the model response or tool message violates the tool-calling protocol."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 1,
        secret: str = "",
    ) -> None:
        super().__init__(_scrub_text(message, secret))
        self.attempts: int = attempts


@runtime_checkable
class ModelAdapter(Protocol):
    """Boundary interface for generating model turns from conversation history and tool schemas."""

    provider: str
    model_name: str

    def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
        *,
        timeout_sec: float,
        max_retries: int = 0,
    ) -> ModelTurnResponse:
        """Send messages and tool definitions to the model and parse the response turn."""
        ...

    def close(self) -> None:
        """Release any underlying transport resources held by the adapter."""
        ...


def _scrub_text(text: str, secret: str = "") -> str:
    """Redact Bearer tokens and explicit secret plaintext from a string."""
    scrubbed = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    if secret:
        scrubbed = scrubbed.replace(secret, "[REDACTED]")
    return scrubbed


def _sanitize_raw_response(data: Any, secret: str = "") -> Any:
    """Recursively redact sensitive keys and secret plaintext from raw response payloads."""
    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for key, value in data.items():
            key_str = str(key)
            if key_str.lower() in _SENSITIVE_KEYS:
                result[key_str] = "[REDACTED]"
            else:
                result[key_str] = _sanitize_raw_response(value, secret)
        return result
    if isinstance(data, list):
        return [_sanitize_raw_response(item, secret) for item in data]
    if isinstance(data, str):
        return _scrub_text(data, secret)
    return data


def serialize_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Serialize internal ``Message`` sequence into OpenAI chat completion message dicts."""
    serialized: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role in ("system", "user"):
            serialized.append({"role": msg.role, "content": msg.content or ""})
        elif msg.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": (
                                tc.raw_arguments
                                if tc.raw_arguments
                                and (tc.raw_arguments != "{}" or not tc.arguments)
                                else json.dumps(tc.arguments, ensure_ascii=False)
                            ),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            serialized.append(entry)
        elif msg.role == "tool":
            if msg.tool_call_id is None or not msg.tool_call_id.strip():
                raise ProtocolViolationError("tool message missing tool_call_id")
            serialized.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "name": msg.name or "",
                    "content": msg.content or "",
                }
            )
        else:
            raise ProtocolViolationError(f"Unsupported message role: {msg.role!r}")
    return serialized


def _validate_tool_calls_list(tool_calls: Sequence[ToolCall], *, attempts: int = 1) -> None:
    """Validate that tool calls have non-empty IDs/names and unique IDs within a single turn."""
    seen_ids: set[str] = set()
    for tc in tool_calls:
        if not isinstance(tc.id, str) or not tc.id.strip():
            raise ProtocolViolationError(
                "tool_call missing non-empty string 'id'", attempts=attempts
            )
        if tc.id in seen_ids:
            safe_id = _scrub_text(tc.id)
            raise ProtocolViolationError(
                f"Duplicate tool_call id in single turn: {safe_id!r}", attempts=attempts
            )
        seen_ids.add(tc.id)
        if not isinstance(tc.name, str) or not tc.name.strip():
            raise ProtocolViolationError(
                "tool_call missing non-empty string 'function.name'", attempts=attempts
            )
        if not isinstance(tc.arguments, dict):
            raise ProtocolViolationError(
                "tool_call 'function.arguments' must decode to a dict", attempts=attempts
            )


def _extract_optional_int(mapping: dict[str, Any], key: str) -> int | None:
    value = mapping.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


class OpenAICompatibleAdapter:
    """HTTP model adapter targeting an OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        api_key: SecretValue,
        client: httpx.Client | None = None,
        temperature: float = 0.0,
    ) -> None:
        self.provider: str = "openai_compatible"
        self.model_name: str = model_name
        self.base_url: str = base_url.rstrip("/")
        self.api_key: SecretValue = api_key
        self.temperature: float = temperature
        self._owns_client: bool = client is None
        self._client: httpx.Client = client if client is not None else httpx.Client()

    def close(self) -> None:
        """Close the underlying ``httpx.Client`` if it was created by this adapter."""
        if self._owns_client:
            self._client.close()

    def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
        *,
        timeout_sec: float,
        max_retries: int = 0,
    ) -> ModelTurnResponse:
        """Execute a chat completion request with bounded retry and strict protocol parsing."""
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": serialize_messages(messages),
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"

        secret_plain = self.api_key.get_secret_value()
        headers = {"Authorization": f"Bearer {secret_plain}"}
        url = f"{self.base_url}/chat/completions"
        max_attempts = 1 + max(0, max_retries)
        start_monotonic = time.monotonic()

        resp: httpx.Response | None = None
        attempt = 0
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self._client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=timeout_sec,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                safe_msg = _scrub_text(str(exc), secret_plain)
                if attempt < max_attempts:
                    continue
                raise ModelAdapterError(
                    f"Transport error after {attempt} attempt(s): {safe_msg}",
                    status_code=None,
                    attempts=attempt,
                ) from None

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < max_attempts:
                    continue
                raise ModelAdapterError(
                    f"HTTP {resp.status_code} after {attempt} attempt(s)",
                    status_code=resp.status_code,
                    attempts=attempt,
                )

            if 400 <= resp.status_code < 500:
                raise ModelAdapterError(
                    f"HTTP {resp.status_code}",
                    status_code=resp.status_code,
                    attempts=attempt,
                )

            if resp.status_code < 200 or resp.status_code >= 300:
                raise ModelAdapterError(
                    f"Unexpected HTTP {resp.status_code}",
                    status_code=resp.status_code,
                    attempts=attempt,
                )
            break

        assert resp is not None
        duration_ms = (time.monotonic() - start_monotonic) * 1000.0

        raw_text = resp.text
        if not raw_text or not raw_text.strip():
            raise ModelAdapterError(
                "Empty response body from model endpoint",
                status_code=resp.status_code,
                attempts=attempt,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            safe_err = _scrub_text(str(exc), secret_plain)
            raise ProtocolViolationError(
                f"Response body is not valid JSON: {safe_err}",
                attempts=attempt,
            ) from None

        if not isinstance(data, dict):
            raise ProtocolViolationError(
                "Response JSON root must be a dict",
                attempts=attempt,
            )

        choices = data.get("choices")
        if not isinstance(choices, list) or len(choices) == 0:
            raise ProtocolViolationError(
                "Response JSON missing non-empty 'choices' list",
                attempts=attempt,
            )

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise ProtocolViolationError(
                "Response 'choices[0]' must be a dict",
                attempts=attempt,
            )

        message_obj = first_choice.get("message")
        if not isinstance(message_obj, dict):
            raise ProtocolViolationError(
                "Response 'choices[0].message' must be a dict",
                attempts=attempt,
            )

        raw_content = message_obj.get("content")
        if raw_content is not None and not isinstance(raw_content, str):
            raise ProtocolViolationError(
                "Response 'choices[0].message.content' must be a string or null",
                attempts=attempt,
            )

        raw_tool_calls = message_obj.get("tool_calls")
        parsed_calls: list[ToolCall] = []
        if raw_tool_calls is not None:
            if not isinstance(raw_tool_calls, list):
                raise ProtocolViolationError(
                    "Response 'choices[0].message.tool_calls' must be a list",
                    attempts=attempt,
                )
            seen_ids: set[str] = set()
            for raw_tc in raw_tool_calls:
                if not isinstance(raw_tc, dict):
                    raise ProtocolViolationError(
                        "Each tool_call entry must be a dict",
                        attempts=attempt,
                    )
                call_id = raw_tc.get("id")
                if not isinstance(call_id, str) or not call_id.strip():
                    raise ProtocolViolationError(
                        "tool_call missing non-empty string 'id'",
                        attempts=attempt,
                    )
                if call_id in seen_ids:
                    safe_call_id = _scrub_text(call_id, secret_plain)
                    raise ProtocolViolationError(
                        f"Duplicate tool_call id in single turn: {safe_call_id!r}",
                        attempts=attempt,
                        secret=secret_plain,
                    )
                seen_ids.add(call_id)

                func_obj = raw_tc.get("function")
                if not isinstance(func_obj, dict):
                    raise ProtocolViolationError(
                        "tool_call missing 'function' dict",
                        attempts=attempt,
                    )
                func_name = func_obj.get("name")
                if not isinstance(func_name, str) or not func_name.strip():
                    raise ProtocolViolationError(
                        "tool_call missing non-empty string 'function.name'",
                        attempts=attempt,
                    )
                raw_args = func_obj.get("arguments")
                if not isinstance(raw_args, str):
                    raise ProtocolViolationError(
                        "tool_call 'function.arguments' must be a JSON string",
                        attempts=attempt,
                    )
                try:
                    parsed_args = json.loads(raw_args)
                except ValueError as exc:
                    safe_err = _scrub_text(str(exc), secret_plain)
                    raise ProtocolViolationError(
                        f"tool_call 'function.arguments' is not valid JSON: {safe_err}",
                        attempts=attempt,
                    ) from None

                if not isinstance(parsed_args, dict):
                    raise ProtocolViolationError(
                        "tool_call 'function.arguments' must decode to a JSON object (dict)",
                        attempts=attempt,
                    )

                parsed_calls.append(
                    ToolCall(
                        id=call_id,
                        name=func_name,
                        arguments=parsed_args,
                        raw_arguments=raw_args,
                    )
                )

        raw_usage = data.get("usage")
        if isinstance(raw_usage, dict):
            usage = TokenUsage(
                prompt_tokens=_extract_optional_int(raw_usage, "prompt_tokens"),
                completion_tokens=_extract_optional_int(raw_usage, "completion_tokens"),
                total_tokens=_extract_optional_int(raw_usage, "total_tokens"),
            )
        else:
            usage = TokenUsage()

        raw_model_id = data.get("model")
        model_id = (
            raw_model_id
            if isinstance(raw_model_id, str) and raw_model_id.strip()
            else self.model_name
        )
        sanitized_data: dict[str, Any] = _sanitize_raw_response(data, secret_plain)

        return ModelTurnResponse(
            content=raw_content,
            tool_calls=tuple(parsed_calls),
            usage=usage,
            model_id=model_id,
            raw_response=sanitized_data,
            duration_ms=duration_ms,
            attempts=attempt,
        )


FakeStep = (
    ModelTurnResponse
    | Exception
    | Callable[[Sequence[Message], Sequence[dict[str, Any]]], ModelTurnResponse]
)


def make_tool_call_turn(
    calls: Sequence[tuple[str, str, dict[str, Any]]],
    *,
    content: str | None = None,
    model_id: str = "fake-deterministic",
) -> ModelTurnResponse:
    """Build a deterministic ``ModelTurnResponse`` containing one or more tool calls."""
    tool_calls = tuple(
        ToolCall(
            id=call_id,
            name=tool_name,
            arguments=dict(arguments),
            raw_arguments=json.dumps(arguments, ensure_ascii=False),
        )
        for call_id, tool_name, arguments in calls
    )
    return ModelTurnResponse(
        content=content,
        tool_calls=tool_calls,
        usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        model_id=model_id,
        raw_response={
            "id": "fake-tool-call-turn",
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": tc.raw_arguments,
                                },
                            }
                            for tc in tool_calls
                        ],
                    },
                }
            ],
        },
        duration_ms=0.0,
        attempts=1,
    )


def make_text_turn(
    content: str,
    *,
    model_id: str = "fake-deterministic",
) -> ModelTurnResponse:
    """Build a deterministic ``ModelTurnResponse`` containing a plain text assistant reply."""
    return ModelTurnResponse(
        content=content,
        tool_calls=(),
        usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        model_id=model_id,
        raw_response={
            "id": "fake-text-turn",
            "model": model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                }
            ],
        },
        duration_ms=0.0,
        attempts=1,
    )


class FakeModelAdapter:
    """Deterministic scripted model double with zero network dependencies."""

    def __init__(
        self,
        steps: Sequence[FakeStep] = (),
        model_name: str = "fake-deterministic",
        *,
        delay_sec: float = 0.0,
    ) -> None:
        self.provider: str = "fake"
        self.model_name: str = model_name
        self.delay_sec: float = delay_sec
        self._steps: list[FakeStep] = list(steps)
        self._call_count: int = 0
        self._recorded_requests: list[tuple[tuple[Message, ...], tuple[dict[str, Any], ...]]] = []

    @property
    def call_count(self) -> int:
        """Return the number of times ``generate()`` has been invoked."""
        return self._call_count

    @property
    def recorded_requests(
        self,
    ) -> list[tuple[tuple[Message, ...], tuple[dict[str, Any], ...]]]:
        """Return recorded ``(messages, tools)`` snapshots for each ``generate()`` call."""
        return self._recorded_requests

    def close(self) -> None:
        """No-op resource cleanup for protocol compatibility."""

    def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
        *,
        timeout_sec: float,
        max_retries: int = 0,
    ) -> ModelTurnResponse:
        """Return the next scripted response or raise the next scripted exception."""
        serialize_messages(messages)

        max_attempts = 1 + max(0, max_retries)
        last_retryable_error: ModelAdapterError | None = None

        for attempt in range(1, max_attempts + 1):
            if not self._steps:
                if last_retryable_error is not None:
                    self._call_count += 1
                    self._recorded_requests.append((tuple(messages), tuple(tools)))
                    if attempt < max_attempts:
                        continue
                    raise ModelAdapterError(
                        str(last_retryable_error),
                        status_code=last_retryable_error.status_code,
                        attempts=attempt,
                    )
                raise ModelAdapterError(
                    "FakeModelAdapter exhausted scripted responses",
                    attempts=attempt,
                )

            self._call_count += 1
            self._recorded_requests.append((tuple(messages), tuple(tools)))
            step = self._steps.pop(0)

            start_monotonic = time.monotonic()
            if self.delay_sec > 0.0:
                if self.delay_sec > timeout_sec:
                    time.sleep(max(0.0, timeout_sec))
                    timeout_err = ModelAdapterError(
                        f"FakeModelAdapter timed out after {timeout_sec:.3f}s",
                        status_code=None,
                        attempts=attempt,
                    )
                    last_retryable_error = timeout_err
                    if attempt < max_attempts:
                        continue
                    raise timeout_err
                time.sleep(self.delay_sec)

            if isinstance(step, ProtocolViolationError):
                raise ProtocolViolationError(str(step), attempts=attempt)

            if isinstance(step, ModelAdapterError):
                if (
                    step.status_code is not None
                    and 400 <= step.status_code < 500
                    and step.status_code != 429
                ):
                    raise ModelAdapterError(
                        str(step),
                        status_code=step.status_code,
                        attempts=attempt,
                    )
                last_retryable_error = step
                if attempt < max_attempts:
                    continue
                raise ModelAdapterError(
                    str(step),
                    status_code=step.status_code,
                    attempts=attempt,
                )

            if isinstance(step, Exception):
                raise step

            if callable(step):
                try:
                    response = step(messages, tools)
                except ProtocolViolationError as exc:
                    raise ProtocolViolationError(str(exc), attempts=attempt) from exc
                except ModelAdapterError as exc:
                    if (
                        exc.status_code is not None
                        and 400 <= exc.status_code < 500
                        and exc.status_code != 429
                    ):
                        raise ModelAdapterError(
                            str(exc),
                            status_code=exc.status_code,
                            attempts=attempt,
                        ) from exc
                    last_retryable_error = exc
                    if attempt < max_attempts:
                        continue
                    raise ModelAdapterError(
                        str(exc),
                        status_code=exc.status_code,
                        attempts=attempt,
                    ) from exc
            else:
                response = step

            _validate_tool_calls_list(response.tool_calls, attempts=attempt)
            elapsed_ms = (time.monotonic() - start_monotonic) * 1000.0
            duration_ms = response.duration_ms if response.duration_ms > 0.0 else elapsed_ms
            return dataclasses.replace(
                response,
                duration_ms=duration_ms,
                attempts=attempt,
            )

        raise ModelAdapterError("FakeModelAdapter exhausted retry loop", attempts=max_attempts)


def build_adapter(
    config: ModelConfig,
    *,
    fake_steps: Sequence[FakeStep] = (),
    client: httpx.Client | None = None,
) -> ModelAdapter:
    """Construct a ``ModelAdapter`` from resolved ``ModelConfig``."""
    if config.provider == "fake":
        return FakeModelAdapter(fake_steps, model_name=config.model_name)
    if config.provider == "openai_compatible":
        if config.api_key is None:
            raise ConfigError(
                "API key is required when building OpenAICompatibleAdapter."
            )
        if not config.model_name.strip():
            raise ConfigError(
                "model_name is required when building OpenAICompatibleAdapter."
            )
        return OpenAICompatibleAdapter(
            base_url=config.base_url,
            model_name=config.model_name,
            api_key=config.api_key,
            client=client,
        )
    raise ConfigError(f"Unsupported model provider: {config.provider!r}")
