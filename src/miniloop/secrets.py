"""Secret encapsulation and safe credential loading without plaintext leakage."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

REDACTED_REPR = "SecretValue('***')"


class SecretLoadError(RuntimeError, ValueError):
    """Raised when an explicitly configured secret file cannot be loaded safely."""


class SecretValue:
    """Opaque wrapper around a sensitive string value that masks str/repr output."""

    __slots__ = ("_secret_value",)

    def __init__(self, secret_value: str) -> None:
        self._secret_value = secret_value

    def get_secret_value(self) -> str:
        """Return the underlying plaintext secret for outbound authentication."""
        return self._secret_value

    def __repr__(self) -> str:
        return REDACTED_REPR

    def __str__(self) -> str:
        return REDACTED_REPR

    def __format__(self, format_spec: str) -> str:
        return REDACTED_REPR

    def __bool__(self) -> bool:
        return bool(self._secret_value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SecretValue):
            return self._secret_value == other._secret_value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._secret_value)


def read_secret(
    *,
    env_value: str | None = None,
    file_path: str | Path | None = None,
    default_files: Sequence[Path] = (),
    name: str = "API_KEY",
) -> SecretValue | None:
    """Resolve a secret from an environment value, explicit file, or fallback files.

    Precedence:
    1. Non-empty ``env_value`` (stripped).
    2. Explicit ``file_path`` (raises ``SecretLoadError`` if missing or empty).
    3. First existing non-empty file in ``default_files``.
    """
    if env_value is not None:
        stripped_env = env_value.strip()
        if stripped_env:
            return SecretValue(stripped_env)

    explicit_path: Path | None = None
    if isinstance(file_path, Path):
        explicit_path = file_path.expanduser()
    elif isinstance(file_path, str):
        stripped_path = file_path.strip()
        if stripped_path:
            explicit_path = Path(stripped_path).expanduser()

    if explicit_path is not None:
        if not explicit_path.exists():
            raise SecretLoadError(f"{name} file does not exist: {file_path}")
        if not explicit_path.is_file():
            raise SecretLoadError(f"{name} path is not a regular file: {file_path}")
        try:
            raw_content = explicit_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise SecretLoadError(f"{name} file is not valid UTF-8: {file_path}") from None
        except OSError:
            raise SecretLoadError(f"Failed to read {name} file: {file_path}") from None

        stripped_file = raw_content.strip()
        if not stripped_file:
            raise SecretLoadError(f"{name} file is empty: {file_path}")
        return SecretValue(stripped_file)

    for candidate in default_files:
        expanded = candidate.expanduser()
        if not expanded.is_file():
            continue
        try:
            raw_default = expanded.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        stripped_default = raw_default.strip()
        if stripped_default:
            return SecretValue(stripped_default)

    return None
