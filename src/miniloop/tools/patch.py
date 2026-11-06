"""Two-phase optimistic-lock patch tools (`propose_patch` / `apply_patch`) and approval policies."""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from miniloop.tools.read_file import resolve_workspace_path
from miniloop.tools.registry import ToolDefinition, ToolExecutionError

PatchStatus = Literal["approved", "rejected", "applied", "invalidated"]


@dataclass
class PatchProposal:
    """A staged file modification bound to the target file's base SHA-256 digest."""

    patch_id: str
    rel_path: str
    old_str: str
    new_str: str
    base_sha256: str
    new_content: str
    unified_diff: str
    status: PatchStatus = "approved"


ApprovalCallback = Callable[[PatchProposal], bool]


def approve_all_allowed() -> ApprovalCallback:
    """Return an approval policy that automatically approves every valid whitelisted proposal."""

    def _approve(_proposal: PatchProposal) -> bool:
        return True

    return _approve


def reject_all() -> ApprovalCallback:
    """Return an approval policy that rejects every patch proposal."""

    def _reject(_proposal: PatchProposal) -> bool:
        return False

    return _reject


def file_sha256(path: Path) -> str:
    """Compute the hex SHA-256 digest of a file on disk."""
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ToolExecutionError(
            "file_read_error",
            f"Failed to read file '{path.name}': {exc}",
        ) from exc
    return hashlib.sha256(raw_bytes).hexdigest()


def _is_protected_test_path(rel_path: str) -> bool:
    """Return True if a workspace-relative POSIX path points to a test file or directory."""
    parts = PurePosixPath(rel_path).parts
    if any(part == "tests" for part in parts):
        return True
    filename = PurePosixPath(rel_path).name
    return filename.startswith("test_") or filename.endswith("_test.py")


def _normalize_editable_path(raw: str) -> str:
    """Normalize a workspace-relative editable file entry without stripping dotfile prefixes."""
    s = raw.strip()
    while s.startswith("./"):
        s = s[2:]
    norm = PurePosixPath(s).as_posix()
    if not norm or norm == "." or norm.startswith("/") or ".." in PurePosixPath(norm).parts:
        raise ValueError(f"Invalid workspace-relative editable file path: {raw!r}")
    return norm


def _atomic_write_utf8(target: Path, content: str) -> None:
    """Atomically write UTF-8 text to `target` while preserving its permission bits."""
    try:
        encoded_bytes = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolExecutionError(
            "invalid_patch_content",
            "Replacement content is not valid UTF-8",
        ) from exc

    tmp_path: Path | None = None
    replaced = False
    try:
        orig_mode = stat.S_IMODE(target.stat().st_mode)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=".miniloop_patch_",
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(encoded_bytes)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.chmod(tmp_path, orig_mode)
        os.replace(tmp_path, target)
        replaced = True
    except OSError as exc:
        raise ToolExecutionError(
            "file_write_error",
            f"Failed to write '{target.name}': {exc}",
        ) from exc
    finally:
        if not replaced and tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


