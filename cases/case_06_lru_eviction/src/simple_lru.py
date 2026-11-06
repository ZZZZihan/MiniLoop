"""Fixed-capacity Least Recently Used (LRU) cache backed by OrderedDict."""

from __future__ import annotations

from collections import OrderedDict


class SimpleLRU[K, V]:
    """A simple fixed-capacity LRU cache."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._store: OrderedDict[K, V] = OrderedDict()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._store)

    def get(self, key: K, default: V | None = None) -> V | None:
        """Return the value for key if present and mark it as most recently used."""
        if key not in self._store:
            return default
        return self._store[key]

    def put(self, key: K, value: V) -> None:
        """Insert or update key with value, evicting the least recently used item if full."""
        if key in self._store:
            self._store[key] = value
            return
        if len(self._store) >= self._capacity:
            self._store.popitem(last=False)
        self._store[key] = value

    def keys_in_lru_order(self) -> list[K]:
        """Return keys ordered from least recently used to most recently used."""
        return list(self._store.keys())
