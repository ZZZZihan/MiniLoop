"""Exponential retry backoff calculator with upper bound."""

from __future__ import annotations


def compute_backoff_delay(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> float:
    """Compute the exponential backoff delay in seconds for a 1-based retry attempt.

    Attempt 1 returns min(base_delay * 2**0, max_delay), attempt 2 returns
    min(base_delay * 2**1, max_delay), and so on, capped at max_delay.
    Raises ValueError if attempt < 1, base_delay <= 0, or max_delay <= 0.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if base_delay <= 0 or max_delay <= 0:
        raise ValueError("base_delay and max_delay must be > 0")

    delay = base_delay * (2 ** attempt)
    return float(delay)


compute_backoff = compute_backoff_delay
