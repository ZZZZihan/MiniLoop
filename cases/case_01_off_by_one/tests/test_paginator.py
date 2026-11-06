"""Unit tests for 1-based sequence pagination."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.paginator import paginate  # noqa: E402


def test_first_page_returns_initial_slice() -> None:
    items = ["a", "b", "c", "d", "e"]
    assert paginate(items, page=1, page_size=2) == ["a", "b"]


def test_middle_and_tail_pages() -> None:
    items = [10, 20, 30, 40, 50]
    assert paginate(items, page=2, page_size=2) == [30, 40]
    assert paginate(items, page=3, page_size=2) == [50]


def test_page_beyond_end_returns_empty_list() -> None:
    items = [1, 2, 3]
    assert paginate(items, page=2, page_size=3) == []
    assert paginate(items, page=5, page_size=2) == []


def test_single_item_pages_cover_entire_sequence() -> None:
    items = ["x", "y", "z"]
    pages = [paginate(items, page=p, page_size=1) for p in (1, 2, 3)]
    assert pages == [["x"], ["y"], ["z"]]


def test_invalid_page_or_page_size_raises_value_error() -> None:
    with pytest.raises(ValueError, match="page must be >= 1"):
        paginate([1, 2, 3], page=0, page_size=2)
    with pytest.raises(ValueError, match="page_size must be >= 1"):
        paginate([1, 2, 3], page=1, page_size=0)
