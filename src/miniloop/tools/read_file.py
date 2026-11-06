"""Workspace-confined read-only `read_file` tool implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from miniloop.tools.registry import ToolDefinition, ToolExecutionError

READ_FILE_PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Relative path to the file inside the workspace.",
        }
    },
    "required": ["path"],
    "additionalProperties": False,
}


def resolve_workspace_path(workspace_root: Path, raw_path: str) -> Path:
    """Resolve a relative path within workspace_root and prevent directory or symlink escape."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolExecutionError("invalid_path", "Path must not be empty")
    if "\x00" in raw_path:
        raise ToolExecutionError("invalid_path", "Path must not contain null bytes")
    if raw_path.startswith("/") or Path(raw_path).is_absolute():
        raise ToolExecutionError("path_outside_workspace", "Absolute paths are not allowed")

    root_resolved = workspace_root.resolve()
    target_resolved = (root_resolved / raw_path).resolve()

    if not target_resolved.is_relative_to(root_resolved):
        raise ToolExecutionError(
            "path_outside_workspace",
            f"Path '{raw_path}' escapes workspace root",
        )

    return target_resolved


def build_read_file_tool(
    workspace_root: Path,
    *,
    max_file_bytes: int = 65536,
    max_output_chars: int = 16000,
) -> ToolDefinition:
    """Build a workspace-scoped read-only `read_file` ToolDefinition."""
    root = workspace_root.resolve()

    def _handle_read_file(arguments: dict[str, Any], _timeout_sec: float) -> str:
        raw_path = arguments["path"]
        target_path = resolve_workspace_path(root, raw_path)

        if not target_path.exists():
            raise ToolExecutionError("file_not_found", f"File not found: '{raw_path}'")

        if target_path.is_dir() or not target_path.is_file():
            raise ToolExecutionError(
                "not_a_regular_file",
                f"Path is not a regular file: '{raw_path}'",
            )

        try:
            stat_result = target_path.stat()
        except OSError as exc:
            raise ToolExecutionError(
                "file_read_error",
                f"Failed to stat '{raw_path}': {exc}",
            ) from exc

        if stat_result.st_size > max_file_bytes:
            raise ToolExecutionError(
                "file_too_large",
                f"File '{raw_path}' size ({stat_result.st_size} bytes) exceeds limit "
                f"({max_file_bytes} bytes)",
            )

        try:
            raw_bytes = target_path.read_bytes()
        except OSError as exc:
            raise ToolExecutionError(
                "file_read_error",
                f"Failed to read '{raw_path}': {exc}",
            ) from exc

        if len(raw_bytes) > max_file_bytes:
            raise ToolExecutionError(
                "file_too_large",
                f"File '{raw_path}' size ({len(raw_bytes)} bytes) exceeds limit "
                f"({max_file_bytes} bytes)",
            )

        if b"\x00" in raw_bytes:
            raise ToolExecutionError(
                "binary_or_invalid_utf8",
                f"File '{raw_path}' contains binary null bytes",
            )

        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(
                "binary_or_invalid_utf8",
                f"File '{raw_path}' is not valid UTF-8 text",
            ) from exc

        if len(content) > max_output_chars:
            return content[:max_output_chars] + "\n...[truncated]"

        return content

    return ToolDefinition(
        name="read_file",
        description="Read a UTF-8 text file from a relative path inside the workspace.",
        parameters_schema=dict(READ_FILE_PARAMETERS_SCHEMA),
        handler=_handle_read_file,
    )
