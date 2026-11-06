"""Unit tests for comma-separated key=value line parsing with quoted values."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.kv_parser import parse_kv_line  # noqa: E402


def test_unquoted_key_value_pairs() -> None:
    assert parse_kv_line("host=localhost,port=8080,env=dev") == {
        "host": "localhost",
        "port": "8080",
        "env": "dev",
    }


def test_quoted_values_containing_commas_and_equals() -> None:
    line = 'host=localhost,note="a,b,c",query="k1=v1,k2=v2",port=8080'
    assert parse_kv_line(line) == {
        "host": "localhost",
        "note": "a,b,c",
        "query": "k1=v1,k2=v2",
        "port": "8080",
    }


def test_strips_outer_quotes_and_handles_empty_quoted_value() -> None:
    line = 'service="gateway",prefix="",region=us-east'
    assert parse_kv_line(line) == {
        "service": "gateway",
        "prefix": "",
        "region": "us-east",
    }


def test_empty_line_and_malformed_segments() -> None:
    assert parse_kv_line("   ") == {}
    with pytest.raises(ValueError):
        parse_kv_line("host=localhost,invalid_segment")
    with pytest.raises(ValueError):
        parse_kv_line("=missing_key")
