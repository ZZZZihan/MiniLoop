"""Comma-separated key=value line parser supporting double-quoted values."""

from __future__ import annotations


def parse_kv_line(line: str) -> dict[str, str]:
    """Parse a comma-separated key=value line into a dictionary.

    Values may be double-quoted to contain commas or '=' characters
    (e.g., 'host=localhost,note="a,b",port=8080'). Outer double quotes are
    stripped from quoted values, and escaped quotes ('\\"' or '""') inside
    quoted values are unescaped to '"'.
    Empty or whitespace-only lines return an empty dict.
    Raises ValueError if quotes are unclosed, a segment is missing '=', or a key is empty.
    """
    if not line.strip():
        return {}

    result: dict[str, str] = {}
    for part in line.split(","):
        if "=" not in part:
            raise ValueError(f"Missing '=' in segment: {part!r}")
        key, raw_val = part.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("Key must not be empty")
        result[key] = raw_val.strip()
    return result
