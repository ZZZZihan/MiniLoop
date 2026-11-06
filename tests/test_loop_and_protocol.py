"""Unit tests for run_loop, the 10-scenario protocol verification suite, and CLI commands."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from miniloop.adapter import (
    FakeModelAdapter,
    ModelAdapterError,
    make_text_turn,
    make_tool_call_turn,
)
from miniloop.cli import main, run_cli
from miniloop.loop import run_loop
from miniloop.protocol_verify import format_protocol_report, run_protocol_suite
from miniloop.secrets import SecretValue
from miniloop.tools import ToolRegistry, build_read_file_tool
from miniloop.trajectory import replay_trajectory
from miniloop.types import (
    BudgetConfig,
    ExitReason,
    Message,
    ModelTurnResponse,
    TokenUsage,
    ToolCall,
)


class _StepClock:
    """Controllable monotonic clock for testing deadline boundaries in run_loop."""

    def __init__(self, start: float = 1000.0) -> None:
        self.current = start

    def __call__(self) -> float:
        return self.current

    def advance(self, delta: float) -> None:
        self.current += delta


class _BypassingBadAdapter:
    """Custom ModelAdapter that returns malformed tool_calls without raising inside generate()."""

    def __init__(self, bad_calls: tuple[ToolCall, ...]) -> None:
        self.provider = "custom"
        self.model_name = "bypassing-adapter"
        self._bad_calls = bad_calls
        self.api_key = SecretValue("adapter-attached-secret-9999")

    def close(self) -> None:
        pass

    def generate(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
        *,
        timeout_sec: float,
        max_retries: int = 0,
    ) -> ModelTurnResponse:
        return ModelTurnResponse(
            content=" secret=adapter-attached-secret-9999 ",
            tool_calls=self._bad_calls,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model_id="bypassing-adapter",
            raw_response={},
            duration_ms=1.0,
            attempts=1,
        )


def test_protocol_suite_all_ten_scenarios_pass() -> None:
    report = run_protocol_suite()
    assert report.total_count == 10
    assert report.passed_count == 10
    assert report.all_passed is True

    expected_ids = [f"S{i:02d}" for i in range(1, 11)]
    assert [s.scenario_id for s in report.scenarios] == expected_ids
    for scenario in report.scenarios:
        assert scenario.passed is True, f"{scenario.name} failed: {scenario.detail}"

    table = format_protocol_report(report)
    assert "10/10 scenarios passed (ALL PASSED)" in table
    for sid in expected_ids:
        assert sid in table


def test_run_loop_system_prompt_and_mid_turn_tool_budget_closure(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("file-a", encoding="utf-8")
    (ws / "b.txt").write_text("file-b", encoding="utf-8")
    traj_path = tmp_path / "mid_turn_stop.jsonl"

    registry = ToolRegistry([build_read_file_tool(ws)])
    adapter = FakeModelAdapter(
        steps=[
            make_tool_call_turn(
                [
                    ("call_1", "read_file", {"path": "a.txt"}),
                    ("call_2", "read_file", {"path": "b.txt"}),
                ]
            ),
            make_text_turn("Should not reach turn 2"),
        ]
    )

    # Allow only 1 tool call -> call_1 runs and closes, call_2 is stopped BEFORE tool_call event
    result = run_loop(
        "Read both a.txt and b.txt",
        adapter=adapter,
        registry=registry,
        budget_config=BudgetConfig(max_model_calls=5, max_tool_calls=1),
        system_prompt="You are a careful read-only inspector.",
        trajectory_path=traj_path,
        run_id="run_mid_turn",
    )

    assert result.exit_reason == ExitReason.STEP_LIMIT
    assert result.model_calls == 1
    assert result.tool_calls == 1

    first_req_messages, _ = adapter.recorded_requests[0]
    assert first_req_messages[0] == Message(
        role="system",
        content="You are a careful read-only inspector.",
    )
    assert first_req_messages[1] == Message(
        role="user",
        content="Read both a.txt and b.txt",
    )

    # Trajectory must remain strictly valid with zero dangling tool_calls
    rep = replay_trajectory(traj_path)
    assert rep.exit_reason == ExitReason.STEP_LIMIT
    assert len(rep.exchanges) == 1
    assert rep.exchanges[0].call_id == "call_1"
    assert rep.exchanges[0].result_content == "file-a"


def test_run_loop_double_guard_catches_unvalidated_adapter_tool_calls(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    traj_path = tmp_path / "double_guard.jsonl"
    registry = ToolRegistry([build_read_file_tool(ws)])

    bad_adapter = _BypassingBadAdapter(
        bad_calls=(
            ToolCall(id="dup_id", name="read_file", arguments={"path": "a.txt"}),
            ToolCall(id="dup_id", name="read_file", arguments={"path": "b.txt"}),
        )
    )
    result = run_loop(
        "Test double guard",
        adapter=bad_adapter,
        registry=registry,
        trajectory_path=traj_path,
        run_id="run_double_guard",
    )
    assert result.exit_reason == ExitReason.PROTOCOL_ERROR
    assert result.model_calls == 1
    assert result.tool_calls == 0

    raw_traj = traj_path.read_text(encoding="utf-8")
    assert "adapter-attached-secret-9999" not in raw_traj
    rep = replay_trajectory(traj_path)
    assert rep.exit_reason == ExitReason.PROTOCOL_ERROR


def test_run_loop_clock_expiry_before_and_during_model_call(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    registry = ToolRegistry([build_read_file_tool(ws)])

    # 1. Clock advances past deadline during model call -> ModelAdapterError maps to TIME_LIMIT
    clock = _StepClock()
    traj_during = tmp_path / "time_limit_during.jsonl"

    def _slow_failing_step(
        _msgs: Sequence[Message],
        _tools: Sequence[dict[str, Any]],
    ) -> ModelTurnResponse:
        clock.advance(25.0)
        raise ModelAdapterError("Request timed out", status_code=None, attempts=1)

    adapter = FakeModelAdapter(steps=[_slow_failing_step])
    res = run_loop(
        "Timeout during model call",
        adapter=adapter,
        registry=registry,
        budget_config=BudgetConfig(max_wall_time_sec=10.0, max_model_retries=0),
        trajectory_path=traj_during,
        clock=clock,
    )
    assert res.exit_reason == ExitReason.TIME_LIMIT
    assert res.model_calls == 1
    rep = replay_trajectory(traj_during)
    assert rep.exit_reason == ExitReason.TIME_LIMIT


def test_cli_verify_protocol_run_and_replay(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. verify-protocol --check
    assert main(["verify-protocol", "--check"]) == 0
    out_verify = capsys.readouterr().out
    assert "10/10 scenarios passed" in out_verify

    # 2. cli run in fake mode on demo_readonly workspace
    repo_root = Path(__file__).resolve().parent.parent
    demo_ws = repo_root / "workspaces" / "demo_readonly"
    traj_out = tmp_path / "cli_fake_run.jsonl"

    assert (
        main(
            [
                "run",
                "--task",
                "Inspect demo workspace",
                "--workspace",
                str(demo_ws),
                "--provider",
                "fake",
                "--output",
                str(traj_out),
            ]
        )
        == 0
    )
    out_run = capsys.readouterr().out
    parsed_summary = json.loads(out_run)
    assert parsed_summary["exit_reason"] == "completed"
    assert parsed_summary["model_calls"] == 2
    assert parsed_summary["tool_calls"] == 2
    assert "AURORA-7" in parsed_summary["final_answer"]
    assert traj_out.is_file()

    # 3. cli replay on the generated trajectory
    assert main(["replay", str(traj_out)]) == 0
    out_replay = capsys.readouterr().out
    assert "Exit reason: completed" in out_replay
    assert "call_fake_readme" in out_replay

    # 4. cli replay on a missing/corrupted file exits with code 1
    with pytest.raises(SystemExit) as exc_info:
        main(["replay", str(tmp_path / "nonexistent.jsonl")])
    assert exc_info.value.code == 1
    err_replay = capsys.readouterr().err
    assert "Replay error:" in err_replay

    # 5. cli run with openai_compatible and missing model name fails without falling back to fake
    monkeypatch.delenv("MINILOOP_MODEL_NAME", raising=False)
    monkeypatch.delenv("MINILOOP_MODEL_API_KEY", raising=False)
    assert (
        run_cli(
            [
                "run",
                "--task",
                "Must fail without model name",
                "--workspace",
                str(demo_ws),
                "--provider",
                "openai_compatible",
            ]
        )
        == 1
    )
    err_cfg = capsys.readouterr().err
    assert "Configuration error:" in err_cfg
