"""Unit tests for SimpleLRU cache eviction and recency refresh."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.simple_lru import SimpleLRU  # noqa: E402


def test_put_and_get_within_capacity() -> None:
    cache: SimpleLRU[str, int] = SimpleLRU(capacity=2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert len(cache) == 2
    assert cache.get("a") == 1
    assert cache.get("b") == 2


def test_get_refreshes_key_recency_before_eviction() -> None:
    cache: SimpleLRU[str, int] = SimpleLRU(capacity=2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1  # "a" becomes most recently used; "b" is now LRU
    cache.put("c", 3)  # should evict "b", not "a"
    assert cache.get("b") is None
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_updating_existing_key_refreshes_recency() -> None:
    cache: SimpleLRU[str, int] = SimpleLRU(capacity=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)  # updating "a" marks it as most recently used
    cache.put("c", 3)  # should evict "b", keeping updated "a"
    assert cache.get("b") is None
    assert cache.get("a") == 10
    assert cache.get("c") == 3
    assert cache.keys_in_lru_order() == ["a", "c"]


def test_missing_key_returns_default_and_invalid_capacity_raises() -> None:
    cache: SimpleLRU[str, int] = SimpleLRU(capacity=2)
    cache.put("x", 100)
    assert cache.get("missing", default=-1) == -1
    assert cache.keys_in_lru_order() == ["x"]
    with pytest.raises(ValueError, match="capacity must be >= 1"):
        SimpleLRU(capacity=0)
