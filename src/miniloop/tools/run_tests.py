"""Fixed-entrypoint `run_tests` tool with pre/post test-file SHA-256 integrity checks."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from miniloop.tools.patch import file_sha256
from miniloop.tools.read_file import resolve_workspace_path
from miniloop.tools.registry import ToolDefinition, ToolExecutionError

RUN_TESTS_PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

_SAFE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "VIRTUAL_ENV",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "USER",
    }
)

_SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "AUTHORIZATION",
)


def compute_file_hashes(workspace_root: Path, rel_paths: Sequence[str]) -> dict[str, str]:
    """Compute SHA-256 digests for a sequence of workspace-relative paths."""
    root = workspace_root.resolve()
    hashes: dict[str, str] = {}
    for rel_path in rel_paths:
        target = resolve_workspace_path(root, rel_path)
        if not target.exists() or target.is_dir() or not target.is_file():
            raise ToolExecutionError(
                "file_not_found",
                f"Protected file not found: '{rel_path}'",
            )
        hashes[rel_path] = file_sha256(target)
    return hashes


def verify_file_hashes(workspace_root: Path, expected_hashes: Mapping[str, str]) -> str | None:
    """Verify every protected file exists inside workspace_root and matches its expected SHA-256.

    Returns None when all files match, or a violation description string on mismatch.
    """
    root = workspace_root.resolve()
    for rel_path, expected_sha in expected_hashes.items():
        try:
            target = resolve_workspace_path(root, rel_path)
        except ToolExecutionError as exc:
            return f"Protected test file '{rel_path}' failed path check: {exc.message}"

        if not target.exists() or target.is_dir() or not target.is_file():
            return f"Protected test file '{rel_path}' was deleted or is no longer a regular file"

        try:
            actual_sha = file_sha256(target)
        except ToolExecutionError as exc:
            return f"Protected test file '{rel_path}' could not be read: {exc.message}"

        if actual_sha != expected_sha:
            return (
                f"Protected test file '{rel_path}' was modified "
                f"(expected sha256={expected_sha}, actual sha256={actual_sha})"
            )
    return None


def _build_clean_test_env(
    workspace_root: Path,
    source_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct a sanitized environment for test execution without leaking API keys or tokens."""
    raw_env = os.environ if source_env is None else source_env
    clean_env: dict[str, str] = {}

    for key, value in raw_env.items():
        upper_key = key.upper()
        if any(marker in upper_key for marker in _SENSITIVE_KEY_MARKERS):
            continue
        if key in _SAFE_ENV_KEYS:
            clean_env[key] = value

    python_bin_dir = str(Path(sys.executable).parent)
    existing_path = clean_env.get("PATH", "")
    path_parts = [p for p in existing_path.split(os.pathsep) if p and p != python_bin_dir]
    path_parts.insert(0, python_bin_dir)
    clean_env["PATH"] = os.pathsep.join(path_parts)

    root = workspace_root.resolve()
    pythonpath_entries = [str(root / "src"), str(root)]
    clean_env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
    return clean_env


def _kill_process_group(pid: int) -> None:
    """Kill all processes in the process group led by `pid`, ignoring already-exited groups."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def build_run_tests_tool(
    workspace_root: Path,
    *,
    test_command: Sequence[str] = ("pytest", "-q"),
    protected_hashes: Mapping[str, str] | None = None,
    max_output_chars: int = 12000,
) -> ToolDefinition:
    """Build a zero-argument `run_tests` ToolDefinition with pre/post test hash guards."""
    root = workspace_root.resolve()
    cmd_tuple = tuple(test_command)
    if not cmd_tuple:
        raise ValueError("test_command must not be empty")
    frozen_hashes = dict(protected_hashes) if protected_hashes is not None else None

    def _handle_run_tests(_arguments: dict[str, Any], timeout_sec: float) -> str:
        if frozen_hashes is not None:
            pre_violation = verify_file_hashes(root, frozen_hashes)
            if pre_violation is not None:
                raise ToolExecutionError("protected_test_modified", pre_violation)

        clean_env = _build_clean_test_env(root)

        try:
            proc = subprocess.Popen(
                list(cmd_tuple),
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=clean_env,
                start_new_session=True,
            )
        except OSError as exc:
            raise ToolExecutionError(
                "test_execution_failed",
                f"Failed to execute test command: {exc}",
            ) from exc

        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Test command timed out after {timeout_sec:.2f}s") from exc
        finally:
            _kill_process_group(proc.pid)
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
            if frozen_hashes is not None:
                post_violation = verify_file_hashes(root, frozen_hashes)
                if post_violation is not None:
                    raise ToolExecutionError("protected_test_modified", post_violation)

        passed = proc.returncode == 0
        sections: list[str] = []
        if stdout:
            sections.append(stdout.rstrip())
        if stderr:
            sections.append(f"[stderr]\n{stderr.rstrip()}")
        combined = "\n".join(sections).strip()
        if len(combined) > max_output_chars:
            combined = combined[:max_output_chars] + "\n...[truncated]"

        return (
            f"exit_code: {proc.returncode}\n"
            f"passed: {str(passed).lower()}\n"
            f"output:\n{combined}"
        )

    return ToolDefinition(
        name="run_tests",
        description=(
            "Run the workspace's fixed test suite with zero arguments and report exit_code, "
            "passed status, and stdout/stderr output."
        ),
        parameters_schema=dict(RUN_TESTS_PARAMETERS_SCHEMA),
        handler=_handle_run_tests,
    )
