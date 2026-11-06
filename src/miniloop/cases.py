"""Frozen Python repair cases loader, workspace isolator, and MANIFEST.json verifier."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

CaseSplit = Literal["dev", "test"]
SplitFilter = Literal["dev", "test", "all"]

MANIFEST_FILENAME = "MANIFEST.json"
MANIFEST_SCHEMA_VERSION = "1.0"
_IGNORED_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache"})


@dataclass(frozen=True)
class ReferenceFix:
    """Human-authored reference patch used only for offline self-verification."""

    path: str
    old_str: str
    new_str: str

    @property
    def rel_path(self) -> str:
        """Alias for path."""
        return self.path


@dataclass(frozen=True)
class RepairCase:
    """Metadata and file layout for a single frozen Python repair benchmark case."""

    case_id: str
    split: CaseSplit
    title: str
    task_prompt: str
    case_dir: Path
    editable_files: tuple[str, ...]
    protected_test_files: tuple[str, ...]
    test_command: tuple[str, ...]
    reference_fix: ReferenceFix


def _sha256_file(path: Path) -> str:
    """Compute the lowercase hex SHA-256 digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_rel_path(raw_path: str) -> str:
    """Validate and normalize a case-relative POSIX path."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("Relative path must be a non-empty string")
    rel = Path(raw_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Invalid case-relative path: {raw_path!r}")
    return rel.as_posix()


def _parse_str_tuple(raw: Any, field_name: str) -> tuple[str, ...]:
    """Validate a non-empty JSON list of non-empty strings."""
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"Field '{field_name}' must be a non-empty list of strings")
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"All entries in '{field_name}' must be non-empty strings")
        items.append(item)
    return tuple(items)


def _load_single_case(case_dir: Path) -> RepairCase:
    """Load and validate a single RepairCase from its directory."""
    case_json_path = case_dir / "case.json"
    if not case_json_path.is_file():
        raise ValueError(f"Missing case.json in {case_dir}")

    raw_data = json.loads(case_json_path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, dict):
        raise ValueError(f"case.json in {case_dir} must be a JSON object")

    case_id = raw_data.get("case_id")
    if not isinstance(case_id, str) or case_id != case_dir.name:
        raise ValueError(
            f"case_id {case_id!r} does not match directory name {case_dir.name!r}"
        )

    split_raw = raw_data.get("split")
    if split_raw not in ("dev", "test"):
        raise ValueError(f"Invalid split {split_raw!r} in {case_json_path}")
    split: CaseSplit = "dev" if split_raw == "dev" else "test"

    title = raw_data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"Missing non-empty 'title' in {case_json_path}")

    task_prompt = raw_data.get("task_prompt")
    if not isinstance(task_prompt, str) or not task_prompt.strip():
        raise ValueError(f"Missing non-empty 'task_prompt' in {case_json_path}")

    editable_files = tuple(
        _normalize_rel_path(p)
        for p in _parse_str_tuple(raw_data.get("editable_files"), "editable_files")
    )
    protected_test_files = tuple(
        _normalize_rel_path(p)
        for p in _parse_str_tuple(
            raw_data.get("protected_test_files"), "protected_test_files"
        )
    )
    test_command = _parse_str_tuple(raw_data.get("test_command"), "test_command")

    ref_raw = raw_data.get("reference_fix")
    if not isinstance(ref_raw, dict):
        raise ValueError(f"Missing 'reference_fix' object in {case_json_path}")
    ref_path = _normalize_rel_path(ref_raw.get("path", ""))
    old_str = ref_raw.get("old_str")
    new_str = ref_raw.get("new_str")
    if (
        not isinstance(old_str, str)
        or not old_str
        or not isinstance(new_str, str)
        or not new_str
        or old_str == new_str
    ):
        raise ValueError(f"Invalid 'reference_fix' strings in {case_json_path}")
    if ref_path not in editable_files:
        raise ValueError(
            f"reference_fix.path {ref_path!r} must be listed in editable_files"
        )

    return RepairCase(
        case_id=case_id,
        split=split,
        title=title,
        task_prompt=task_prompt,
        case_dir=case_dir.resolve(),
        editable_files=editable_files,
        protected_test_files=protected_test_files,
        test_command=test_command,
        reference_fix=ReferenceFix(path=ref_path, old_str=old_str, new_str=new_str),
    )


def load_cases(
    cases_root: Path = Path("cases"),
    *,
    split: SplitFilter = "all",
) -> list[RepairCase]:
    """Load frozen repair cases from cases_root, optionally filtered by split."""
    if split not in ("dev", "test", "all"):
        raise ValueError(f"Invalid split filter: {split!r}")
    root = cases_root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Cases directory not found: {cases_root}")

    cases: list[RepairCase] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name.startswith(".") or entry.name in _IGNORED_DIR_NAMES:
            continue
        case = _load_single_case(entry)
        if split == "all" or case.split == split:
            cases.append(case)
    return cases


def _scan_case_files(cases_root: Path) -> dict[str, str]:
    """Scan all non-cache regular files under cases_root (excluding MANIFEST.json)."""
    root = cases_root.resolve()
    file_hashes: dict[str, str] = {}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in _IGNORED_DIR_NAMES and not d.startswith(".")
        )
        current_dir = Path(dirpath)
        for filename in sorted(filenames):
            if filename.endswith(".pyc") or filename.startswith("."):
                continue
            file_path = current_dir / filename
            rel_posix = file_path.relative_to(root).as_posix()
            if rel_posix == MANIFEST_FILENAME:
                continue
            if file_path.is_file():
                file_hashes[rel_posix] = _sha256_file(file_path)

    return dict(sorted(file_hashes.items()))


def compute_cases_manifest(cases_root: Path = Path("cases")) -> dict[str, Any]:
    """Compute the canonical MANIFEST.json dictionary for all cases under cases_root."""
    cases = load_cases(cases_root, split="all")
    root = cases_root.resolve()

    for case in cases:
        required_rels = ["case.json", *case.editable_files, *case.protected_test_files]
        for rel in required_rels:
            full_path = root / case.case_id / rel
            if not full_path.is_file():
                raise FileNotFoundError(
                    f"Case '{case.case_id}' missing declared file: {rel}"
                )

    files_map = _scan_case_files(cases_root)
    dev_ids = [c.case_id for c in cases if c.split == "dev"]
    test_ids = [c.case_id for c in cases if c.split == "test"]

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "case_count": len(cases),
        "splits": {
            "dev": dev_ids,
            "test": test_ids,
        },
        "files": files_map,
    }


def verify_cases_manifest(cases_root: Path = Path("cases")) -> list[str]:
    """Verify cases/MANIFEST.json against on-disk files, returning any mismatches."""
    root = cases_root.resolve()
    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return [f"missing_manifest: {MANIFEST_FILENAME}"]

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid_manifest_json: {exc}"]

    if not isinstance(manifest_data, dict):
        return ["invalid_manifest_root: expected JSON object"]

    recorded_files = manifest_data.get("files")
    if not isinstance(recorded_files, dict):
        return ["invalid_manifest_files: missing 'files' mapping"]

    try:
        computed = compute_cases_manifest(cases_root)
    except Exception as exc:
        return [f"case_load_error: {exc}"]

    actual_files: dict[str, str] = computed["files"]
    mismatches: list[str] = []

    if manifest_data.get("case_count") != computed["case_count"]:
        mismatches.append(
            f"case_count_mismatch: expected {manifest_data.get('case_count')}, "
            f"got {computed['case_count']}"
        )

    if manifest_data.get("splits") != computed["splits"]:
        mismatches.append("splits_mismatch: manifest splits do not match cases")

    for rel_path in sorted(set(recorded_files) | set(actual_files)):
        if rel_path not in actual_files:
            mismatches.append(f"missing_file: {rel_path}")
        elif rel_path not in recorded_files:
            mismatches.append(f"unregistered_file: {rel_path}")
        else:
            expected_sha = recorded_files[rel_path]
            actual_sha = actual_files[rel_path]
            if expected_sha != actual_sha:
                mismatches.append(
                    f"sha256_mismatch: {rel_path} "
                    f"(expected {expected_sha}, got {actual_sha})"
                )

    return mismatches


def prepare_isolated_workspace(case: RepairCase, dest_dir: Path) -> dict[str, str]:
    """Copy only editable_files and protected_test_files into dest_dir (excluding case.json).

    Returns a mapping of {rel_path: sha256} for all protected_test_files.
    """
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()

    all_rel_paths = [*case.editable_files, *case.protected_test_files]
    for rel_path in all_rel_paths:
        norm_rel = _normalize_rel_path(rel_path)
        if Path(norm_rel).name == "case.json":
            raise ValueError("Refusing to copy case.json into isolated workspace")
        src_file = (case.case_dir / norm_rel).resolve()
        if not src_file.is_relative_to(case.case_dir.resolve()) or not src_file.is_file():
            raise FileNotFoundError(
                f"Source file '{norm_rel}' not found in case '{case.case_id}'"
            )
        dst_file = (dest_resolved / norm_rel).resolve()
        if not dst_file.is_relative_to(dest_resolved):
            raise ValueError(f"Destination path escapes workspace: {norm_rel!r}")
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst_file)

    protected_hashes: dict[str, str] = {}
    for test_rel in case.protected_test_files:
        norm_rel = _normalize_rel_path(test_rel)
        protected_hashes[norm_rel] = _sha256_file(dest_resolved / norm_rel)

    return protected_hashes


def _build_clean_test_env(workspace_dir: Path) -> dict[str, str]:
    """Build a clean environment for running a case's test command."""
    venv_bin = str(Path(sys.executable).parent)
    existing_path = os.environ.get("PATH", "")
    path_parts = [venv_bin, *([existing_path] if existing_path else [])]

    pythonpath_parts = [str(workspace_dir.resolve()), str((workspace_dir / "src").resolve())]
    clean_env: dict[str, str] = {
        "PATH": os.pathsep.join(path_parts),
        "PYTHONPATH": os.pathsep.join(pythonpath_parts),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for preserved_key in ("HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "VIRTUAL_ENV"):
        if preserved_key in os.environ:
            clean_env[preserved_key] = os.environ[preserved_key]
    return clean_env


def _run_case_tests(
    workspace_dir: Path,
    test_command: Sequence[str],
    *,
    timeout_sec: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    """Run a case's test command inside workspace_dir with a clean environment."""
    clean_env = _build_clean_test_env(workspace_dir)

    return subprocess.run(
        list(test_command),
        cwd=workspace_dir,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        env=clean_env,
        check=False,
    )


def verify_case_reference_fix(
    case: RepairCase,
    tmp_root: Path | None = None,
) -> bool:
    """Verify that a case fails tests initially and passes after applying reference_fix."""
    if tmp_root is None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            return verify_case_reference_fix(case, Path(tmp_dir))

    dest_dir = tmp_root / case.case_id
    expected_test_hashes = prepare_isolated_workspace(case, dest_dir)

    if (dest_dir / "case.json").exists():
        return False

    # Step 1: Initial buggy state must fail pytest (returncode != 0)
    initial_run = _run_case_tests(dest_dir, case.test_command)
    if initial_run.returncode == 0:
        return False

    # Step 2: Apply human reference fix (old_str -> new_str)
    target_path = dest_dir / case.reference_fix.path
    if not target_path.is_file():
        return False
    original_content = target_path.read_text(encoding="utf-8")
    if original_content.count(case.reference_fix.old_str) != 1:
        return False
    patched_content = original_content.replace(
        case.reference_fix.old_str,
        case.reference_fix.new_str,
        1,
    )
    target_path.write_text(patched_content, encoding="utf-8")

    # Step 3: Patched state must pass pytest (returncode == 0) with intact test hashes
    fixed_run = _run_case_tests(dest_dir, case.test_command)
    if fixed_run.returncode != 0:
        return False

    for test_rel, expected_sha in expected_test_hashes.items():
        test_file = dest_dir / test_rel
        if not test_file.is_file() or _sha256_file(test_file) != expected_sha:
            return False

    return True


prepare_case_workspace = prepare_isolated_workspace
verify_case_with_reference_fix = verify_case_reference_fix