class PatchStore:
    """In-memory optimistic-lock store managing patch proposals and atomic application."""

    def __init__(
        self,
        workspace_root: Path,
        editable_files: Iterable[str] | None = None,
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        self.workspace_root: Path = workspace_root.resolve()
        self.editable_files: frozenset[str] | None = (
            frozenset(_normalize_editable_path(p) for p in editable_files)
            if editable_files is not None
            else None
        )
        self.approval_callback: ApprovalCallback = (
            approval_callback if approval_callback is not None else approve_all_allowed()
        )
        self._proposals: dict[str, PatchProposal] = {}
        self._seq: int = 0

    @property
    def proposals(self) -> dict[str, PatchProposal]:
        """Return a shallow copy of all recorded proposals keyed by patch_id."""
        return dict(self._proposals)

    def get(self, patch_id: str) -> PatchProposal | None:
        """Look up a previously created PatchProposal by patch_id."""
        return self._proposals.get(patch_id)

    def propose(self, path: str, old_str: str, new_str: str) -> PatchProposal:
        """Validate and stage a patch without modifying the target file on disk."""
        target = resolve_workspace_path(self.workspace_root, path)
        norm_rel = target.relative_to(self.workspace_root).as_posix()
        raw_norm = PurePosixPath(path).as_posix()

        if (
            _is_protected_test_path(norm_rel)
            or _is_protected_test_path(raw_norm)
            or (self.editable_files is not None and norm_rel not in self.editable_files)
        ):
            raise ToolExecutionError(
                "patch_path_forbidden",
                f"File '{norm_rel}' is protected and cannot be modified",
            )

        if not target.exists() or target.is_dir() or not target.is_file():
            raise ToolExecutionError(
                "file_not_found",
                f"File not found: '{norm_rel}'",
            )

        try:
            raw_bytes = target.read_bytes()
        except OSError as exc:
            raise ToolExecutionError(
                "file_read_error",
                f"Failed to read '{norm_rel}': {exc}",
            ) from exc

        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(
                "binary_or_invalid_utf8",
                f"File '{norm_rel}' is not valid UTF-8 text",
            ) from exc

        base_sha = hashlib.sha256(raw_bytes).hexdigest()

        if old_str == "" or old_str == new_str:
            raise ToolExecutionError(
                "invalid_patch_content",
                "old_str must be non-empty and different from new_str",
            )

        count = content.count(old_str)
        if count == 0:
            raise ToolExecutionError(
                "old_str_not_found",
                f"old_str was not found in '{norm_rel}'",
            )
        if count > 1:
            raise ToolExecutionError(
                "old_str_not_unique",
                f"old_str matched {count} locations in '{norm_rel}'; must match exactly once",
            )

        new_content = content.replace(old_str, new_str, 1)
        try:
            new_content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ToolExecutionError(
                "invalid_patch_content",
                "Replacement content is not valid UTF-8",
            ) from exc

        diff_lines = difflib.unified_diff(
            content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{norm_rel}",
            tofile=f"b/{norm_rel}",
        )
        unified_diff = "".join(diff_lines)

        self._seq += 1
        patch_id = f"patch_{self._seq:04d}"

        proposal = PatchProposal(
            patch_id=patch_id,
            rel_path=norm_rel,
            old_str=old_str,
            new_str=new_str,
            base_sha256=base_sha,
            new_content=new_content,
            unified_diff=unified_diff,
            status="approved",
        )
        approved = bool(self.approval_callback(proposal))
        proposal.status = "approved" if approved else "rejected"
        self._proposals[patch_id] = proposal
        return proposal

    def apply(self, patch_id: str) -> PatchProposal:
        """Apply an approved PatchProposal if the target file's SHA-256 is unchanged."""
        proposal = self._proposals.get(patch_id)
        if proposal is None:
            raise ToolExecutionError(
                "unknown_patch_id",
                f"Unknown patch_id: '{patch_id}'",
            )

        if proposal.status == "rejected":
            raise ToolExecutionError(
                "patch_rejected",
                f"Patch '{patch_id}' was rejected by approval policy",
            )

        if proposal.status == "applied":
            raise ToolExecutionError(
                "patch_already_applied",
                f"Patch '{patch_id}' has already been applied",
            )

        if proposal.status == "invalidated":
            raise ToolExecutionError(
                "patch_stale_file_changed",
                f"Target file '{proposal.rel_path}' changed since patch '{patch_id}' was proposed",
            )

        try:
            target = resolve_workspace_path(self.workspace_root, proposal.rel_path)
        except ToolExecutionError:
            proposal.status = "invalidated"
            raise

        root_resolved = self.workspace_root.resolve()
        raw_entry = root_resolved / proposal.rel_path
        current_norm_rel = target.relative_to(root_resolved).as_posix()

        if (
            _is_protected_test_path(current_norm_rel)
            or _is_protected_test_path(proposal.rel_path)
            or (
                self.editable_files is not None
                and current_norm_rel not in self.editable_files
            )
        ):
            proposal.status = "invalidated"
            raise ToolExecutionError(
                "patch_path_forbidden",
                f"File '{current_norm_rel}' is protected and cannot be modified",
            )

        if current_norm_rel != proposal.rel_path or raw_entry.is_symlink():
            proposal.status = "invalidated"
            raise ToolExecutionError(
                "patch_stale_file_changed",
                f"Target file '{proposal.rel_path}' changed since patch '{patch_id}' was proposed",
            )

        if not target.exists() or target.is_dir() or not target.is_file():
            proposal.status = "invalidated"
            raise ToolExecutionError(
                "patch_stale_file_changed",
                f"Target file '{proposal.rel_path}' changed since patch '{patch_id}' was proposed",
            )

        current_sha256 = file_sha256(target)
        if current_sha256 != proposal.base_sha256:
            proposal.status = "invalidated"
            raise ToolExecutionError(
                "patch_stale_file_changed",
                f"Target file '{proposal.rel_path}' changed since patch '{patch_id}' was proposed",
            )

        _atomic_write_utf8(target, proposal.new_content)

        for other_id, other in self._proposals.items():
            if (
                other_id != patch_id
                and other.rel_path == proposal.rel_path
                and other.status == "approved"
            ):
                other.status = "invalidated"

        proposal.status = "applied"
        return proposal


PROPOSE_PATCH_PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Relative path to the editable source file inside the workspace.",
        },
        "old_str": {
            "type": "string",
            "description": "Exact existing text snippet to replace (must occur exactly once).",
        },
        "new_str": {
            "type": "string",
            "description": "Replacement text snippet.",
        },
    },
    "required": ["path", "old_str", "new_str"],
    "additionalProperties": False,
}

