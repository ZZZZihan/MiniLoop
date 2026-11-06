"""Configuration dictionary merger with explicit None fallback semantics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def merge_config(
    overrides: Mapping[str, Any],
    defaults: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge overrides onto defaults.

    Keys missing from overrides or mapped to None in overrides fall back to defaults.
    Explicit falsy values such as 0, False, or "" must be preserved.
    Extra keys in overrides with non-None values are also included.
    """
    merged: dict[str, Any] = {}
    for k, v in defaults.items():
        merged[k] = overrides.get(k) or v
    for k, val in overrides.items():
        if k not in merged and val is not None:
            merged[k] = val
    return merged


merge_defaults = merge_config
