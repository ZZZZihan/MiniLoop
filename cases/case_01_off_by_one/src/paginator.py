"""1-based sequence pagination helper."""

from __future__ import annotations

from collections.abc import Sequence


def paginate[T](items: Sequence[T], page: int, page_size: int) -> list[T]:
    """Return the 1-based page slice from items.

    Raises ValueError if page < 1 or page_size < 1.
    Returns an empty list when the requested page is past the end of items.
    """
    if page < 1:
        raise ValueError("page must be >= 1")
    if page_size < 1:
        raise ValueError("page_size must be >= 1")

    start = page * page_size
    end = start + page_size
    return list(items[start:end])
