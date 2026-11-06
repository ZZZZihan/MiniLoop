"""Deterministic 10-scenario protocol verification suite (zero network, zero real keys)."""

from __future__ import annotations

import hashlib
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from miniloop.adapter import (
    FakeModelAdapter,
    ModelAdapterError,
    make_text_turn,
    make_tool_call_turn,
)
from miniloop.loop import run_loop
from miniloop.tools.read_file import READ_FILE_PARAMETERS_SCHEMA, build_read_file_tool
from miniloop.tools.registry import ToolDefinition, ToolExecutionError, ToolRegistry
from miniloop.trajectory import TrajectoryCorruptedError, replay_trajectory
from miniloop.types import (
    BudgetConfig,
    ExitReason,
    Message,
    ModelTurnResponse,
    TokenUsage,
    ToolCall,
)

_DEMO_README_CONTENT = (
    "# MiniLoop Read-Only Demo Workspace\n\n"
    "This directory is the read-only verification workspace for MiniLoop **P0-01** "
    "(single read-only tool loop).\n\n"
    "## System Facts\n\n"
    "- **Service Codename**: `AURORA-7`\n"
    "- **Primary Role**: Deterministic telemetry ingestion and batch verification node\n"
    "- **Scheduled Maintenance Window**: `UTC 03:30` (daily 15-minute drain window)\n"
    "- **Reference Configuration File**: `config_sample.ini`\n\n"
    "For runtime listener port, batch size limit, and checksum verification mode, "
    "inspect `config_sample.ini` in this same workspace.\n"
)

_DEMO_CONFIG_CONTENT = """\
[service]
codename = AURORA-7
environment = demo-readonly
maintenance_window = UTC 03:30

[network]
listen_host = 127.0.0.1
listen_port = 18420
request_timeout_sec = 12

[ingestion]
max_batch_items = 64
checksum_mode = sha256-strict
retry_limit = 3
"""


@dataclass(frozen=True)
class ProtocolScenarioResult:
    """Verification outcome for a single deterministic protocol scenario."""

    scenario_id: str
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ProtocolSuiteReport:
    """Aggregate report for all 10 deterministic protocol scenarios."""

    scenarios: tuple[ProtocolScenarioResult, ...]

    @property
    def passed_count(self) -> int:
        """Return number of scenarios that passed."""
        return sum(1 for s in self.scenarios if s.passed)

    @property
    def total_count(self) -> int:
        """Return total number of scenarios executed."""
        return len(self.scenarios)

    @property
    def all_passed(self) -> bool:
        """Return True iff the suite is non-empty and every scenario passed."""
        return len(self.scenarios) > 0 and all(s.passed for s in self.scenarios)


