"""Unit tests for configuration dictionary merging with falsy overrides."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_merge import merge_config  # noqa: E402


def test_missing_and_none_overrides_fall_back_to_defaults() -> None:
    defaults = {"host": "127.0.0.1", "port": 8080, "retries": 3}
    overrides = {"port": None, "host": "localhost"}
    assert merge_config(overrides, defaults) == {
        "host": "localhost",
        "port": 8080,
        "retries": 3,
    }


def test_zero_integer_and_float_overrides_are_preserved() -> None:
    defaults = {"retries": 3, "timeout_sec": 5.0}
    overrides = {"retries": 0, "timeout_sec": 0.0}
    assert merge_config(overrides, defaults) == {
        "retries": 0,
        "timeout_sec": 0.0,
    }


def test_false_boolean_and_empty_string_overrides_are_preserved() -> None:
    defaults = {"verify_tls": True, "prefix": "/api", "tags": ["v1"]}
    overrides = {"verify_tls": False, "prefix": "", "tags": []}
    assert merge_config(overrides, defaults) == {
        "verify_tls": False,
        "prefix": "",
        "tags": [],
    }


def test_extra_non_none_keys_from_overrides_are_included() -> None:
    defaults = {"workers": 4}
    overrides = {"workers": 2, "region": "local", "ignored": None}
    assert merge_config(overrides, defaults) == {
        "workers": 2,
        "region": "local",
    }
