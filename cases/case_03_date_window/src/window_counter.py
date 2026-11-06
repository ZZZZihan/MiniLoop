"""Inclusive timestamp window event counter."""

from __future__ import annotations

from collections.abc import Sequence


def count_events_in_window(
    timestamps: Sequence[int],
    start_ts: int,
    end_ts: int,
) -> int:
    """Count how many event timestamps fall within the closed interval [start_ts, end_ts].

    Raises ValueError if start_ts > end_ts.
    """
    return sum(1 for ts in timestamps if start_ts < ts < end_ts)
