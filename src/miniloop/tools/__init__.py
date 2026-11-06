"""MiniLoop tool definitions, schema validation, registry, and built-in tools."""

from __future__ import annotations

from miniloop.tools.patch import (
    APPLY_PATCH_PARAMETERS_SCHEMA,
    PROPOSE_PATCH_PARAMETERS_SCHEMA,
    ApprovalCallback,
    PatchProposal,
    PatchStatus,
    PatchStore,
    approve_all_allowed,
    build_patch_tools,
    file_sha256,
    reject_all,
)
from miniloop.tools.read_file import build_read_file_tool, resolve_workspace_path
from miniloop.tools.registry import (
    ToolDefinition,
    ToolExecutionError,
    ToolRegistry,
    validate_tool_arguments,
)
from miniloop.tools.run_tests import (
    RUN_TESTS_PARAMETERS_SCHEMA,
    build_run_tests_tool,
    compute_file_hashes,
    verify_file_hashes,
)

__all__ = [
    "APPLY_PATCH_PARAMETERS_SCHEMA",
    "ApprovalCallback",
    "PROPOSE_PATCH_PARAMETERS_SCHEMA",
    "PatchProposal",
    "PatchStatus",
    "PatchStore",
    "RUN_TESTS_PARAMETERS_SCHEMA",
    "ToolDefinition",
    "ToolExecutionError",
    "ToolRegistry",
    "approve_all_allowed",
    "build_patch_tools",
    "build_read_file_tool",
    "build_run_tests_tool",
    "compute_file_hashes",
    "file_sha256",
    "reject_all",
    "resolve_workspace_path",
    "validate_tool_arguments",
    "verify_file_hashes",
]
