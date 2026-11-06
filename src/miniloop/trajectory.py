"""Sanitized Schema v1.0 JSONL trajectory recorder and pure read-only replayer."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from miniloop.secrets import SecretValue
from miniloop.types import ExitReason, TrajectoryEvent

TRAJECTORY_SCHEMA_VERSION = "1.0"
SCHEMA_VERSION = TRAJECTORY_SCHEMA_VERSION

VALID_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_start",
        "model_request",
        "model_response",
        "tool_call",
        "tool_result",
        "budget_stop",
        "run_end",
    }
)

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "api_key",
    "api-key",
    "apikey",
    "authorization",
    "secret",
    "password",
    "access_token",
    "auth_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "private_key",
)

_BEARER_PATTERN = re.compile(r"(?i)(Bearer\s+)\S+")


class TrajectoryCorruptedError(ValueError):
    """Raised when a JSONL trajectory file is missing, malformed, truncated, or inconsistent."""


@dataclass(frozen=True)
class ReplayToolExchange:
    """Paired tool invocation and tool result reconstructed during read-only replay."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result_content: str
    is_error: bool
    error_code: str | None
    call_seq: int
    result_seq: int


@dataclass(frozen=True)
class ReplayReport:
    """Complete read-only verification report for a recorded trajectory."""

    run_id: str
    schema_version: str
    events: tuple[TrajectoryEvent, ...]
    exchanges: tuple[ReplayToolExchange, ...]
    exit_reason: ExitReason
    final_answer: str | None
    model_calls: int
    tool_calls: int


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered == "token" or lowered.endswith("_token") or lowered.endswith("-token"):
        return True
    return any(marker in lowered for marker in _SENSITIVE_KEY_SUBSTRINGS)


def _normalize_secrets(
    secrets: Sequence[str],
    extra_secrets: Sequence[str],
) -> tuple[str, ...]:
    unique = {s for s in (*secrets, *extra_secrets) if isinstance(s, str) and len(s) >= 4}
    return tuple(sorted(unique, key=len, reverse=True))


def _sanitize_string(value: str, active_secrets: tuple[str, ...]) -> str:
    redacted_str = _BEARER_PATTERN.sub(rf"\1{REDACTED}", value)
    for secret in active_secrets:
        if secret in redacted_str:
            redacted_str = redacted_str.replace(secret, REDACTED)
    return redacted_str


def _sanitize_value(value: Any, active_secrets: tuple[str, ...]) -> Any:
    if isinstance(value, SecretValue):
        return REDACTED
    if isinstance(value, Mapping):
        sanitized_dict: dict[str, Any] = {}
        for raw_key, raw_val in value.items():
            key_str = str(raw_key)
            sanitized_key = _sanitize_string(key_str, active_secrets)
            if _is_sensitive_key(key_str):
                sanitized_dict[sanitized_key] = REDACTED
            else:
                sanitized_dict[sanitized_key] = _sanitize_value(raw_val, active_secrets)
        return sanitized_dict
    if isinstance(value, str):
        return _sanitize_string(value, active_secrets)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize_value(item, active_secrets) for item in value]
    return value


def sanitize_payload(
    value: Any,
    *,
    secrets: Sequence[str] = (),
    extra_secrets: Sequence[str] = (),
) -> Any:
    """Recursively redact sensitive keys, Bearer tokens, and known plaintext secrets."""
    active_secrets = _normalize_secrets(secrets, extra_secrets)
    return _sanitize_value(value, active_secrets)


