"""Core frozen data contracts and enums shared across MiniLoop modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


class ExitReason(StrEnum):
    """Explicit terminal states for a MiniLoop execution run."""

    COMPLETED = "completed"
    STEP_LIMIT = "step_limit"
    TIME_LIMIT = "time_limit"
    MODEL_ERROR = "model_error"
    PROTOCOL_ERROR = "protocol_error"
    CANCELLED = "cancelled"
    RUNTIME_ERROR = "runtime_error"


@dataclass(frozen=True)
class ToolCall:
    """A single structured tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = "{}"


@dataclass(frozen=True)
class ToolResult:
    """The result of executing or validating a tool call, paired by call_id."""

    call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    error_code: str | None = None
    duration_ms: float = 0.0


@dataclass(frozen=True)
class TokenUsage:
    """Token accounting reported by a model response."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class Message:
    """A single conversation turn in the loop history."""

    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ModelTurnResponse:
    """Normalized model output for a single assistant turn."""

    content: str | None
    tool_calls: tuple[ToolCall, ...]
    usage: TokenUsage
    model_id: str
    raw_response: dict[str, Any]
    duration_ms: float = 0.0
    attempts: int = 1


@dataclass(frozen=True)
class BudgetConfig:
    """Step, tool, retry, and wall-clock budget limits for a run."""

    max_model_calls: int = 10
    max_tool_calls: int = 15
    max_wall_time_sec: float = 120.0
    per_request_timeout_sec: float = 30.0
    per_tool_timeout_sec: float = 15.0
    max_model_retries: int = 2


@dataclass(frozen=True)
class TrajectoryEvent:
    """A single event record in a Schema v1.0 JSONL trajectory."""

    schema_version: str
    run_id: str
    sequence: int
    timestamp: str
    event_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class RunResult:
    """Summary outcome returned when the execution loop terminates."""

    run_id: str
    exit_reason: ExitReason
    final_answer: str | None
    model_calls: int
    tool_calls: int
    wall_time_sec: float
    trajectory_path: Path | None = None
    error_detail: str | None = None
