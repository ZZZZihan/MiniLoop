"""Unit tests for closed-interval timestamp window counting."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.window_counter import count_events_in_window  # noqa: E402


def test_counts_events_strictly_inside_window() -> None:
    timestamps = [100, 120, 150, 180, 220]
    assert count_events_in_window(timestamps, start_ts=110, end_ts=200) == 3


def test_includes_exact_start_and_end_boundary_timestamps() -> None:
    timestamps = [99, 100, 150, 200, 201]
    assert count_events_in_window(timestamps, start_ts=100, end_ts=200) == 3


def test_zero_width_window_counts_exact_matches() -> None:
    timestamps = [50, 60, 60, 70]
    assert count_events_in_window(timestamps, start_ts=60, end_ts=60) == 2
    assert count_events_in_window(timestamps, start_ts=65, end_ts=65) == 0


def test_start_greater_than_end_raises_value_error() -> None:
    with pytest.raises(ValueError):
        count_events_in_window([100, 200], start_ts=201, end_ts=200)
