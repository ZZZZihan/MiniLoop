"""Serial tool-calling execution loop with deadline budget control and trajectory recording."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path

from miniloop.adapter import ModelAdapter, ModelAdapterError, ProtocolViolationError
from miniloop.budget import BudgetTracker
from miniloop.secrets import SecretValue
from miniloop.tools.registry import ToolRegistry
from miniloop.trajectory import TrajectoryRecorder
from miniloop.types import (
     BudgetConfig,
    ExitReason,
    Message,
    ModelTurnResponse,
    RunResult,
    ToolCall,
)


def _is_retryable_model_error(exc: ModelAdapterError) -> bool:
    """Return True if the model error is transient and eligible for retry."""
    if exc.status_code is None:
        return True
    return exc.status_code == 429 or exc.status_code >= 500


def _validate_turn_tool_calls(
    tool_calls: Sequence[ToolCall],
    run_seen_call_ids: set[str],
) -> None:
    """Double-check that every tool call has a valid structure and a run-wide unique ID."""
    turn_seen_ids: set[str] = set()
    for tc in tool_calls:
        if not isinstance(tc.id, str) or not tc.id.strip():
            raise ProtocolViolationError("tool_call missing non-empty string 'id'")
        if tc.id in turn_seen_ids:
            raise ProtocolViolationError(f"Duplicate tool_call id in single turn: {tc.id!r}")
        if tc.id in run_seen_call_ids:
            raise ProtocolViolationError(
                f"Duplicate tool_call id across turns in run: {tc.id!r}"
            )
        turn_seen_ids.add(tc.id)
        if not isinstance(tc.name, str) or not tc.name.strip():
            raise ProtocolViolationError("tool_call missing non-empty string 'name'")
        if not isinstance(tc.arguments, dict):
            raise ProtocolViolationError("tool_call 'arguments' must be a dict")
    run_seen_call_ids.update(turn_seen_ids)


def _collect_redaction_secrets(
    adapter: ModelAdapter,
    extra_secrets: Sequence[str],
) -> tuple[str, ...]:
    """Gather explicit secrets and any SecretValue attached to the adapter."""
    collected: list[str] = list(extra_secrets)
    adapter_key = getattr(adapter, "api_key", None)
    if isinstance(adapter_key, SecretValue):
        collected.append(adapter_key.get_secret_value())
    return tuple(collected)


_DEFAULT_BUDGET_CONFIG = BudgetConfig()


def run_loop(
    task: str,
    *,
    adapter: ModelAdapter,
    registry: ToolRegistry,
    budget_config: BudgetConfig = _DEFAULT_BUDGET_CONFIG,
    system_prompt: str | None = None,
    trajectory_path: Path | None = None,
    run_id: str | None = None,
    secrets_to_redact: Sequence[str] = (),
    clock: Callable[[], float] = time.monotonic,
) -> RunResult:
    """Execute the serial model-tool loop until completion, budget stop, or error."""
    resolved_run_id = (
        run_id.strip()
        if isinstance(run_id, str) and run_id.strip()
        else f"run_{uuid.uuid4().hex[:12]}"
    )
    tracker = BudgetTracker(budget_config, clock=clock)
    redaction_secrets = _collect_redaction_secrets(adapter, secrets_to_redact)

    recorder: TrajectoryRecorder | None = None
    if trajectory_path is not None:
        recorder = TrajectoryRecorder(
            trajectory_path,
            run_id=resolved_run_id,
            secrets=redaction_secrets,
        )

    def _record(event_type: str, payload: dict[str, object]) -> None:
        if recorder is not None:
            recorder.record(event_type, payload)

    messages: list[Message] = []
    if system_prompt is not None and system_prompt != "":
        messages.append(Message(role="system", content=system_prompt))
    messages.append(Message(role="user", content=task))

    _record(
        "run_start",
        {
            "task": task,
            "provider": adapter.provider,
            "model_name": adapter.model_name,
            "tools": [tool.name for tool in registry.list_tools()],
            "budget": asdict(budget_config),
        },
    )

    openai_tools = registry.to_openai_tools()
    run_seen_call_ids: set[str] = set()
    turn_idx = 0

    while True:
        turn_idx += 1

        # Step A: Pre-model-call budget check
        stop_reason = tracker.check_before_model_call()
        if stop_reason is not None:
            detail = f"Budget limit reached before model call: {stop_reason.value}"
            _record(
                "budget_stop",
                {
                    "stage": "before_model_call",
                    "exit_reason": stop_reason.value,
                    "model_calls": tracker.model_calls,
                    "tool_calls": tracker.tool_calls,
                    "elapsed_sec": tracker.elapsed_sec,
                },
            )
            _record(
                "run_end",
                {
                    "exit_reason": stop_reason.value,
                    "final_answer": None,
                    "model_calls": tracker.model_calls,
                    "tool_calls": tracker.tool_calls,
                    "wall_time_sec": tracker.elapsed_sec,
                    "error": detail,
                },
            )
            return RunResult(
                run_id=resolved_run_id,
                exit_reason=stop_reason,
                final_answer=None,
                model_calls=tracker.model_calls,
                tool_calls=tracker.tool_calls,
                wall_time_sec=tracker.elapsed_sec,
                trajectory_path=trajectory_path,
                error_detail=detail,
            )

        # Step B: Request model turn
        try:
            req_timeout = tracker.next_model_timeout()
        except TimeoutError as exc:
            detail = str(exc)
            _record(
                "budget_stop",
                {
                    "stage": "before_model_call",
                    "exit_reason": ExitReason.TIME_LIMIT.value,
                    "model_calls": tracker.model_calls,
                    "tool_calls": tracker.tool_calls,
                    "elapsed_sec": tracker.elapsed_sec,
                },
            )
            _record(
                "run_end",
                {
                    "exit_reason": ExitReason.TIME_LIMIT.value,
                    "final_answer": None,
                    "model_calls": tracker.model_calls,
                    "tool_calls": tracker.tool_calls,
                    "wall_time_sec": tracker.elapsed_sec,
                    "error": detail,
                },
            )
            return RunResult(
                run_id=resolved_run_id,
                exit_reason=ExitReason.TIME_LIMIT,
                final_answer=None,
                model_calls=tracker.model_calls,
                tool_calls=tracker.tool_calls,
                wall_time_sec=tracker.elapsed_sec,
                trajectory_path=trajectory_path,
                error_detail=detail,
            )

        allowed_retries = tracker.remaining_retries_allowed()
        max_attempts = 1 + allowed_retries
        _record(
            "model_request",
            {
                "turn": turn_idx,
                "message_count": len(messages),
                "timeout_sec": req_timeout,
                "max_retries": allowed_retries,
            },
        )

        turn_resp: ModelTurnResponse | None = None
        attempt = 0
        for attempt in range(1, max_attempts + 1):
            if tracker.remaining_sec <= 0:
                detail = "Wall time budget exhausted before model retry"
                _record(
                    "budget_stop",
                    {
                        "stage": "before_model_call",
                        "exit_reason": ExitReason.TIME_LIMIT.value,
                        "model_calls": tracker.model_calls,
                        "tool_calls": tracker.tool_calls,
                        "elapsed_sec": tracker.elapsed_sec,
                    },
                )
                _record(
                    "run_end",
                    {
                        "exit_reason": ExitReason.TIME_LIMIT.value,
                        "final_answer": None,
                        "model_calls": tracker.model_calls,
                        "tool_calls": tracker.tool_calls,
                        "wall_time_sec": tracker.elapsed_sec,
                        "error": detail,
                    },
                )
                return RunResult(
                    run_id=resolved_run_id,
                    exit_reason=ExitReason.TIME_LIMIT,
                    final_answer=None,
                    model_calls=tracker.model_calls,
                    tool_calls=tracker.tool_calls,
                    wall_time_sec=tracker.elapsed_sec,
                    trajectory_path=trajectory_path,
                    error_detail=detail,
                )

            try:
                attempt_timeout = tracker.next_model_timeout()
            except TimeoutError as exc:
                detail = str(exc)
                _record(
                    "budget_stop",
                    {
                        "stage": "before_model_call",
                        "exit_reason": ExitReason.TIME_LIMIT.value,
                        "model_calls": tracker.model_calls,
                        "tool_calls": tracker.tool_calls,
                        "elapsed_sec": tracker.elapsed_sec,
                    },
                )
                _record(
                    "run_end",
                    {
                        "exit_reason": ExitReason.TIME_LIMIT.value,
                        "final_answer": None,
                        "model_calls": tracker.model_calls,
                        "tool_calls": tracker.tool_calls,
                        "wall_time_sec": tracker.elapsed_sec,
                        "error": detail,
                    },
                )
                return RunResult(
                    run_id=resolved_run_id,
                    exit_reason=ExitReason.TIME_LIMIT,
                    final_answer=None,
                    model_calls=tracker.model_calls,
                    tool_calls=tracker.tool_calls,
                    wall_time_sec=tracker.elapsed_sec,
                    trajectory_path=trajectory_path,
                    error_detail=detail,
                )

            tracker.record_model_calls(1)
            try:
                turn_resp = adapter.generate(
                    messages,
                    openai_tools,
                    timeout_sec=attempt_timeout,
                    max_retries=0,
                )
                break
            except ModelAdapterError as exc:
                if tracker.remaining_sec <= 0:
                    _record(
                        "budget_stop",
                        {
                            "stage": "after_model_call",
                            "exit_reason": ExitReason.TIME_LIMIT.value,
                            "model_calls": tracker.model_calls,
                            "tool_calls": tracker.tool_calls,
                            "elapsed_sec": tracker.elapsed_sec,
                        },
                    )
                    _record(
                        "run_end",
                        {
                            "exit_reason": ExitReason.TIME_LIMIT.value,
                            "final_answer": None,
                            "model_calls": tracker.model_calls,
                            "tool_calls": tracker.tool_calls,
                            "wall_time_sec": tracker.elapsed_sec,
                            "error": str(exc),
                        },
                    )
                    return RunResult(
                        run_id=resolved_run_id,
                        exit_reason=ExitReason.TIME_LIMIT,
                        final_answer=None,
                        model_calls=tracker.model_calls,
                        tool_calls=tracker.tool_calls,
                        wall_time_sec=tracker.elapsed_sec,
                        trajectory_path=trajectory_path,
                        error_detail=str(exc),
                    )
                if not _is_retryable_model_error(exc) or attempt >= max_attempts:
                    _record(
                        "run_end",
                        {
                            "exit_reason": ExitReason.MODEL_ERROR.value,
                            "final_answer": None,
                            "model_calls": tracker.model_calls,
                            "tool_calls": tracker.tool_calls,
                            "wall_time_sec": tracker.elapsed_sec,
                            "error": str(exc),
                        },
                    )
                    return RunResult(
                        run_id=resolved_run_id,
                        exit_reason=ExitReason.MODEL_ERROR,
                        final_answer=None,
                        model_calls=tracker.model_calls,
                        tool_calls=tracker.tool_calls,
                        wall_time_sec=tracker.elapsed_sec,
                        trajectory_path=trajectory_path,
                        error_detail=str(exc),
                    )
                continue
            except ProtocolViolationError as exc:
                _record(
                    "run_end",
                    {
                        "exit_reason": ExitReason.PROTOCOL_ERROR.value,
                        "final_answer": None,
                        "model_calls": tracker.model_calls,
                        "tool_calls": tracker.tool_calls,
                        "wall_time_sec": tracker.elapsed_sec,
                        "error": str(exc),
                    },
                )
                return RunResult(
                    run_id=resolved_run_id,
                    exit_reason=ExitReason.PROTOCOL_ERROR,
                    final_answer=None,
                    model_calls=tracker.model_calls,
                    tool_calls=tracker.tool_calls,
                    wall_time_sec=tracker.elapsed_sec,
                    trajectory_path=trajectory_path,
                    error_detail=str(exc),
                )

        assert turn_resp is not None
        _record(
            "model_response",
            {
                "model_id": turn_resp.model_id,
                "content": turn_resp.content,
                "tool_calls": [asdict(tc) for tc in turn_resp.tool_calls],
                "usage": asdict(turn_resp.usage),
                "duration_ms": turn_resp.duration_ms,
                "attempts": max(attempt, turn_resp.attempts),
            },
        )

        if tracker.remaining_sec <= 0:
            detail = "Wall time budget exhausted during model call"
            _record(
                "budget_stop",
                {
                    "stage": "after_model_call",
                    "exit_reason": ExitReason.TIME_LIMIT.value,
                    "model_calls": tracker.model_calls,
                    "tool_calls": tracker.tool_calls,
                    "elapsed_sec": tracker.elapsed_sec,
                },
            )
            _record(
                "run_end",
                {
                    "exit_reason": ExitReason.TIME_LIMIT.value,
                    "final_answer": None,
                    "model_calls": tracker.model_calls,
                    "tool_calls": tracker.tool_calls,
                    "wall_time_sec": tracker.elapsed_sec,
                    "error": detail,
                },
            )
            return RunResult(
                run_id=resolved_run_id,
                exit_reason=ExitReason.TIME_LIMIT,
                final_answer=None,
                model_calls=tracker.model_calls,
                tool_calls=tracker.tool_calls,
                wall_time_sec=tracker.elapsed_sec,
                trajectory_path=trajectory_path,
                error_detail=detail,
            )

        # Step C: Termination check or serial tool execution
        if not turn_resp.tool_calls:
            messages.append(
                Message(
                    role="assistant",
                    content=turn_resp.content,
                    tool_calls=(),
                )
            )
            _record(
                "run_end",
                {
                    "exit_reason": ExitReason.COMPLETED.value,
                    "final_answer": turn_resp.content,
                    "model_calls": tracker.model_calls,
                    "tool_calls": tracker.tool_calls,
                    "wall_time_sec": tracker.elapsed_sec,
                },
            )
            return RunResult(
                run_id=resolved_run_id,
                exit_reason=ExitReason.COMPLETED,
                final_answer=turn_resp.content,
                model_calls=tracker.model_calls,
                tool_calls=tracker.tool_calls,
                wall_time_sec=tracker.elapsed_sec,
                trajectory_path=trajectory_path,
            )

        try:
            _validate_turn_tool_calls(turn_resp.tool_calls, run_seen_call_ids)
        except ProtocolViolationError as exc:
            _record(
                "run_end",
                {
                    "exit_reason": ExitReason.PROTOCOL_ERROR.value,
                    "final_answer": None,
                    "model_calls": tracker.model_calls,
                    "tool_calls": tracker.tool_calls,
                    "wall_time_sec": tracker.elapsed_sec,
                    "error": str(exc),
                },
            )
            return RunResult(
                run_id=resolved_run_id,
                exit_reason=ExitReason.PROTOCOL_ERROR,
                final_answer=None,
                model_calls=tracker.model_calls,
                tool_calls=tracker.tool_calls,
                wall_time_sec=tracker.elapsed_sec,
                trajectory_path=trajectory_path,
                error_detail=str(exc),
            )

        messages.append(
            Message(
                role="assistant",
                content=turn_resp.content,
                tool_calls=turn_resp.tool_calls,
            )
        )

        for tc in turn_resp.tool_calls:
            tool_stop = tracker.check_before_tool_call()
            if tool_stop is not None:
                detail = f"Budget limit reached before tool call: {tool_stop.value}"
                _record(
                    "budget_stop",
                    {
                        "stage": "before_tool_call",
                        "exit_reason": tool_stop.value,
                        "model_calls": tracker.model_calls,
                        "tool_calls": tracker.tool_calls,
                        "elapsed_sec": tracker.elapsed_sec,
                    },
                )
                _record(
                    "run_end",
                    {
                        "exit_reason": tool_stop.value,
                        "final_answer": None,
                        "model_calls": tracker.model_calls,
                        "tool_calls": tracker.tool_calls,
                        "wall_time_sec": tracker.elapsed_sec,
                        "error": detail,
                    },
                )
                return RunResult(
                    run_id=resolved_run_id,
                    exit_reason=tool_stop,
                    final_answer=None,
                    model_calls=tracker.model_calls,
                    tool_calls=tracker.tool_calls,
                    wall_time_sec=tracker.elapsed_sec,
                    trajectory_path=trajectory_path,
                    error_detail=detail,
                )

            try:
                t_timeout = tracker.next_tool_timeout()
            except TimeoutError as exc:
                detail = str(exc)
                _record(
                    "budget_stop",
                    {
                        "stage": "before_tool_call",
                        "exit_reason": ExitReason.TIME_LIMIT.value,
                        "model_calls": tracker.model_calls,
                        "tool_calls": tracker.tool_calls,
                        "elapsed_sec": tracker.elapsed_sec,
                    },
                )
                _record(
                    "run_end",
                    {
                        "exit_reason": ExitReason.TIME_LIMIT.value,
                        "final_answer": None,
                        "model_calls": tracker.model_calls,
                        "tool_calls": tracker.tool_calls,
                        "wall_time_sec": tracker.elapsed_sec,
                        "error": detail,
                    },
                )
                return RunResult(
                    run_id=resolved_run_id,
                    exit_reason=ExitReason.TIME_LIMIT,
                    final_answer=None,
                    model_calls=tracker.model_calls,
                    tool_calls=tracker.tool_calls,
                    wall_time_sec=tracker.elapsed_sec,
                    trajectory_path=trajectory_path,
                    error_detail=detail,
                )

            _record(
                "tool_call",
                {
                    "call_id": tc.id,
                    "tool_name": tc.name,
                    "arguments": tc.arguments,
                },
            )
            tracker.record_tool_call()
            res = registry.execute(tc, timeout_sec=t_timeout)
            _record(
                "tool_result",
                {
                    "call_id": res.call_id,
                    "tool_name": res.tool_name,
                    "content": res.content,
                    "is_error": res.is_error,
                    "error_code": res.error_code,
                    "duration_ms": res.duration_ms,
                },
            )

            if res.error_code == "tool_timeout" and tracker.remaining_sec <= 0:
                _record(
                    "run_end",
                    {
                        "exit_reason": ExitReason.TIME_LIMIT.value,
                        "final_answer": None,
                        "model_calls": tracker.model_calls,
                        "tool_calls": tracker.tool_calls,
                        "wall_time_sec": tracker.elapsed_sec,
                        "error": res.content,
                    },
                )
                return RunResult(
                    run_id=resolved_run_id,
                    exit_reason=ExitReason.TIME_LIMIT,
                    final_answer=None,
                    model_calls=tracker.model_calls,
                    tool_calls=tracker.tool_calls,
                    wall_time_sec=tracker.elapsed_sec,
                    trajectory_path=trajectory_path,
                    error_detail=res.content,
                )

            messages.append(
                Message(
                    role="tool",
                    tool_call_id=res.call_id,
                    name=res.tool_name,
                    content=res.content,
                )
            )
