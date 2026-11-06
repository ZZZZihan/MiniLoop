"""Unit tests for 1-based exponential retry backoff calculation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backoff import compute_backoff_delay  # noqa: E402


def test_first_attempt_uses_zero_exponent() -> None:
    assert compute_backoff_delay(1, base_delay=1.0, max_delay=60.0) == 1.0
    assert compute_backoff_delay(1, base_delay=2.5, max_delay=60.0) == 2.5


def test_exponential_doubling_across_attempts() -> None:
    delays = [compute_backoff_delay(a, base_delay=0.5, max_delay=60.0) for a in (1, 2, 3, 4)]
    assert delays == [0.5, 1.0, 2.0, 4.0]


def test_delay_is_capped_at_max_delay() -> None:
    assert compute_backoff_delay(6, base_delay=2.0, max_delay=30.0) == 30.0
    assert compute_backoff_delay(1, base_delay=10.0, max_delay=5.0) == 5.0


def test_invalid_attempt_or_non_positive_delays_raise_value_error() -> None:
    with pytest.raises(ValueError, match="attempt must be >= 1"):
        compute_backoff_delay(0)
    with pytest.raises(ValueError, match="must be > 0"):
        compute_backoff_delay(1, base_delay=0.0)
    with pytest.raises(ValueError, match="must be > 0"):
        compute_backoff_delay(1, base_delay=1.0, max_delay=-1.0)
