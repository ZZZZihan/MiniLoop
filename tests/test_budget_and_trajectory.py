"""Unit tests for BudgetTracker, sanitize_payload, TrajectoryRecorder, and replay_trajectory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from miniloop.budget import BudgetTracker
from miniloop.secrets import SecretValue
from miniloop.trajectory import (
    REDACTED,
    TRAJECTORY_SCHEMA_VERSION,
    TrajectoryCorruptedError,
    TrajectoryRecorder,
    format_replay_report,
    replay_trajectory,
    sanitize_payload,
)
from miniloop.types import BudgetConfig, ExitReason


class FakeClock:
    """Deterministic monotonic clock for budget and timeout tests."""

    def __init__(self, initial: float = 100.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_budget_tracker_step_and_tool_limits() -> None:
    clock = FakeClock(initial=50.0)
    config = BudgetConfig(
        max_model_calls=3,
        max_tool_calls=2,
        max_wall_time_sec=60.0,
        per_request_timeout_sec=20.0,
        per_tool_timeout_sec=10.0,
        max_model_retries=2,
    )
    tracker = BudgetTracker(config, clock=clock)

    assert tracker.elapsed_sec == 0.0
    assert tracker.remaining_sec == 60.0
    assert tracker.check_before_model_call() is None
    assert tracker.check_before_tool_call() is None
    assert tracker.remaining_retries_allowed() == 2

    # First call uses 1 attempt; 2 calls remain -> 1 retry allowed on next request
    tracker.record_model_calls(1)
    assert tracker.model_calls == 1
    assert tracker.remaining_retries_allowed() == 1

    # Second turn takes 2 attempts (1 initial + 1 retry) -> hits max_model_calls=3
    tracker.record_model_calls(2)
    assert tracker.model_calls == 3
    assert tracker.remaining_retries_allowed() == 0
    assert tracker.check_before_model_call() == ExitReason.STEP_LIMIT

    # Tool calls are tracked independently up to max_tool_calls=2
    assert tracker.check_before_tool_call() is None
    tracker.record_tool_call()
    assert tracker.tool_calls == 1
    assert tracker.check_before_tool_call() is None
    tracker.record_tool_call()
    assert tracker.tool_calls == 2
    assert tracker.check_before_tool_call() == ExitReason.STEP_LIMIT


def test_budget_tracker_wall_time_and_timeout_clamping() -> None:
    clock = FakeClock(initial=10.0)
    config = BudgetConfig(
        max_model_calls=10,
        max_tool_calls=10,
        max_wall_time_sec=25.0,
        per_request_timeout_sec=15.0,
        per_tool_timeout_sec=12.0,
        max_model_retries=3,
    )
    tracker = BudgetTracker(config, clock=clock)

    # Full budget available: per-op timeout is smaller than remaining_sec (25.0)
    assert tracker.next_model_timeout() == 15.0
    assert tracker.next_tool_timeout() == 12.0

    # Advance 18s -> 7s remaining, both timeouts clamp to remaining_sec
    clock.advance(18.0)
    assert tracker.elapsed_sec == pytest.approx(18.0)
    assert tracker.remaining_sec == pytest.approx(7.0)
    assert tracker.next_model_timeout() == pytest.approx(7.0)
    assert tracker.next_tool_timeout() == pytest.approx(7.0)

    # Advance another 7s -> deadline reached (0s remaining)
    clock.advance(7.0)
    assert tracker.remaining_sec == pytest.approx(0.0)
    assert tracker.check_before_model_call() == ExitReason.TIME_LIMIT
    assert tracker.check_before_tool_call() == ExitReason.TIME_LIMIT

    with pytest.raises(TimeoutError, match="Wall time budget exhausted"):
        tracker.next_model_timeout()

    with pytest.raises(TimeoutError, match="Wall time budget exhausted"):
        tracker.next_tool_timeout()


def test_sanitize_payload_redacts_keys_bearer_and_secrets() -> None:
    sentinel_secret = "sk-magpie-sentinel-998877"
    short_ignored = "abc"
    raw = {
        "api_key": "plain-key",
        "nested": {
            "Authorization": "Bearer super-secret-jwt-token",
            "x_apikey_header": "12345",
            "client_secret": "top-secret",
            "user_password": "pw",
            "access_token": "acc-tok",
            "auth_token": "auth-tok",
            "wrapped_secret": SecretValue("wrapped"),
            "usage": {
                "prompt_tokens": 42,
                "completion_tokens": 18,
                "total_tokens": 60,
            },
        },
        "messages": [
            f"Header had bearer tok_xyz_123 and key {sentinel_secret} inside text.",
            f"Short {short_ignored} should not be redacted.",
        ],
    }

    cleaned = sanitize_payload(raw, secrets=[sentinel_secret, short_ignored])

    assert cleaned["api_key"] == REDACTED
    assert cleaned["nested"]["Authorization"] == REDACTED
    assert cleaned["nested"]["x_apikey_header"] == REDACTED
    assert cleaned["nested"]["client_secret"] == REDACTED
    assert cleaned["nested"]["user_password"] == REDACTED
    assert cleaned["nested"]["access_token"] == REDACTED
    assert cleaned["nested"]["auth_token"] == REDACTED
    assert cleaned["nested"]["wrapped_secret"] == REDACTED
    # Token counts must not be falsely redacted
    assert cleaned["nested"]["usage"] == {
        "prompt_tokens": 42,
        "completion_tokens": 18,
        "total_tokens": 60,
    }
    assert cleaned["messages"][0] == (
        f"Header had bearer {REDACTED} and key {REDACTED} inside text."
    )
    assert cleaned["messages"][1] == "Short abc should not be redacted."

    serialized = json.dumps(cleaned)
    assert sentinel_secret not in serialized
    assert "super-secret-jwt-token" not in serialized
    assert "tok_xyz_123" not in serialized


def test_sanitize_payload_redacts_secrets_in_mapping_keys() -> None:
    sentinel_secret = "sk-live-sentinel-123456"
    raw = {
        "tool_output": {
            sentinel_secret: "present",
            "Bearer key-in-dict-header": "ok",
        }
    }

    cleaned = sanitize_payload(raw, secrets=[sentinel_secret])

    assert cleaned == {
        "tool_output": {
            REDACTED: "present",
            f"Bearer {REDACTED}": "ok",
        }
    }
    assert sentinel_secret not in json.dumps(cleaned)


def test_sanitize_payload_redacts_token_suffix_keys_while_preserving_token_counts() -> None:
    cleaned = sanitize_payload(
        {
            "refresh_token": "rt-123",
            "id_token": "idt-456",
            "session_token": "st-789",
            "prompt_tokens": 42,
        }
    )
    assert cleaned == {
        "refresh_token": REDACTED,
        "id_token": REDACTED,
        "session_token": REDACTED,
        "prompt_tokens": 42,
    }


def test_replay_trajectory_preserves_unicode_line_separators(tmp_path: Path) -> None:
    traj_path = tmp_path / "runs" / "unicode_ls.jsonl"
    recorder = TrajectoryRecorder(traj_path, run_id="run_unicode_ls")
    unicode_task = f"line1{chr(0x2028)}line2{chr(0x2029)}line3"
    unicode_answer = f"done{chr(0x2028)}ok"

    recorder.record("run_start", {"task": unicode_task})
    recorder.record(
        "run_end",
        {
            "exit_reason": "completed",
            "final_answer": unicode_answer,
            "model_calls": 1,
            "tool_calls": 0,
        },
    )

    report = replay_trajectory(traj_path)
    assert len(report.events) == 2
    assert report.events[0].payload["task"] == unicode_task
    assert report.final_answer == unicode_answer
    assert report.exit_reason == ExitReason.COMPLETED


def test_trajectory_recorder_does_not_advance_sequence_on_failed_write(
    tmp_path: Path,
) -> None:
    traj_path = tmp_path / "runs" / "failed_event.jsonl"
    recorder = TrajectoryRecorder(traj_path, run_id="run_seq_atomic")

    recorder.record("run_start", {"task": "Check sequence atomicity"})
    assert recorder.sequence == 1

    with pytest.raises(TypeError):
        recorder.record("model_request", {"unserializable": object()})
    assert recorder.sequence == 1

    recorder.record(
        "run_end",
        {
            "exit_reason": "runtime_error",
            "final_answer": None,
            "model_calls": 0,
            "tool_calls": 0,
        },
    )
    assert recorder.sequence == 2

    report = replay_trajectory(traj_path)
    assert [event.sequence for event in report.events] == [1, 2]
    assert report.exit_reason == ExitReason.RUNTIME_ERROR


def test_trajectory_recorder_and_readonly_replay(tmp_path: Path) -> None:
    traj_path = tmp_path / "nested" / "runs" / "traj.jsonl"
    sentinel = "magpie-live-secret-token-4242"
    recorder = TrajectoryRecorder(traj_path, run_id="run_test_001", secrets=[sentinel])

    with pytest.raises(ValueError, match="Invalid event_type"):
        recorder.record("not_a_valid_event", {})

    recorder.record(
        "run_start",
        {
            "task": "Inspect config",
            "api_key": sentinel,
        },
    )
    recorder.record("model_request", {"turn": 1, "message_count": 2})
    recorder.record(
        "model_response",
        {
            "model_id": "fake-model",
            "content": None,
            "attempts": 1,
        },
    )
    recorder.record(
        "tool_call",
        {
            "call_id": "call_1",
            "tool_name": "read_file",
            "arguments": {"path": "README_DEMO.md"},
        },
    )
    recorder.record(
        "tool_call",
        {
            "call_id": "call_2",
            "tool_name": "read_file",
            "arguments": {"path": "../outside.txt"},
        },
    )
    recorder.record(
        "tool_result",
        {
            "call_id": "call_1",
            "tool_name": "read_file",
            "content": f"Service token={sentinel}",
            "is_error": False,
            "error_code": None,
        },
    )
    recorder.record(
        "tool_result",
        {
            "call_id": "call_2",
            "tool_name": "read_file",
            "content": "Access denied",
            "is_error": True,
            "error_code": "path_outside_workspace",
        },
    )
    recorder.record(
        "run_end",
        {
            "exit_reason": "completed",
            "final_answer": "Codename is AURORA-7.",
            "model_calls": 2,
            "tool_calls": 2,
        },
    )

    raw_disk_text = traj_path.read_text(encoding="utf-8")
    assert sentinel not in raw_disk_text

    report = replay_trajectory(traj_path)
    assert report.run_id == "run_test_001"
    assert report.schema_version == TRAJECTORY_SCHEMA_VERSION
    assert len(report.events) == 8
    assert [e.sequence for e in report.events] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert report.exit_reason == ExitReason.COMPLETED
    assert report.final_answer == "Codename is AURORA-7."
    assert report.model_calls == 2
    assert report.tool_calls == 2

    assert len(report.exchanges) == 2
    ex1, ex2 = report.exchanges
    assert ex1.call_id == "call_1"
    assert ex1.tool_name == "read_file"
    assert ex1.arguments == {"path": "README_DEMO.md"}
    assert ex1.result_content == f"Service token={REDACTED}"
    assert ex1.is_error is False
    assert ex1.error_code is None
    assert ex1.call_seq == 4
    assert ex1.result_seq == 6

    assert ex2.call_id == "call_2"
    assert ex2.is_error is True
    assert ex2.error_code == "path_outside_workspace"
    assert ex2.call_seq == 5
    assert ex2.result_seq == 7

    formatted = format_replay_report(report)
    assert "run_test_001" in formatted
    assert "Total events: 8" in formatted
    assert "call_1 -> read_file" in formatted
    assert "call_2 -> read_file" in formatted
    assert "path_outside_workspace" in formatted
    assert "Exit reason: completed" in formatted


def _make_event_line(
    seq: int,
    event_type: str,
    payload: dict[str, object] | None = None,
    *,
    run_id: str = "run_1",
    schema_version: str = "1.0",
) -> str:
    return json.dumps(
        {
            "schema_version": schema_version,
            "run_id": run_id,
            "sequence": seq,
            "timestamp": "2026-11-06T00:00:00+00:00",
            "event_type": event_type,
            "payload": payload or {},
        }
    )


def test_replay_detects_missing_or_empty_file(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist.jsonl"
    with pytest.raises(TrajectoryCorruptedError, match="empty_or_missing_trajectory"):
        replay_trajectory(missing)

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n   \n", encoding="utf-8")
    with pytest.raises(TrajectoryCorruptedError, match="empty_or_missing_trajectory"):
        replay_trajectory(empty)


def test_replay_detects_corrupted_json_line(tmp_path: Path) -> None:
    bad_json = tmp_path / "bad.jsonl"
    bad_json.write_text(
        "\n".join(
            [
                _make_event_line(1, "run_start"),
                "{not-valid-json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TrajectoryCorruptedError, match="invalid_json_at_line_2"):
        replay_trajectory(bad_json)

    non_object = tmp_path / "non_object.jsonl"
    non_object.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(TrajectoryCorruptedError, match="invalid_json_at_line_1"):
        replay_trajectory(non_object)


def test_replay_detects_schema_run_id_and_missing_run_start(tmp_path: Path) -> None:
    bad_schema = tmp_path / "bad_schema.jsonl"
    bad_schema.write_text(
        _make_event_line(1, "run_start", schema_version="2.0") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TrajectoryCorruptedError, match="unsupported_schema_version"):
        replay_trajectory(bad_schema)

    bad_run_id = tmp_path / "bad_run_id.jsonl"
    bad_run_id.write_text(
        "\n".join(
            [
                _make_event_line(1, "run_start", run_id="run_a"),
                _make_event_line(2, "run_end", {"exit_reason": "completed"}, run_id="run_b"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TrajectoryCorruptedError, match="inconsistent_run_id"):
        replay_trajectory(bad_run_id)

    no_start = tmp_path / "no_start.jsonl"
    no_start.write_text(
        _make_event_line(1, "model_request") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TrajectoryCorruptedError, match="missing_run_start"):
        replay_trajectory(no_start)


def test_replay_detects_sequence_gap_and_truncation(tmp_path: Path) -> None:
    seq_gap = tmp_path / "seq_gap.jsonl"
    seq_gap.write_text(
        "\n".join(
            [
                _make_event_line(1, "run_start"),
                _make_event_line(3, "run_end", {"exit_reason": "completed"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        TrajectoryCorruptedError,
        match="sequence_gap_at_line_2: expected 2, got 3",
    ):
        replay_trajectory(seq_gap)

    truncated = tmp_path / "truncated.jsonl"
    truncated.write_text(
        "\n".join(
            [
                _make_event_line(1, "run_start"),
                _make_event_line(2, "model_request"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        TrajectoryCorruptedError,
        match="truncated_trajectory_missing_run_end",
    ):
        replay_trajectory(truncated)


def test_replay_detects_dangling_duplicate_and_unknown_call_ids(tmp_path: Path) -> None:
    # 1. Dangling tool_call without matching tool_result
    dangling = tmp_path / "dangling.jsonl"
    dangling.write_text(
        "\n".join(
            [
                _make_event_line(1, "run_start"),
                _make_event_line(
                    2,
                    "tool_call",
                    {"call_id": "call_open", "tool_name": "read_file", "arguments": {}},
                ),
                _make_event_line(3, "run_end", {"exit_reason": "completed"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TrajectoryCorruptedError, match="unmatched_or_duplicate_call_id"):
        replay_trajectory(dangling)

    # 2. Unknown call_id in tool_result
    unknown_res = tmp_path / "unknown_res.jsonl"
    unknown_res.write_text(
        "\n".join(
            [
                _make_event_line(1, "run_start"),
                _make_event_line(
                    2,
                    "tool_result",
                    {"call_id": "call_ghost", "tool_name": "read_file", "content": "ok"},
                ),
                _make_event_line(3, "run_end", {"exit_reason": "completed"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TrajectoryCorruptedError, match="unmatched_or_duplicate_call_id"):
        replay_trajectory(unknown_res)

    # 3. Duplicate call_id in tool_call
    duplicate_call = tmp_path / "duplicate_call.jsonl"
    duplicate_call.write_text(
        "\n".join(
            [
                _make_event_line(1, "run_start"),
                _make_event_line(
                    2,
                    "tool_call",
                    {"call_id": "call_dup", "tool_name": "read_file", "arguments": {}},
                ),
                _make_event_line(
                    3,
                    "tool_result",
                    {"call_id": "call_dup", "tool_name": "read_file", "content": "ok"},
                ),
                _make_event_line(
                    4,
                    "tool_call",
                    {"call_id": "call_dup", "tool_name": "read_file", "arguments": {}},
                ),
                _make_event_line(5, "run_end", {"exit_reason": "completed"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TrajectoryCorruptedError, match="unmatched_or_duplicate_call_id"):
        replay_trajectory(duplicate_call)
