"""Unit tests for ToolRegistry, schema argument validation, and the read_file tool."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from miniloop.tools import (
    ToolDefinition,
    ToolExecutionError,
    ToolRegistry,
    build_read_file_tool,
    resolve_workspace_path,
    validate_tool_arguments,
)
from miniloop.types import ToolCall


def test_read_demo_readonly_workspace_files_and_call_id_pairing() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    demo_ws = repo_root / "workspaces" / "demo_readonly"

    registry = ToolRegistry([build_read_file_tool(demo_ws)])
    openai_tools = registry.to_openai_tools()
    assert len(openai_tools) == 1
    assert openai_tools[0]["type"] == "function"
    assert openai_tools[0]["function"]["name"] == "read_file"
    assert registry.get("read_file") is not None
    assert len(registry.list_tools()) == 1

    res_readme = registry.execute(
        ToolCall(id="call_readme_01", name="read_file", arguments={"path": "README_DEMO.md"})
    )
    assert res_readme.is_error is False
    assert res_readme.error_code is None
    assert res_readme.call_id == "call_readme_01"
    assert res_readme.tool_name == "read_file"
    assert "AURORA-7" in res_readme.content
    assert "UTC 03:30" in res_readme.content
    assert "config_sample.ini" in res_readme.content
    assert res_readme.duration_ms >= 0.0

    res_config = registry.execute(
        ToolCall(id="call_cfg_02", name="read_file", arguments={"path": "config_sample.ini"})
    )
    assert res_config.is_error is False
    assert res_config.error_code is None
    assert res_config.call_id == "call_cfg_02"
    assert res_config.tool_name == "read_file"
    assert "listen_port = 18420" in res_config.content
    assert "max_batch_items = 64" in res_config.content
    assert "checksum_mode = sha256-strict" in res_config.content


def test_unknown_tool_returns_error_without_side_effects() -> None:
    side_effects = 0

    def _tracked_handler(_args: dict[str, Any], _timeout: float) -> str:
        nonlocal side_effects
        side_effects += 1
        return "ok"

    registry = ToolRegistry(
        [
            ToolDefinition(
                name="known_tool",
                description="A known tool",
                parameters_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                handler=_tracked_handler,
            )
        ]
    )

    result = registry.execute(
        ToolCall(id="call_unknown_1", name="nonexistent_tool", arguments={"foo": "bar"})
    )
    assert result.call_id == "call_unknown_1"
    assert result.tool_name == "nonexistent_tool"
    assert result.is_error is True
    assert result.error_code == "unknown_tool"
    assert "Error [unknown_tool]" in result.content
    assert side_effects == 0


def test_invalid_arguments_intercepted_with_zero_side_effects() -> None:
    side_effect_counter = 0

    def _side_effect_handler(args: dict[str, Any], _timeout: float) -> str:
        nonlocal side_effect_counter
        side_effect_counter += 1
        return f"read {args['path']}"

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_lines": {"type": "integer"},
            "ratio": {"type": "number"},
            "recursive": {"type": "boolean"},
            "tags": {"type": "array"},
            "meta": {"type": "object"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    tool = ToolDefinition(
        name="read_file",
        description="Test tool with typed schema",
        parameters_schema=schema,
        handler=_side_effect_handler,
    )
    registry = ToolRegistry([tool])

    invalid_cases: list[tuple[str, dict[str, Any]]] = [
        ("call_missing_path", {}),
        ("call_none_path", {"path": None}),
        ("call_extra_field", {"path": "a.txt", "extra": 1}),
        ("call_wrong_path_type", {"path": 123}),
        ("call_bool_as_int", {"path": "a.txt", "max_lines": True}),
        ("call_bool_as_int_false", {"path": "a.txt", "max_lines": False}),
        ("call_bool_as_number", {"path": "a.txt", "ratio": True}),
        ("call_str_as_bool", {"path": "a.txt", "recursive": "true"}),
        ("call_dict_as_array", {"path": "a.txt", "tags": {}}),
        ("call_list_as_object", {"path": "a.txt", "meta": []}),
    ]

    for call_id, bad_args in invalid_cases:
        res = registry.execute(ToolCall(id=call_id, name="read_file", arguments=bad_args))
        assert res.call_id == call_id
        assert res.is_error is True
        assert res.error_code == "invalid_arguments"
        assert "Error [invalid_arguments]" in res.content
        assert side_effect_counter == 0

    assert validate_tool_arguments(schema, "not-a-dict") is not None

    # Valid call with all supported JSON Schema types succeeds and increments counter once
    valid_res = registry.execute(
        ToolCall(
            id="call_valid",
            name="read_file",
            arguments={
                "path": "a.txt",
                "max_lines": 10,
                "ratio": 0.5,
                "recursive": False,
                "tags": ["x"],
                "meta": {"k": "v"},
            },
        )
    )
    assert valid_res.is_error is False
    assert valid_res.content == "read a.txt"
    assert side_effect_counter == 1


def test_path_security_blocks_traversal_absolute_and_external_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside_file = workspace / "inside.txt"
    inside_file.write_text("safe content", encoding="utf-8")

    outside_file = tmp_path / "outside_secret.txt"
    outside_file.write_text("outside secret", encoding="utf-8")

    escape_symlink = workspace / "linked_outside.txt"
    escape_symlink.symlink_to(outside_file)

    registry = ToolRegistry([build_read_file_tool(workspace)])

    for idx, forbidden_path in enumerate(
        [
            "../pyproject.toml",
            "../outside_secret.txt",
            "sub/../../outside_secret.txt",
            "/etc/passwd",
            str(outside_file),
            "linked_outside.txt",
        ]
    ):
        call_id = f"sec_{idx}"
        res = registry.execute(
            ToolCall(id=call_id, name="read_file", arguments={"path": forbidden_path})
        )
        assert res.call_id == call_id
        assert res.is_error is True
        assert res.error_code == "path_outside_workspace"
        assert "outside secret" not in res.content

    # Empty or whitespace path returns invalid_path
    empty_res = registry.execute(
        ToolCall(id="sec_empty", name="read_file", arguments={"path": "   "})
    )
    assert empty_res.is_error is True
    assert empty_res.error_code == "invalid_path"

    with pytest.raises(ToolExecutionError) as exc_info:
        resolve_workspace_path(workspace, "../outside_secret.txt")
    assert exc_info.value.code == "path_outside_workspace"


def test_read_file_errors_for_directory_missing_oversized_binary_and_truncation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "subdir").mkdir()

    oversized_file = workspace / "big.txt"
    oversized_file.write_bytes(b"a" * 200)

    binary_file = workspace / "binary.bin"
    binary_file.write_bytes(b"\xff\xfe\x00\x80invalid-utf8")

    long_text_file = workspace / "long_text.txt"
    long_text_file.write_text("0123456789" * 10, encoding="utf-8")

    registry = ToolRegistry(
        [build_read_file_tool(workspace, max_file_bytes=128, max_output_chars=25)]
    )

    # 1. Non-existent file
    res_missing = registry.execute(
        ToolCall(id="c_missing", name="read_file", arguments={"path": "no_such_file.txt"})
    )
    assert res_missing.is_error is True
    assert res_missing.error_code == "file_not_found"

    # 2. Directory instead of regular file
    res_dir = registry.execute(
        ToolCall(id="c_dir", name="read_file", arguments={"path": "subdir"})
    )
    assert res_dir.is_error is True
    assert res_dir.error_code == "not_a_regular_file"

    # 3. File exceeding max_file_bytes
    res_big = registry.execute(
        ToolCall(id="c_big", name="read_file", arguments={"path": "big.txt"})
    )
    assert res_big.is_error is True
    assert res_big.error_code == "file_too_large"

    # 4. Binary / invalid UTF-8 file
    res_bin = registry.execute(
        ToolCall(id="c_bin", name="read_file", arguments={"path": "binary.bin"})
    )
    assert res_bin.is_error is True
    assert res_bin.error_code == "binary_or_invalid_utf8"

    # 5. Output exceeding max_output_chars is truncated deterministically
    res_trunc = registry.execute(
        ToolCall(id="c_trunc", name="read_file", arguments={"path": "long_text.txt"})
    )
    assert res_trunc.is_error is False
    assert res_trunc.error_code is None
    assert res_trunc.content == ("0123456789" * 10)[:25] + "\n...[truncated]"


def test_slow_tool_triggers_timeout_and_duplicate_registration_rejected() -> None:
    def _slow_handler(_args: dict[str, Any], _timeout: float) -> str:
        time.sleep(0.5)
        return "too late"

    def _crashing_handler(_args: dict[str, Any], _timeout: float) -> str:
        raise RuntimeError("unexpected boom")

    slow_tool = ToolDefinition(
        name="slow_tool",
        description="Sleeps past timeout",
        parameters_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        handler=_slow_handler,
    )
    crash_tool = ToolDefinition(
        name="crash_tool",
        description="Raises unexpected RuntimeError",
        parameters_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        handler=_crashing_handler,
    )

    registry = ToolRegistry([slow_tool, crash_tool])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(slow_tool)

    start = time.monotonic()
    timeout_res = registry.execute(
        ToolCall(id="call_slow", name="slow_tool", arguments={}),
        timeout_sec=0.05,
    )
    elapsed = time.monotonic() - start

    assert timeout_res.call_id == "call_slow"
    assert timeout_res.tool_name == "slow_tool"
    assert timeout_res.is_error is True
    assert timeout_res.error_code == "tool_timeout"
    assert "Error [tool_timeout]" in timeout_res.content
    assert elapsed < 0.4

    crash_res = registry.execute(ToolCall(id="call_crash", name="crash_tool", arguments={}))
    assert crash_res.call_id == "call_crash"
    assert crash_res.is_error is True
    assert crash_res.error_code == "tool_runtime_error"
    assert "unexpected boom" in crash_res.content


def test_timed_out_tool_does_not_block_process_exit() -> None:
    script = (
        "import time\n"
        "from miniloop.tools import ToolDefinition, ToolRegistry\n"
        "from miniloop.types import ToolCall\n"
        "schema = {\n"
        "    'type': 'object',\n"
        "    'properties': {},\n"
        "    'required': [],\n"
        "    'additionalProperties': False,\n"
        "}\n"
        "tool = ToolDefinition(\n"
        "    name='hang_tool',\n"
        "    description='Sleeps 30s',\n"
        "    parameters_schema=schema,\n"
        "    handler=lambda _args, _timeout: (time.sleep(30), 'done')[1],\n"
        ")\n"
        "call = ToolCall(id='c1', name='hang_tool', arguments={})\n"
        "res = ToolRegistry([tool]).execute(call, timeout_sec=0.05)\n"
        "print(res.error_code)\n"
    )
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=True,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 1.0
    assert proc.stdout.strip() == "tool_timeout"