class TrajectoryRecorder:
    """Appends sanitized Schema v1.0 events to a UTF-8 JSONL file."""

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        secrets: Sequence[str] = (),
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        self.path: Path = Path(path)
        self.run_id: str = run_id
        self._secrets: tuple[str, ...] = tuple(secrets)
        self._sequence: int = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as handle:
            handle.flush()

    @property
    def sequence(self) -> int:
        """Return the last recorded sequence number."""
        return self._sequence

    def record(self, event_type: str, payload: Mapping[str, Any]) -> TrajectoryEvent:
        """Validate, sanitize, and flush a single event line to the JSONL trajectory."""
        if event_type not in VALID_EVENT_TYPES:
            raise ValueError(
                f"Invalid event_type {event_type!r}; expected one of {sorted(VALID_EVENT_TYPES)}"
            )
        next_sequence = self._sequence + 1
        sanitized_payload = sanitize_payload(dict(payload), secrets=self._secrets)
        if not isinstance(sanitized_payload, dict):
            raise TypeError("Sanitized payload must be a dict")

        event = TrajectoryEvent(
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            run_id=self.run_id,
            sequence=next_sequence,
            timestamp=datetime.now(UTC).isoformat(),
            event_type=event_type,
            payload=sanitized_payload,
        )
        serialized = json.dumps(
            {
                "schema_version": event.schema_version,
                "run_id": event.run_id,
                "sequence": event.sequence,
                "timestamp": event.timestamp,
                "event_type": event.event_type,
                "payload": event.payload,
            },
            ensure_ascii=False,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(serialized + "\n")
            handle.flush()
        self._sequence = next_sequence
        return event

    def close(self) -> None:
        """No-op close hook for callers using explicit lifecycle management."""

    def __enter__(self) -> TrajectoryRecorder:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def replay_trajectory(path: Path) -> ReplayReport:
    """Parse and validate a recorded JSONL trajectory strictly in read-only mode."""
    target = Path(path)
    if not target.exists() or not target.is_file():
        raise TrajectoryCorruptedError("empty_or_missing_trajectory: file does not exist")

    try:
        raw_text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TrajectoryCorruptedError(f"empty_or_missing_trajectory: {exc}") from exc

    raw_lines = [line.rstrip("\r") for line in raw_text.split("\n")]
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    if not any(line.strip() for line in raw_lines):
        raise TrajectoryCorruptedError("empty_or_missing_trajectory: trajectory file is empty")

    events: list[TrajectoryEvent] = []
    exchanges: list[ReplayToolExchange] = []
    pending_calls: dict[str, tuple[int, str, dict[str, Any]]] = {}
    seen_call_ids: set[str] = set()
    expected_seq = 1
    expected_run_id: str | None = None

    for lineno, raw_line in enumerate(raw_lines, start=1):
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise TrajectoryCorruptedError(
                f"invalid_json_at_line_{lineno}: {exc.msg}"
            ) from exc

        if not isinstance(parsed, dict):
            raise TrajectoryCorruptedError(
                f"invalid_json_at_line_{lineno}: expected JSON object"
            )

        schema_version = parsed.get("schema_version")
        if schema_version != TRAJECTORY_SCHEMA_VERSION:
            raise TrajectoryCorruptedError(
                f"unsupported_schema_version: expected {TRAJECTORY_SCHEMA_VERSION!r}, "
                f"got {schema_version!r}"
            )

        seq = parsed.get("sequence")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq != expected_seq:
            raise TrajectoryCorruptedError(
                f"sequence_gap_at_line_{lineno}: expected {expected_seq}, got {seq}"
            )

        row_run_id = parsed.get("run_id")
        if not isinstance(row_run_id, str) or not row_run_id.strip():
            raise TrajectoryCorruptedError(
                f"inconsistent_run_id: missing or empty run_id at line {lineno}"
            )
        if expected_run_id is None:
            expected_run_id = row_run_id
        elif row_run_id != expected_run_id:
            raise TrajectoryCorruptedError(
                f"inconsistent_run_id: expected {expected_run_id!r}, got {row_run_id!r}"
            )

        event_type = parsed.get("event_type")
        if expected_seq == 1 and event_type != "run_start":
            raise TrajectoryCorruptedError(
                f"missing_run_start: first event was {event_type!r}"
            )
        if not isinstance(event_type, str) or event_type not in VALID_EVENT_TYPES:
            raise TrajectoryCorruptedError(
                f"invalid_json_at_line_{lineno}: unknown event_type {event_type!r}"
            )

        timestamp = parsed.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise TrajectoryCorruptedError(
                f"invalid_json_at_line_{lineno}: missing or invalid timestamp"
            )

        payload = parsed.get("payload")
        if not isinstance(payload, dict):
            raise TrajectoryCorruptedError(
                f"invalid_json_at_line_{lineno}: payload must be a JSON object"
            )

        event = TrajectoryEvent(
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            run_id=row_run_id,
            sequence=seq,
            timestamp=timestamp,
            event_type=event_type,
            payload=payload,
        )
        events.append(event)
        expected_seq += 1

        if event_type == "tool_call":
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                raise TrajectoryCorruptedError(
                    f"unmatched_or_duplicate_call_id: missing call_id at sequence {seq}"
                )
            if call_id in seen_call_ids:
                raise TrajectoryCorruptedError(
                    f"unmatched_or_duplicate_call_id: duplicate call_id {call_id!r}"
                )
            raw_tool_name = payload.get("tool_name") or payload.get("name")
            if not isinstance(raw_tool_name, str) or not raw_tool_name.strip():
                raise TrajectoryCorruptedError(
                    f"invalid_json_at_line_{lineno}: missing tool_name in tool_call"
                )
            raw_args = payload.get("arguments", {})
            if not isinstance(raw_args, dict):
                raise TrajectoryCorruptedError(
                    f"invalid_json_at_line_{lineno}: tool_call arguments must be a dict"
                )
            seen_call_ids.add(call_id)
            pending_calls[call_id] = (seq, raw_tool_name, dict(raw_args))

        elif event_type == "tool_result":
            call_id = payload.get("call_id")
            if (
                not isinstance(call_id, str)
                or not call_id.strip()
                or call_id not in pending_calls
            ):
                raise TrajectoryCorruptedError(
                    f"unmatched_or_duplicate_call_id: tool_result references unknown "
                    f"or already-resolved call_id {call_id!r}"
                )
            call_seq, call_tool_name, call_args = pending_calls.pop(call_id)
            res_tool_name = payload.get("tool_name")
            tool_name = (
                res_tool_name
                if isinstance(res_tool_name, str) and res_tool_name.strip()
                else call_tool_name
            )
            raw_content = (
                payload.get("content")
                if "content" in payload
                else payload.get("result_content", "")
            )
            result_content = str(raw_content) if raw_content is not None else ""
            is_error = bool(payload.get("is_error", False))
            raw_error_code = payload.get("error_code")
            error_code = str(raw_error_code) if raw_error_code is not None else None
            exchanges.append(
                ReplayToolExchange(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=call_args,
                    result_content=result_content,
                    is_error=is_error,
                    error_code=error_code,
                    call_seq=call_seq,
                    result_seq=seq,
                )
            )

    if events[0].event_type != "run_start":
        raise TrajectoryCorruptedError("missing_run_start")

    if events[-1].event_type != "run_end":
        if pending_calls:
            dangling = ", ".join(sorted(pending_calls))
            raise TrajectoryCorruptedError(
                f"truncated_trajectory_missing_run_end: truncated_trajectory: missing run_end "
                f"(unmatched_or_duplicate_call_id: dangling call_ids {dangling})"
            )
        raise TrajectoryCorruptedError(
            "truncated_trajectory_missing_run_end: truncated_trajectory: missing run_end"
        )

    if pending_calls:
        dangling = ", ".join(sorted(pending_calls))
        raise TrajectoryCorruptedError(
            f"unmatched_or_duplicate_call_id: dangling tool_call without tool_result ({dangling})"
        )

    end_payload = events[-1].payload
    raw_exit_reason = end_payload.get("exit_reason", ExitReason.COMPLETED.value)
    if not isinstance(raw_exit_reason, str):
        raise TrajectoryCorruptedError(
            f"invalid_exit_reason: expected string, got {raw_exit_reason!r}"
        )
    try:
        exit_reason = ExitReason(raw_exit_reason)
    except ValueError as exc:
        raise TrajectoryCorruptedError(
            f"invalid_exit_reason: unknown exit_reason {raw_exit_reason!r}"
        ) from exc

    raw_final_answer = end_payload.get("final_answer")
    final_answer = str(raw_final_answer) if isinstance(raw_final_answer, str) else None

    raw_model_calls = end_payload.get("model_calls")
    if isinstance(raw_model_calls, int) and not isinstance(raw_model_calls, bool):
        model_calls = raw_model_calls
    else:
        response_events = [e for e in events if e.event_type == "model_response"]
        if response_events:
            model_calls = sum(
                int(e.payload.get("attempts", 1))
                if isinstance(e.payload.get("attempts"), int)
                and not isinstance(e.payload.get("attempts"), bool)
                else 1
                for e in response_events
            )
        else:
            model_calls = sum(1 for e in events if e.event_type == "model_request")

    raw_tool_calls = end_payload.get("tool_calls")
    if isinstance(raw_tool_calls, int) and not isinstance(raw_tool_calls, bool):
        tool_calls = raw_tool_calls
    else:
        tool_calls = len(exchanges)

    assert expected_run_id is not None
    return ReplayReport(
        run_id=expected_run_id,
        schema_version=TRAJECTORY_SCHEMA_VERSION,
        events=tuple(events),
        exchanges=tuple(exchanges),
        exit_reason=exit_reason,
        final_answer=final_answer,
        model_calls=model_calls,
        tool_calls=tool_calls,
    )


def format_replay_report(report: ReplayReport) -> str:
    """Format a human-readable summary of a ``ReplayReport``."""
    lines: list[str] = [
        f"Run ID: {report.run_id} (schema v{report.schema_version})",
        f"Total events: {len(report.events)} | model_calls: {report.model_calls} | "
        f"tool_calls: {report.tool_calls}",
    ]
    if report.exchanges:
        lines.append("Tool exchanges:")
        for ex in report.exchanges:
            args_str = json.dumps(ex.arguments, ensure_ascii=False, sort_keys=True)
            if ex.is_error:
                status = f"error({ex.error_code or 'unknown'}): {ex.result_content}"
            else:
                status = f"result: {ex.result_content}"
            lines.append(
                f"  [{ex.call_seq}->{ex.result_seq}] "
                f"{ex.call_id} -> {ex.tool_name}({args_str}) -> {status}"
            )
    else:
        lines.append("Tool exchanges: (none)")

    lines.append(f"Exit reason: {report.exit_reason.value}")
    if report.final_answer is not None:
        lines.append(f"Final answer: {report.final_answer}")
    return "\n".join(lines)


format_replay_summary = format_replay_report