APPLY_PATCH_PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "patch_id": {
            "type": "string",
            "description": "Identifier of the approved patch returned by propose_patch.",
        },
    },
    "required": ["patch_id"],
    "additionalProperties": False,
}


def build_patch_tools(store: PatchStore) -> tuple[ToolDefinition, ToolDefinition]:
    """Build paired `(propose_patch, apply_patch)` tool definitions backed by `store`."""

    def _handle_propose_patch(arguments: dict[str, Any], _timeout_sec: float) -> str:
        proposal = store.propose(
            path=arguments["path"],
            old_str=arguments["old_str"],
            new_str=arguments["new_str"],
        )
        return (
            f"patch_id: {proposal.patch_id}\n"
            f"path: {proposal.rel_path}\n"
            f"status: {proposal.status}\n"
            f"base_sha256: {proposal.base_sha256}\n"
            f"diff:\n{proposal.unified_diff}"
        )

    def _handle_apply_patch(arguments: dict[str, Any], _timeout_sec: float) -> str:
        proposal = store.apply(patch_id=arguments["patch_id"])
        target = resolve_workspace_path(store.workspace_root, proposal.rel_path)
        new_sha256 = file_sha256(target)
        return (
            f"patch_id: {proposal.patch_id}\n"
            f"path: {proposal.rel_path}\n"
            f"status: {proposal.status}\n"
            f"sha256: {new_sha256}"
        )

    propose_tool = ToolDefinition(
        name="propose_patch",
        description=(
            "Propose an exact string replacement on an allowed source file without writing to "
            "disk. Returns a patch_id, approval status, and unified diff."
        ),
        parameters_schema=dict(PROPOSE_PATCH_PARAMETERS_SCHEMA),
        handler=_handle_propose_patch,
    )

    apply_tool = ToolDefinition(
        name="apply_patch",
        description=(
            "Apply a previously approved patch by patch_id if the target file has not changed "
            "since the patch was proposed."
        ),
        parameters_schema=dict(APPLY_PATCH_PARAMETERS_SCHEMA),
        handler=_handle_apply_patch,
    )

    return propose_tool, apply_tool