def _populate_demo_workspace(workspace_dir: Path) -> Path:
    """Create a clean isolated copy of the demo_readonly workspace files."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "README_DEMO.md").write_text(_DEMO_README_CONTENT, encoding="utf-8")
    (workspace_dir / "config_sample.ini").write_text(_DEMO_CONFIG_CONTENT, encoding="utf-8")
    return workspace_dir


def _snapshot_workspace_hashes(workspace_dir: Path) -> dict[str, str]:
    """Compute SHA-256 digest for every file inside workspace_dir."""
    hashes: dict[str, str] = {}
    for file_path in sorted(workspace_dir.rglob("*")):
        if file_path.is_file():
            rel = file_path.relative_to(workspace_dir).as_posix()
            hashes[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return hashes


def _build_counted_read_file_tool(
    workspace_dir: Path,
) -> tuple[ToolDefinition, Callable[[], int]]:
    """Wrap build_read_file_tool with an execution counter for side-effect verification."""
    base_tool = build_read_file_tool(workspace_dir)
    execution_count = 0

    def _counted_handler(arguments: dict[str, Any], timeout_sec: float) -> str:
        nonlocal execution_count
        execution_count += 1
        return base_tool.handler(arguments, timeout_sec)

    wrapped = ToolDefinition(
        name=base_tool.name,
        description=base_tool.description,
        parameters_schema=dict(READ_FILE_PARAMETERS_SCHEMA),
        handler=_counted_handler,
    )
    return wrapped, lambda: execution_count


def _scenario_s01_normal_completion(tmp_root: Path) -> str:
    ws = _populate_demo_workspace(tmp_root / "s01_ws")
    traj_path = tmp_root / "s01.jsonl"
    tool, get_exec_count = _build_counted_read_file_tool(ws)
    registry = ToolRegistry([tool])

    def _turn_2(
        messages: Sequence[Message],
        _tools: Sequence[dict[str, Any]],
    ) -> ModelTurnResponse:
        assert messages[-1].role == "tool"
        assert messages[-1].tool_call_id == "call_s01_1"
        assert messages[-1].content is not None and "AURORA-7" in messages[-1].content
        return make_text_turn("Service codename is AURORA-7 (maintenance UTC 03:30).")

    adapter = FakeModelAdapter(
        steps=[
            make_tool_call_turn([("call_s01_1", "read_file", {"path": "README_DEMO.md"})]),
            _turn_2,
        ]
    )
    res = run_loop(
        "Read README_DEMO.md and report codename.",
        adapter=adapter,
        registry=registry,
        trajectory_path=traj_path,
        run_id="run_s01",
    )
    assert res.exit_reason == ExitReason.COMPLETED
    assert res.model_calls == 2
    assert res.tool_calls == 1
    assert get_exec_count() == 1
    assert res.final_answer is not None and "AURORA-7" in res.final_answer

    report = replay_trajectory(traj_path)
    assert report.exit_reason == ExitReason.COMPLETED
    assert len(report.exchanges) == 1
    assert report.exchanges[0].call_id == "call_s01_1"
    return "exit_reason=completed, model_calls=2, tool_calls=1, call_id paired"


def _scenario_s02_invalid_arguments_no_side_effect(tmp_root: Path) -> str:
    ws = _populate_demo_workspace(tmp_root / "s02_ws")
    traj_path = tmp_root / "s02.jsonl"
    tool, get_exec_count = _build_counted_read_file_tool(ws)
    registry = ToolRegistry([tool])

    def _turn_2_after_bad_args(
        messages: Sequence[Message],
        _tools: Sequence[dict[str, Any]],
    ) -> ModelTurnResponse:
        assert get_exec_count() == 0, "Handler must not run when arguments fail schema check"
        last_msg = messages[-1]
        assert last_msg.role == "tool"
        assert last_msg.tool_call_id == "call_s02_bad"
        assert last_msg.content is not None and "invalid_arguments" in last_msg.content
        return make_tool_call_turn(
            [("call_s02_good", "read_file", {"path": "README_DEMO.md"})]
        )

    def _turn_3_finish(
        messages: Sequence[Message],
        _tools: Sequence[dict[str, Any]],
    ) -> ModelTurnResponse:
        assert get_exec_count() == 1
        last_msg = messages[-1]
        assert last_msg.role == "tool"
        assert last_msg.tool_call_id == "call_s02_good"
        assert last_msg.content is not None and "AURORA-7" in last_msg.content
        return make_text_turn("Recovered after fixing path argument: AURORA-7.")

    adapter = FakeModelAdapter(
        steps=[
            make_tool_call_turn([("call_s02_bad", "read_file", {"path": 12345})]),
            _turn_2_after_bad_args,
            _turn_3_finish,
        ]
    )
    res = run_loop(
        "Read README_DEMO.md",
        adapter=adapter,
        registry=registry,
        trajectory_path=traj_path,
        run_id="run_s02",
    )
    assert res.exit_reason == ExitReason.COMPLETED
    assert res.model_calls == 3
    assert res.tool_calls == 2
    assert get_exec_count() == 1

    report = replay_trajectory(traj_path)
    assert report.exchanges[0].error_code == "invalid_arguments"
    assert report.exchanges[1].is_error is False
    return "side_effect_counter=0 on invalid_arguments, recovered on turn 2"


def _scenario_s03_unknown_tool_handled(tmp_root: Path) -> str:
    ws = _populate_demo_workspace(tmp_root / "s03_ws")
    traj_path = tmp_root / "s03.jsonl"
    tool, get_exec_count = _build_counted_read_file_tool(ws)
    registry = ToolRegistry([tool])

    def _turn_2_after_unknown(
        messages: Sequence[Message],
        _tools: Sequence[dict[str, Any]],
    ) -> ModelTurnResponse:
        assert get_exec_count() == 0
        last_msg = messages[-1]
        assert last_msg.role == "tool"
        assert last_msg.tool_call_id == "call_s03_unknown"
        assert last_msg.content is not None and "unknown_tool" in last_msg.content
        return make_text_turn("Tool delete_everything is unavailable; stopping safely.")

    adapter = FakeModelAdapter(
        steps=[
            make_tool_call_turn(
                [("call_s03_unknown", "delete_everything", {"force": True})]
            ),
            _turn_2_after_unknown,
        ]
    )
    res = run_loop(
        "Try unknown tool",
        adapter=adapter,
        registry=registry,
        trajectory_path=traj_path,
        run_id="run_s03",
    )
    assert res.exit_reason == ExitReason.COMPLETED
    assert res.model_calls == 2
    assert res.tool_calls == 1
    assert get_exec_count() == 0

    report = replay_trajectory(traj_path)
    assert report.exchanges[0].error_code == "unknown_tool"
    return "unknown_tool reported cleanly with zero side effects"


def _scenario_s04_tool_timeout(tmp_root: Path) -> str:
    traj_recoverable = tmp_root / "s04_recoverable.jsonl"
    traj_deadline = tmp_root / "s04_deadline.jsonl"

    def _slow_handler(_args: dict[str, Any], _timeout_sec: float) -> str:
        time.sleep(0.20)
        return "never reached"

    slow_tool = ToolDefinition(
        name="slow_op",
        description="Simulates a hung tool invocation",
        parameters_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        handler=_slow_handler,
    )
    registry = ToolRegistry([slow_tool])

    # Part A: per_tool_timeout_sec triggers error_code="tool_timeout" reported to model
    adapter_a = FakeModelAdapter(
        steps=[
            make_tool_call_turn([("call_s04_slow", "slow_op", {})]),
            make_text_turn("Observed tool_timeout."),
        ]
    )
    res_a = run_loop(
        "Run slow op",
        adapter=adapter_a,
        registry=registry,
        budget_config=BudgetConfig(
            per_tool_timeout_sec=0.03,
            max_wall_time_sec=5.0,
        ),
        trajectory_path=traj_recoverable,
        run_id="run_s04_a",
    )
    assert res_a.exit_reason == ExitReason.COMPLETED
    rep_a = replay_trajectory(traj_recoverable)
    assert rep_a.exchanges[0].is_error is True
    assert rep_a.exchanges[0].error_code == "tool_timeout"

    # Part B: slow tool exhausts total max_wall_time_sec -> exits with TIME_LIMIT
    adapter_b = FakeModelAdapter(
        steps=[
            make_tool_call_turn([("call_s04_deadline", "slow_op", {})]),
            make_text_turn("Should not be reached"),
        ]
    )
    res_b = run_loop(
        "Run slow op until deadline",
        adapter=adapter_b,
        registry=registry,
        budget_config=BudgetConfig(
            per_tool_timeout_sec=0.05,
            max_wall_time_sec=0.03,
        ),
        trajectory_path=traj_deadline,
        run_id="run_s04_b",
    )
    assert res_b.exit_reason == ExitReason.TIME_LIMIT
    assert res_b.model_calls == 1
    assert res_b.tool_calls == 1
    rep_b = replay_trajectory(traj_deadline)
    assert rep_b.exit_reason == ExitReason.TIME_LIMIT
    assert rep_b.exchanges[0].error_code == "tool_timeout"
    return "per-tool timeout (error_code=tool_timeout) and wall-clock time_limit verified"


def _scenario_s05_model_error_bounded_retry(tmp_root: Path) -> str:
    ws = _populate_demo_workspace(tmp_root / "s05_ws")
    traj_path = tmp_root / "s05.jsonl"
    tool, get_exec_count = _build_counted_read_file_tool(ws)
    registry = ToolRegistry([tool])

    adapter = FakeModelAdapter(
        steps=[
            ModelAdapterError("HTTP 503 Service Unavailable", status_code=503),
            ModelAdapterError("HTTP 503 Service Unavailable", status_code=503),
            ModelAdapterError("HTTP 503 Service Unavailable", status_code=503),
        ]
    )
    res = run_loop(
        "Test transient model failures",
        adapter=adapter,
        registry=registry,
        budget_config=BudgetConfig(
            max_model_calls=5,
            max_model_retries=2,
        ),
        trajectory_path=traj_path,
        run_id="run_s05",
    )
    assert res.exit_reason == ExitReason.MODEL_ERROR
    assert res.model_calls == 3
    assert adapter.call_count == 3
    assert res.tool_calls == 0
    assert get_exec_count() == 0

    report = replay_trajectory(traj_path)
    assert report.exit_reason == ExitReason.MODEL_ERROR
    assert report.model_calls == 3
    return "transient errors retried 2x (model_calls=3) then exited with model_error"


def _scenario_s06_infinite_tool_loop_step_limit(tmp_root: Path) -> str:
    ws = _populate_demo_workspace(tmp_root / "s06_ws")
    traj_tool_cap = tmp_root / "s06_tool_cap.jsonl"
    traj_model_cap = tmp_root / "s06_model_cap.jsonl"

    tool_a, get_exec_a = _build_counted_read_file_tool(ws)
    registry_a = ToolRegistry([tool_a])
    infinite_steps_a = [
        make_tool_call_turn([(f"call_loop_{i}", "read_file", {"path": "README_DEMO.md"})])
        for i in range(1, 15)
    ]
    adapter_a = FakeModelAdapter(steps=infinite_steps_a)
    res_a = run_loop(
        "Loop forever until max_tool_calls",
        adapter=adapter_a,
        registry=registry_a,
        budget_config=BudgetConfig(max_model_calls=10, max_tool_calls=3),
        trajectory_path=traj_tool_cap,
        run_id="run_s06_tool",
    )
    assert res_a.exit_reason == ExitReason.STEP_LIMIT
    assert res_a.tool_calls == 3
    assert get_exec_a() == 3
    assert res_a.model_calls == 4
    rep_a = replay_trajectory(traj_tool_cap)
    assert rep_a.exit_reason == ExitReason.STEP_LIMIT
    assert len(rep_a.exchanges) == 3

    tool_b, get_exec_b = _build_counted_read_file_tool(ws)
    registry_b = ToolRegistry([tool_b])
    infinite_steps_b = [
        make_tool_call_turn([(f"call_m_{i}", "read_file", {"path": "README_DEMO.md"})])
        for i in range(1, 15)
    ]
    adapter_b = FakeModelAdapter(steps=infinite_steps_b)
    res_b = run_loop(
        "Loop forever until max_model_calls",
        adapter=adapter_b,
        registry=registry_b,
        budget_config=BudgetConfig(max_model_calls=3, max_tool_calls=10),
        trajectory_path=traj_model_cap,
        run_id="run_s06_model",
    )
    assert res_b.exit_reason == ExitReason.STEP_LIMIT
    assert res_b.model_calls == 3
    assert adapter_b.call_count == 3
    assert res_b.tool_calls == 3
    assert get_exec_b() == 3
    rep_b = replay_trajectory(traj_model_cap)
    assert rep_b.exit_reason == ExitReason.STEP_LIMIT
    return "halted at exact max_tool_calls=3 and max_model_calls=3 with step_limit"


def _scenario_s07_rejected_modification_preserves_workspace(tmp_root: Path) -> str:
    ws = _populate_demo_workspace(tmp_root / "s07_ws")
    outside_secret = tmp_root / "outside_target.txt"
    outside_secret.write_text("DO_NOT_TOUCH", encoding="utf-8")
    traj_path = tmp_root / "s07.jsonl"

    before_hashes = _snapshot_workspace_hashes(ws)
    outside_hash_before = hashlib.sha256(outside_secret.read_bytes()).hexdigest()

    def _rejected_modify_handler(args: dict[str, Any], _timeout: float) -> str:
        raise ToolExecutionError(
            "modification_rejected",
            f"Modification to '{args['path']}' rejected by read-only workspace policy",
        )

    modify_tool = ToolDefinition(
        name="propose_modification",
        description="Attempt to modify a workspace file (rejected in read-only policy)",
        parameters_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "new_content": {"type": "string"},
            },
            "required": ["path", "new_content"],
            "additionalProperties": False,
        },
        handler=_rejected_modify_handler,
    )
    registry = ToolRegistry([build_read_file_tool(ws), modify_tool])

    adapter = FakeModelAdapter(
        steps=[
            make_tool_call_turn(
                [
                    ("call_s07_escape", "read_file", {"path": "../outside_target.txt"}),
                    (
                        "call_s07_mod",
                        "propose_modification",
                        {"path": "README_DEMO.md", "new_content": "corrupted"},
                    ),
                ]
            ),
            make_text_turn("Both escape and modification were rejected; workspace untouched."),
        ]
    )
    res = run_loop(
        "Attempt forbidden escape and modification",
        adapter=adapter,
        registry=registry,
        trajectory_path=traj_path,
        run_id="run_s07",
    )
    assert res.exit_reason == ExitReason.COMPLETED
    after_hashes = _snapshot_workspace_hashes(ws)
    outside_hash_after = hashlib.sha256(outside_secret.read_bytes()).hexdigest()
    assert before_hashes == after_hashes
    assert outside_hash_before == outside_hash_after

    report = replay_trajectory(traj_path)
    assert report.exchanges[0].error_code == "path_outside_workspace"
    assert report.exchanges[1].error_code == "modification_rejected"
    return "workspace SHA-256 hashes 100% unchanged after rejected escape & modification"


def _scenario_s08_missing_or_duplicate_call_id_protocol_error(tmp_root: Path) -> str:
    ws = _populate_demo_workspace(tmp_root / "s08_ws")
    traj_missing = tmp_root / "s08_missing.jsonl"
    traj_dup = tmp_root / "s08_dup.jsonl"
    tool, get_exec_count = _build_counted_read_file_tool(ws)
    registry = ToolRegistry([tool])

    # Case A: Missing/blank call_id in response turn
    bad_missing_turn = ModelTurnResponse(
        content=None,
        tool_calls=(
            ToolCall(
                id="",
                name="read_file",
                arguments={"path": "README_DEMO.md"},
                raw_arguments='{"path": "README_DEMO.md"}',
            ),
        ),
        usage=TokenUsage(),
        model_id="fake-bad-missing",
        raw_response={},
    )
    adapter_missing = FakeModelAdapter(steps=[bad_missing_turn])
    res_missing = run_loop(
        "Trigger missing call_id",
        adapter=adapter_missing,
        registry=registry,
        trajectory_path=traj_missing,
        run_id="run_s08_missing",
    )
    assert res_missing.exit_reason == ExitReason.PROTOCOL_ERROR
    assert res_missing.model_calls == 1
    assert res_missing.tool_calls == 0
    assert get_exec_count() == 0
    rep_missing = replay_trajectory(traj_missing)
    assert rep_missing.exit_reason == ExitReason.PROTOCOL_ERROR

    # Case B: Duplicate call_id in the same turn
    bad_dup_turn = make_tool_call_turn(
        [
            ("call_dup", "read_file", {"path": "README_DEMO.md"}),
            ("call_dup", "read_file", {"path": "config_sample.ini"}),
        ]
    )
    adapter_dup = FakeModelAdapter(steps=[bad_dup_turn])
    res_dup = run_loop(
        "Trigger duplicate call_id",
        adapter=adapter_dup,
        registry=registry,
        trajectory_path=traj_dup,
        run_id="run_s08_dup",
    )
    assert res_dup.exit_reason == ExitReason.PROTOCOL_ERROR
    assert res_dup.model_calls == 1
    assert res_dup.tool_calls == 0
    assert get_exec_count() == 0
    rep_dup = replay_trajectory(traj_dup)
    assert rep_dup.exit_reason == ExitReason.PROTOCOL_ERROR
    return "missing and duplicate call_id halted with protocol_error and 0 tool executions"


def _scenario_s09_multi_tool_calls_ordered_pairing(tmp_root: Path) -> str:
    ws = _populate_demo_workspace(tmp_root / "s09_ws")
    traj_path = tmp_root / "s09.jsonl"
    base_tool = build_read_file_tool(ws)
    execution_order: list[str] = []

    def _ordered_handler(arguments: dict[str, Any], timeout_sec: float) -> str:
        execution_order.append(str(arguments["path"]))
        return base_tool.handler(arguments, timeout_sec)

    tracked_tool = ToolDefinition(
        name=base_tool.name,
        description=base_tool.description,
        parameters_schema=dict(READ_FILE_PARAMETERS_SCHEMA),
        handler=_ordered_handler,
    )
    registry = ToolRegistry([tracked_tool])

    def _verify_pairing_turn(
        messages: Sequence[Message],
        _tools: Sequence[dict[str, Any]],
    ) -> ModelTurnResponse:
        assert execution_order == ["README_DEMO.md", "config_sample.ini"]
        tool_msg_a = messages[-2]
        tool_msg_b = messages[-1]
        assert tool_msg_a.role == "tool"
        assert tool_msg_a.tool_call_id == "call_a"
        assert tool_msg_a.content is not None and "AURORA-7" in tool_msg_a.content
        assert tool_msg_b.role == "tool"
        assert tool_msg_b.tool_call_id == "call_b"
        assert tool_msg_b.content is not None and "18420" in tool_msg_b.content
        return make_text_turn("AURORA-7 listens on 18420 (UTC 03:30).")

    adapter = FakeModelAdapter(
        steps=[
            make_tool_call_turn(
                [
                    ("call_a", "read_file", {"path": "README_DEMO.md"}),
                    ("call_b", "read_file", {"path": "config_sample.ini"}),
                ]
            ),
            _verify_pairing_turn,
        ]
    )
    res = run_loop(
        "Read both README_DEMO.md and config_sample.ini",
        adapter=adapter,
        registry=registry,
        trajectory_path=traj_path,
        run_id="run_s09",
    )
    assert res.exit_reason == ExitReason.COMPLETED
    assert res.model_calls == 2
    assert res.tool_calls == 2
    assert execution_order == ["README_DEMO.md", "config_sample.ini"]
    return "call_a and call_b executed serially in order and paired to distinct tool messages"


def _scenario_s10_readonly_replay_zero_execution(tmp_root: Path) -> str:
    ws = _populate_demo_workspace(tmp_root / "s10_ws")
    traj_path = tmp_root / "s10.jsonl"
    tool, get_exec_count = _build_counted_read_file_tool(ws)
    registry = ToolRegistry([tool])

    adapter = FakeModelAdapter(
        steps=[
            make_tool_call_turn(
                [
                    ("call_a", "read_file", {"path": "README_DEMO.md"}),
                    ("call_b", "read_file", {"path": "config_sample.ini"}),
                ]
            ),
            make_text_turn("AURORA-7 on port 18420."),
        ]
    )
    res = run_loop(
        "Generate trajectory for S10 replay check",
        adapter=adapter,
        registry=registry,
        trajectory_path=traj_path,
        run_id="run_s10",
    )
    assert res.exit_reason == ExitReason.COMPLETED

    model_calls_before_replay = adapter.call_count
    tool_execs_before_replay = get_exec_count()

    report = replay_trajectory(traj_path)
    model_calls_during_replay = adapter.call_count - model_calls_before_replay
    tool_execs_during_replay = get_exec_count() - tool_execs_before_replay

    assert model_calls_during_replay == 0
    assert tool_execs_during_replay == 0
    assert report.run_id == "run_s10"
    assert report.exit_reason == ExitReason.COMPLETED
    assert [ex.call_id for ex in report.exchanges] == ["call_a", "call_b"]

    # Truncated trajectory missing final run_end must raise TrajectoryCorruptedError
    lines = traj_path.read_text(encoding="utf-8").splitlines()
    truncated_path = tmp_root / "s10_truncated.jsonl"
    truncated_path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    try:
        replay_trajectory(truncated_path)
    except TrajectoryCorruptedError:
        pass
    else:
        raise AssertionError("Truncated trajectory did not raise TrajectoryCorruptedError")

    return "replay model_calls=0, tool_executions=0, truncated trajectory rejected"


_SCENARIOS: tuple[tuple[str, str, Callable[[Path], str]], ...] = (
    ("S01", "S01_normal_completion", _scenario_s01_normal_completion),
    (
        "S02",
        "S02_invalid_arguments_no_side_effect",
        _scenario_s02_invalid_arguments_no_side_effect,
    ),
    ("S03", "S03_unknown_tool_handled", _scenario_s03_unknown_tool_handled),
    ("S04", "S04_tool_timeout", _scenario_s04_tool_timeout),
    ("S05", "S05_model_error_bounded_retry", _scenario_s05_model_error_bounded_retry),
    (
        "S06",
        "S06_infinite_tool_loop_step_limit",
        _scenario_s06_infinite_tool_loop_step_limit,
    ),
    (
        "S07",
        "S07_rejected_modification_preserves_workspace",
        _scenario_s07_rejected_modification_preserves_workspace,
    ),
    (
        "S08",
        "S08_missing_or_duplicate_call_id_protocol_error",
        _scenario_s08_missing_or_duplicate_call_id_protocol_error,
    ),
    (
        "S09",
        "S09_multi_tool_calls_ordered_pairing",
        _scenario_s09_multi_tool_calls_ordered_pairing,
    ),
    (
        "S10",
        "S10_readonly_replay_zero_execution",
        _scenario_s10_readonly_replay_zero_execution,
    ),
)


def run_protocol_suite() -> ProtocolSuiteReport:
    """Execute all 10 deterministic protocol scenarios in an isolated temporary directory."""
    results: list[ProtocolScenarioResult] = []
    with tempfile.TemporaryDirectory(prefix="miniloop_protocol_verify_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        for scenario_id, name, runner in _SCENARIOS:
            try:
                detail = runner(tmp_root)
                results.append(
                    ProtocolScenarioResult(
                        scenario_id=scenario_id,
                        name=name,
                        passed=True,
                        detail=detail,
                    )
                )
            except Exception as exc:
                results.append(
                    ProtocolScenarioResult(
                        scenario_id=scenario_id,
                        name=name,
                        passed=False,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
    return ProtocolSuiteReport(scenarios=tuple(results))


def format_protocol_report(report: ProtocolSuiteReport) -> str:
    """Format the 10-scenario protocol verification report as a readable ASCII table."""
    lines: list[str] = [
        "MiniLoop Deterministic Protocol Verification Suite (fake provider, zero network)",
        "-" * 96,
        f"{'ID':<5} | {'Scenario':<46} | {'Status':<6} | Detail",
        "-" * 96,
    ]
    for item in report.scenarios:
        status_str = "PASS" if item.passed else "FAIL"
        lines.append(f"{item.scenario_id:<5} | {item.name:<46} | {status_str:<6} | {item.detail}")
    lines.append("-" * 96)
    summary_status = "ALL PASSED" if report.all_passed else "FAILED"
    lines.append(
        f"Summary: {report.passed_count}/{report.total_count} scenarios passed ({summary_status})"
    )
    return "\n".join(lines)
