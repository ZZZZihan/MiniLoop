"""Wall-clock deadline and step/tool call budget controller."""

from __future__ import annotations

import time
from collections.abc import Callable

from miniloop.types import BudgetConfig, ExitReason


class BudgetTracker:
    """Tracks model calls, tool calls, and wall-clock deadline for a single run."""

    def __init__(
        self,
        config: BudgetConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config: BudgetConfig = config
        self._clock: Callable[[], float] = clock
        self._start: float = clock()
        self._deadline: float = self._start + config.max_wall_time_sec
        self.model_calls: int = 0
        self.tool_calls: int = 0

    @property
    def elapsed_sec(self) -> float:
        """Return elapsed wall-clock seconds since tracker creation (clamped >= 0)."""
        return max(0.0, self._clock() - self._start)

    @property
    def remaining_sec(self) -> float:
        """Return remaining wall-clock seconds before the run deadline."""
        return self._deadline - self._clock()

    def check_before_model_call(self) -> ExitReason | None:
        """Return an ``ExitReason`` if time or model step limits are exhausted."""
        if self.remaining_sec <= 0:
            return ExitReason.TIME_LIMIT
        if self.model_calls >= self.config.max_model_calls:
            return ExitReason.STEP_LIMIT
        return None

    def check_before_tool_call(self) -> ExitReason | None:
        """Return an ``ExitReason`` if time or tool call limits are exhausted."""
        if self.remaining_sec <= 0:
            return ExitReason.TIME_LIMIT
        if self.tool_calls >= self.config.max_tool_calls:
            return ExitReason.STEP_LIMIT
        return None

    def next_model_timeout(self) -> float:
        """Return timeout in seconds for the next model request."""
        remaining = self.remaining_sec
        if remaining <= 0:
            raise TimeoutError("Wall time budget exhausted")
        return min(self.config.per_request_timeout_sec, remaining)

    def next_tool_timeout(self) -> float:
        """Return timeout in seconds for the next tool invocation."""
        remaining = self.remaining_sec
        if remaining <= 0:
            raise TimeoutError("Wall time budget exhausted")
        return min(self.config.per_tool_timeout_sec, remaining)

    def record_model_calls(self, count: int = 1) -> None:
        """Record ``count`` model call attempts (including retries)."""
        if count < 0:
            raise ValueError(f"count must be >= 0, got {count}")
        self.model_calls += count

    def record_model_attempt(self, count: int = 1) -> None:
        """Alias for ``record_model_calls``."""
        self.record_model_calls(count)

    def record_tool_call(self, count: int = 1) -> None:
        """Record ``count`` tool invocations."""
        if count < 0:
            raise ValueError(f"count must be >= 0, got {count}")
        self.tool_calls += count

    def remaining_retries_allowed(self) -> int:
        """Return max retries allowed for the current model turn within budget."""
        return max(
            0,
            min(
                self.config.max_model_retries,
                self.config.max_model_calls - self.model_calls - 1,
            ),
        )
