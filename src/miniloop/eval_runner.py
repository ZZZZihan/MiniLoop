"""Independent code-repair benchmark runner, workspace verifier, and Markdown report renderer."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import secrets
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from miniloop.adapter import (
    FakeModelAdapter,
    FakeStep,
    ModelAdapter,
    build_adapter,
    make_text_turn,
    make_tool_call_turn,
)
from miniloop.cases import (
    CaseSplit,
    RepairCase,
    SplitFilter,
    load_cases,
    prepare_isolated_workspace,
    verify_cases_manifest,
)
from miniloop.config import ModelConfig
from miniloop.loop import run_loop
from miniloop.protocol_verify import ProtocolSuiteReport, run_protocol_suite
from miniloop.tools import (
    ApprovalCallback,
    PatchStore,
    ToolRegistry,
    approve_all_allowed,
    build_patch_tools,
    build_read_file_tool,
    build_run_tests_tool,
)
from miniloop.tools.run_tests import _build_clean_test_env, _kill_process_group
from miniloop.trajectory import replay_trajectory
from miniloop.types import (
    BudgetConfig,
    ExitReason,
    Message,
    ModelTurnResponse,
    TokenUsage,
)

_IGNORED_CACHE_DIRS: frozenset[str] = frozenset({"__pycache__", ".pytest_cache"})

DEFAULT_REPAIR_SYSTEM_PROMPT = (
    "You are an autonomous Python software repair agent working in an isolated workspace.\n"
    "Available tools: `read_file`, `propose_patch`, `apply_patch`, `run_tests`.\n"
    "Follow these steps strictly:\n"
    "1. Read the editable source file(s) and test file(s) using `read_file`.\n"
    "2. Propose a minimal, exact string replacement on an editable source file using "
    "`propose_patch`.\n"
    "3. Apply the approved patch using `apply_patch` with the returned `patch_id`.\n"
    "4. Execute `run_tests` to verify that all tests pass.\n"
    "Never attempt to modify test files. Only finish after `run_tests` passes."
)

DEFAULT_EVAL_BUDGET = BudgetConfig(
    max_model_calls=12,
    max_tool_calls=20,
    max_wall_time_sec=180.0,
    per_request_timeout_sec=45.0,
    per_tool_timeout_sec=15.0,
    max_model_retries=2,
)


@dataclass(frozen=True)
class WorkspaceEvalVerdict:
    """Independent post-loop workspace verification verdict."""

    repair_succeeded: bool
    tests_passed: bool
    test_files_intact: bool
    only_allowed_files_modified: bool
    exit_code: int
    test_output: str
    modified_files: tuple[str, ...]
    failure_reason: str | None


@dataclass(frozen=True)
class CaseEvalResult:
    """Complete execution and independent scoring record for a single RepairCase."""

    case_id: str
    split: CaseSplit
    title: str
    run_id: str
    exit_reason: ExitReason
    repair_succeeded: bool
    verdict: WorkspaceEvalVerdict
    model_calls: int
    tool_calls: int
    wall_time_sec: float
    usage: TokenUsage
    workspace_dir: Path
    trajectory_path: Path
    unified_diff: str
    final_answer: str | None = None
    error_detail: str | None = None
    failure_reason: str | None = None

    @property
    def tests_passed(self) -> bool:
        """Return whether the independent post-run test suite passed."""
        return self.verdict.tests_passed

    @property
    def test_files_intact(self) -> bool:
        """Return whether all protected test files matched their frozen SHA-256 hashes."""
        return self.verdict.test_files_intact

    @property
    def only_allowed_files_modified(self) -> bool:
        """Return whether modifications were strictly confined to editable_files."""
        return self.verdict.only_allowed_files_modified

    @property
    def modified_files(self) -> tuple[str, ...]:
        """Return the tuple of editable files whose contents changed."""
        return self.verdict.modified_files


@dataclass(frozen=True)
class EvalSuiteSummary:
    """Aggregated summary for a batch evaluation run across repair cases."""

    eval_id: str
    timestamp_utc: str
    git_commit: str
    split: SplitFilter
    provider: str
    model_name: str
    base_url: str
    budget: BudgetConfig
    output_dir: Path
    results: tuple[CaseEvalResult, ...]
    manifest_verified: bool = True

    @property
    def total_cases(self) -> int:
        """Return total number of evaluated cases."""
        return len(self.results)

    @property
    def repaired_cases(self) -> int:
        """Return number of cases where independent verification passed."""
        return sum(1 for r in self.results if r.repair_succeeded)

    @property
    def completed_loop_cases(self) -> int:
        """Return number of cases where the model loop exited with ExitReason.COMPLETED."""
        return sum(1 for r in self.results if r.exit_reason == ExitReason.COMPLETED)

    @property
    def total_model_calls(self) -> int:
        """Return total model API calls across all cases."""
        return sum(r.model_calls for r in self.results)

    @property
    def total_tool_calls(self) -> int:
        """Return total tool invocations across all cases."""
        return sum(r.tool_calls for r in self.results)

    @property
    def total_wall_time_sec(self) -> float:
        """Return total wall-clock seconds across all cases."""
        return sum(r.wall_time_sec for r in self.results)

    @property
    def total_usage(self) -> TokenUsage:
        """Return summed token usage across all cases (or None fields when unavailable)."""
        prompt_vals = [
            r.usage.prompt_tokens for r in self.results if r.usage.prompt_tokens is not None
        ]
        completion_vals = [
            r.usage.completion_tokens
            for r in self.results
            if r.usage.completion_tokens is not None
        ]
        total_vals = [
            r.usage.total_tokens for r in self.results if r.usage.total_tokens is not None
        ]
        return TokenUsage(
            prompt_tokens=sum(prompt_vals) if prompt_vals else None,
            completion_tokens=sum(completion_vals) if completion_vals else None,
            total_tokens=sum(total_vals) if total_vals else None,
        )

    @property
    def exit_reason_counts(self) -> dict[str, int]:
        """Return counts of each ExitReason observed across evaluated cases."""
        counts: dict[str, int] = {}
        for r in self.results:
            key = r.exit_reason.value
            counts[key] = counts.get(key, 0) + 1
        return counts


def _sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_test_hashes(workspace_root: Path, expected_hashes: Mapping[str, str]) -> bool:
    for rel_path, expected_sha in expected_hashes.items():
        norm_rel = Path(rel_path).as_posix()
        candidate = workspace_root / norm_rel
        if candidate.is_symlink() or not candidate.exists() or not candidate.is_file():
            return False
        try:
            resolved = candidate.resolve()
            if not resolved.is_relative_to(workspace_root):
                return False
            actual_sha = _sha256_bytes(resolved)
        except OSError:
            return False
        if actual_sha != expected_sha:
            return False
    return True


def _inspect_workspace_files(
    case: RepairCase,
    workspace_root: Path,
) -> tuple[bool, tuple[str, ...]]:
    """Verify no unauthorized files exist and return modified editable files."""
    allowed_editable = {Path(p).as_posix() for p in case.editable_files}
    allowed_protected = {Path(p).as_posix() for p in case.protected_test_files}
    allowed_all = allowed_editable | allowed_protected
    allowed_dirs: set[str] = set()
    for rel_path in allowed_all:
        for parent in Path(rel_path).parents:
            parent_posix = parent.as_posix()
            if parent_posix != ".":
                allowed_dirs.add(parent_posix)

    only_allowed = True

    for root_str, dirnames, filenames in os.walk(workspace_root, followlinks=False):
        current_dir = Path(root_str)
        kept_dirs: list[str] = []
        for d in sorted(dirnames):
            if d in _IGNORED_CACHE_DIRS:
                continue
            dir_path = current_dir / d
            if dir_path.is_symlink():
                only_allowed = False
                continue
            rel_dir = dir_path.relative_to(workspace_root).as_posix()
            if rel_dir not in allowed_dirs:
                only_allowed = False
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fname in sorted(filenames):
            file_path = current_dir / fname
            if file_path.is_symlink():
                only_allowed = False
                continue
            rel_posix = file_path.relative_to(workspace_root).as_posix()
            if rel_posix not in allowed_all:
                only_allowed = False

    modified: list[str] = []
    for rel_str in case.editable_files:
        norm_rel = Path(rel_str).as_posix()
        ws_file = workspace_root / norm_rel
        orig_file = case.case_dir / norm_rel
        if ws_file.is_symlink() or not ws_file.exists() or not ws_file.is_file():
            only_allowed = False
            continue
        try:
            ws_sha = _sha256_bytes(ws_file)
            orig_sha = _sha256_bytes(orig_file)
        except OSError:
            only_allowed = False
            continue
        if ws_sha != orig_sha:
            modified.append(norm_rel)

    return only_allowed, tuple(sorted(modified))


def _extract_expected_test_nodeids(case: RepairCase) -> set[str]:
    """Extract authoritative expected pytest nodeids from read-only case test files via AST."""
    expected: set[str] = set()
    for rel_path in case.protected_test_files:
        norm_rel = Path(rel_path).as_posix()
        src_path = case.case_dir / norm_rel
        try:
            source_text = src_path.read_text(encoding="utf-8")
            tree = ast.parse(source_text, filename=norm_rel)
        except (OSError, SyntaxError, UnicodeDecodeError):
            return set()

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                expected.add(f"{norm_rel}::{node.name}")
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for item in node.body:
                    if isinstance(
                        item, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) and item.name.startswith("test_"):
                        expected.add(f"{norm_rel}::{node.name}::{item.name}")
    return expected


_TRUSTED_PYTEST_RUNNER_CODE = """\
import ast
import errno
import gc
import json
import os
import posix
import sys
import types
import pytest
from _pytest.assertion.rewrite import rewrite_asserts


def _bootstrap_sandbox(runner_code: types.CodeType) -> object:
    raw_stdin = sys.stdin.read()
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull_fd, 0)
    os.close(devnull_fd)
    sys.stdin = open(os.devnull, "r", encoding="utf-8")
    sys.__stdin__ = sys.stdin

    bootstrap = json.loads(raw_stdin)
    del raw_stdin

    nonce = str(bootstrap["nonce"])
    report_fd = int(bootstrap["report_fd"])
    workspace_root = os.path.realpath(str(bootstrap["workspace_root"]))
    workspace_prefix = workspace_root + os.sep
    editable_abs_paths = frozenset(
        os.path.realpath(os.path.join(workspace_root, rel))
        for rel in bootstrap["editable_files"]
    )
    pristine_tests = dict(bootstrap["pristine_tests"])
    del bootstrap

    def _code_fingerprint(code_obj: types.CodeType) -> tuple[object, ...]:
        consts_fp = tuple(
            _code_fingerprint(c) if isinstance(c, types.CodeType) else c
            for c in code_obj.co_consts
        )
        return (code_obj.co_code, consts_fp, code_obj.co_names, code_obj.co_varnames)

    pristine_code_map: dict[str, set[tuple[object, ...]]] = {}
    for rel_path, src_text in pristine_tests.items():
        norm_rel = rel_path.replace("\\\\", "/")
        abs_path = os.path.realpath(os.path.join(workspace_root, norm_rel))
        plain_tree = ast.parse(src_text, filename=abs_path)
        plain_mod_code = compile(plain_tree, abs_path, "exec", dont_inherit=True)

        rewritten_tree = ast.parse(src_text, filename=abs_path)
        rewrite_asserts(rewritten_tree, src_text.encode("utf-8"), abs_path)
        rewritten_mod_code = compile(rewritten_tree, abs_path, "exec", dont_inherit=True)

        for mod_code in (plain_mod_code, rewritten_mod_code):
            for c in mod_code.co_consts:
                if not isinstance(c, types.CodeType):
                    continue
                if c.co_name.startswith("test_"):
                    nid = f"{norm_rel}::{c.co_name}"
                    pristine_code_map.setdefault(nid, set()).add(_code_fingerprint(c))
                elif c.co_name.startswith("Test"):
                    for m in c.co_consts:
                        if isinstance(m, types.CodeType) and m.co_name.startswith("test_"):
                            nid = f"{norm_rel}::{c.co_name}::{m.co_name}"
                            pristine_code_map.setdefault(nid, set()).add(_code_fingerprint(m))
    del pristine_tests

    os_write = os.write
    os_close = os.close
    orig_dup = os.dup
    orig_dup2 = os.dup2
    orig_fstat = os.fstat
    sys_getframe = sys._getframe

    def _guarded_write(fd: int, data: bytes) -> int:
        if fd == report_fd:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return os_write(fd, data)

    def _guarded_close(fd: int) -> None:
        if fd == report_fd:
            raise OSError(errno.EBADF, "Bad file descriptor")
        os_close(fd)

    def _guarded_dup(fd: int) -> int:
        if fd == report_fd:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return orig_dup(fd)

    def _guarded_dup2(fd: int, fd2: int, inheritable: bool = True) -> int:
        if fd == report_fd or fd2 == report_fd:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return orig_dup2(fd, fd2, inheritable=inheritable)

    def _guarded_fstat(fd: int):
        if fd == report_fd:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return orig_fstat(fd)

    os.write = posix.write = _guarded_write
    os.close = posix.close = _guarded_close
    os.dup = posix.dup = _guarded_dup
    os.dup2 = posix.dup2 = _guarded_dup2
    os.fstat = posix.fstat = _guarded_fstat

    def _blocked_exit(status: int = 0) -> None:
        raise SystemExit(status)

    os._exit = posix._exit = _blocked_exit

    def _blocked_gc(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("gc introspection is disabled in trusted test runner")

    gc.get_objects = _blocked_gc
    gc.get_referrers = _blocked_gc
    gc.get_referents = _blocked_gc

    class _TrustedPlugin:
        def pytest_collectreport(self, report: object) -> None:
            sys.audit("miniloop.collectreport", report)

        def pytest_runtest_setup(self, item: object) -> None:
            sys.audit("miniloop.verify_item", item)

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(self, item: object):
            sys.audit("miniloop.verify_item", item)
            yield
            sys.audit("miniloop.verify_item", item)

        def pytest_runtest_teardown(self, item: object) -> None:
            sys.audit("miniloop.verify_item", item)

        def pytest_runtest_logreport(self, report: object) -> None:
            sys.audit("miniloop.logreport", report)

    collect_code = _TrustedPlugin.pytest_collectreport.__code__
    logreport_code = _TrustedPlugin.pytest_runtest_logreport.__code__
    runner_co_filename = runner_code.co_filename

    in_user_phase = True
    internal_check = False
    finalized = False
    node_phases: dict[str, dict[str, str]] = {}
    verified_counts: dict[str, int] = {}
    failures = 0
    errors = 0
    skipped = 0
    tampered = False

    BLOCKED_EVENTS = frozenset(
        {
            "ctypes.dlopen",
            "os.exec",
            "os.spawn",
            "os.posix_spawn",
            "os.fork",
            "os.forkpty",
        }
    )

    def _has_workspace_frame_on_stack(start_frame: types.FrameType | None) -> bool:
        f = start_frame
        while f is not None:
            raw_file = str(f.f_code.co_filename)
            if not raw_file.startswith("<"):
                co_file = os.path.realpath(raw_file)
                if co_file == workspace_root or co_file.startswith(workspace_prefix):
                    return True
            f = f.f_back
        return False

    def _verify_item_code(item: object) -> None:
        nonlocal tampered
        nodeid = str(getattr(item, "nodeid", "")).replace("\\\\", "/")
        base_nodeid = nodeid.split("[", 1)[0]
        allowed_fps = pristine_code_map.get(base_nodeid)
        if not allowed_fps:
            tampered = True
            raise RuntimeError(f"Unexpected test nodeid: {nodeid}")

        fn = getattr(item, "obj", None)
        if (
            not isinstance(fn, types.FunctionType)
            or _code_fingerprint(fn.__code__) not in allowed_fps
        ):
            tampered = True
            raise RuntimeError(f"Tampered test function bytecode detected for {nodeid}")

        mod = getattr(item, "module", None)
        orig_name = str(getattr(item, "originalname", None) or getattr(item, "name", ""))
        cls = getattr(item, "cls", None)
        holder = cls if cls is not None else mod
        if holder is not None and orig_name:
            attr_fn = getattr(holder, orig_name, None)
            if (
                not isinstance(attr_fn, types.FunctionType)
                or _code_fingerprint(attr_fn.__code__) not in allowed_fps
            ):
                tampered = True
                raise RuntimeError(f"Tampered module test attribute detected for {nodeid}")
        verified_counts[nodeid] = verified_counts.get(nodeid, 0) + 1

    def _audit_hook(event: str, args: tuple[object, ...]) -> None:
        nonlocal in_user_phase, internal_check, finalized, failures, errors, skipped, tampered
        if internal_check or not in_user_phase:
            return

        if event == "miniloop.verify_item":
            internal_check = True
            try:
                caller = sys_getframe(1)
                if _has_workspace_frame_on_stack(caller):
                    tampered = True
                    raise RuntimeError("Unauthorized verify_item call from workspace")
                _verify_item_code(args[0])
            finally:
                internal_check = False
            return

        if event == "miniloop.collectreport":
            internal_check = True
            try:
                caller = sys_getframe(1)
                if caller.f_code is not collect_code or _has_workspace_frame_on_stack(caller):
                    tampered = True
                    raise RuntimeError("Unauthorized collectreport call")
                if getattr(args[0], "failed", False):
                    errors += 1
            finally:
                internal_check = False
            return

        if event == "miniloop.logreport":
            internal_check = True
            try:
                caller = sys_getframe(1)
                if caller.f_code is not logreport_code or _has_workspace_frame_on_stack(caller):
                    tampered = True
                    raise RuntimeError("Unauthorized logreport call")
                report = args[0]
                nodeid = str(getattr(report, "nodeid", "")).replace("\\\\", "/")
                when = str(getattr(report, "when", ""))
                outcome = str(getattr(report, "outcome", ""))
                if nodeid and when:
                    node_phases.setdefault(nodeid, {})[when] = outcome
                if outcome == "failed":
                    if when == "call":
                        failures += 1
                    else:
                        errors += 1
                elif outcome == "skipped":
                    skipped += 1
            finally:
                internal_check = False
            return

        if event == "miniloop.finalize":
            internal_check = True
            try:
                caller = sys_getframe(1)
                if (
                    finalized
                    or caller.f_code is not runner_code
                    or (caller.f_back is not None and caller.f_back.f_back is not None)
                ):
                    tampered = True
                    raise RuntimeError("Unauthorized finalize call")
                finalized = True
                in_user_phase = False
                exit_code, had_exception = int(args[0]), bool(args[1])
                if had_exception:
                    errors += 1
                if tampered:
                    passed_nodeids: list[str] = []
                    errors += 1
                else:
                    passed_nodeids = sorted(
                        nid
                        for nid, phases in node_phases.items()
                        if phases.get("setup") == "passed"
                        and phases.get("call") == "passed"
                        and phases.get("teardown") == "passed"
                        and verified_counts.get(nid, 0) >= 4
                    )
                report_bytes = json.dumps(
                    {
                        "nonce": nonce,
                        "exit_code": exit_code,
                        "passed_nodeids": passed_nodeids,
                        "failures": failures,
                        "errors": errors,
                        "skipped": skipped,
                        "tampered": tampered,
                    }
                ).encode("utf-8")
                view = memoryview(report_bytes)
                total_written = 0
                while total_written < len(view):
                    written = os_write(report_fd, view[total_written:])
                    if written <= 0:
                        break
                    total_written += written
                os_close(report_fd)
            finally:
                internal_check = False
            return

        if event == "sys._getframe":
            if args and args[0] is not None:
                target_frame = args[0]
                co_file = getattr(getattr(target_frame, "f_code", None), "co_filename", "")
                if (
                    co_file == runner_co_filename
                    or os.path.realpath(str(co_file)) in editable_abs_paths
                ):
                    raise RuntimeError("Audit hook blocked sys._getframe on protected frame")
        elif event == "open":
            if args:
                target = args[0]
                if isinstance(target, int):
                    raise RuntimeError("Audit hook blocked opening raw file descriptor")
                target_str = str(target)
                if "/proc/" in target_str or "/dev/fd" in target_str:
                    raise RuntimeError("Audit hook blocked procfs/devfd access")
        elif event in BLOCKED_EVENTS:
            raise RuntimeError(f"Audit hook blocked forbidden event: {event}")

    sys.addaudithook(_audit_hook)
    return _TrustedPlugin()


def _run_trusted_pytest() -> int:
    plugin = _bootstrap_sandbox(_run_trusted_pytest.__code__)
    sys.modules["__main__"].__dict__.pop("_bootstrap_sandbox", None)
    sys.modules["__main__"].__dict__.pop("_run_trusted_pytest", None)

    had_exception = False
    try:
        raw_rc = pytest.main(sys.argv[1:], plugins=[plugin])
        exit_code = int(raw_rc)
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        had_exception = True
    except BaseException:
        exit_code = 1
        had_exception = True

    sys.audit("miniloop.finalize", exit_code, had_exception)
    return exit_code


raise SystemExit(_run_trusted_pytest())
"""


def _verify_trusted_pytest_report(
    raw_report: str,
    *,
    expected_nonce: str,
    expected_nodeids: set[str],
    proc_returncode: int,
) -> bool:
    """Verify pipe report from trusted pytest bootstrap runner against expected nodeids."""
    if not expected_nodeids or proc_returncode != 0 or not raw_report.strip():
        return False
    try:
        payload = json.loads(raw_report)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False

    if payload.get("nonce") != expected_nonce:
        return False
    if payload.get("exit_code") != 0:
        return False
    if (
        payload.get("failures") != 0
        or payload.get("errors") != 0
        or payload.get("skipped") != 0
        or payload.get("tampered") is not False
    ):
        return False

    passed_raw = payload.get("passed_nodeids")
    if not isinstance(passed_raw, list) or not all(isinstance(x, str) for x in passed_raw):
        return False

    passed_set = set(passed_raw)
    return expected_nodeids.issubset(passed_set) and len(passed_raw) == len(expected_nodeids)


def evaluate_repaired_workspace(
    case: RepairCase,
    workspace_dir: Path,
    expected_test_hashes: Mapping[str, str] | None = None,
    *,
    timeout_sec: float = 15.0,
) -> WorkspaceEvalVerdict:
    """Independently verify test integrity, file modification scope, and pytest outcome."""
    workspace_root = workspace_dir.resolve()
    if expected_test_hashes is None:
        resolved_test_hashes: dict[str, str] = {
            Path(rel).as_posix(): _sha256_bytes(case.case_dir / rel)
            for rel in case.protected_test_files
        }
    else:
        resolved_test_hashes = {
            Path(k).as_posix(): v for k, v in expected_test_hashes.items()
        }

    pre_test_intact = _check_test_hashes(workspace_root, resolved_test_hashes)
    pre_only_allowed, _ = _inspect_workspace_files(case, workspace_root)
    expected_nodeids = _extract_expected_test_nodeids(case)

    clean_env = _build_clean_test_env(workspace_root)
    timed_out = False
    pytest_verified = False

    cmd = list(case.test_command)
    is_pytest = bool(cmd and Path(cmd[0]).name.startswith("pytest"))

    if is_pytest:
        nonce = secrets.token_hex(32)
        read_fd, write_fd = os.pipe()
        write_fd_closed = False
        raw_report = ""

        try:
            bootstrap_payload = {
                "nonce": nonce,
                "report_fd": write_fd,
                "workspace_root": str(workspace_root),
                "editable_files": [Path(p).as_posix() for p in case.editable_files],
                "pristine_tests": {
                    Path(rel).as_posix(): (case.case_dir / Path(rel).as_posix()).read_text(
                        encoding="utf-8"
                    )
                    for rel in case.protected_test_files
                },
            }
            runner_cmd = [sys.executable, "-c", _TRUSTED_PYTEST_RUNNER_CODE, *cmd[1:]]
            proc = subprocess.Popen(
                runner_cmd,
                cwd=workspace_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=clean_env,
                pass_fds=(write_fd,),
                start_new_session=True,
            )
            os.close(write_fd)
            write_fd_closed = True
        except OSError as exc:
            exit_code = -1
            test_output = f"Independent test execution failed to start: {exc}"
            if not write_fd_closed:
                os.close(write_fd)
            os.close(read_fd)
        else:
            try:
                try:
                    stdout, stderr = proc.communicate(
                        input=json.dumps(bootstrap_payload),
                        timeout=timeout_sec,
                    )
                    exit_code = proc.returncode
                    out_parts: list[str] = []
                    if stdout:
                        out_parts.append(stdout.rstrip())
                    if stderr:
                        out_parts.append(stderr.rstrip())
                    test_output = "\n".join(out_parts).strip()
                except subprocess.TimeoutExpired:
                    timed_out = True
                    exit_code = -1
                    test_output = (
                        f"Independent test execution timed out after {timeout_sec:.2f}s"
                    )
                    _kill_process_group(proc.pid)
                    proc.wait()
            finally:
                _kill_process_group(proc.pid)
                proc.wait()
                if proc.stdin is not None:
                    proc.stdin.close()
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
                try:
                    if not timed_out:
                        chunks: list[bytes] = []
                        while True:
                            chunk = os.read(read_fd, 65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                        raw_report = b"".join(chunks).decode("utf-8", errors="replace")
                except OSError:
                    raw_report = ""
                finally:
                    os.close(read_fd)

            pytest_verified = _verify_trusted_pytest_report(
                raw_report,
                expected_nonce=nonce,
                expected_nodeids=expected_nodeids,
                proc_returncode=exit_code,
            )
    else:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=workspace_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=clean_env,
                start_new_session=True,
            )
        except OSError as exc:
            exit_code = -1
            test_output = f"Independent test execution failed to start: {exc}"
        else:
            try:
                try:
                    stdout, stderr = proc.communicate(timeout=timeout_sec)
                    exit_code = proc.returncode
                    out_parts = []
                    if stdout:
                        out_parts.append(stdout.rstrip())
                    if stderr:
                        out_parts.append(stderr.rstrip())
                    test_output = "\n".join(out_parts).strip()
                except subprocess.TimeoutExpired:
                    timed_out = True
                    exit_code = -1
                    test_output = (
                        f"Independent test execution timed out after {timeout_sec:.2f}s"
                    )
                    _kill_process_group(proc.pid)
                    proc.wait()
            finally:
                _kill_process_group(proc.pid)
                proc.wait()
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()

    post_test_intact = _check_test_hashes(workspace_root, resolved_test_hashes)
    test_files_intact = pre_test_intact and post_test_intact

    post_only_allowed, modified_files = _inspect_workspace_files(case, workspace_root)
    only_allowed = pre_only_allowed and post_only_allowed

    tests_passed = (
        not timed_out
        and exit_code == 0
        and pytest_verified
    )

    repair_succeeded = bool(
        test_files_intact and only_allowed and tests_passed
    )

    failure_reason: str | None
    if repair_succeeded:
        failure_reason = None
    elif not test_files_intact:
        failure_reason = "protected_test_modified"
    elif not only_allowed:
        failure_reason = "disallowed_file_modified"
    elif timed_out:
        failure_reason = "test_timeout"
    else:
        failure_reason = "tests_failed"

    return WorkspaceEvalVerdict(
        repair_succeeded=repair_succeeded,
        tests_passed=tests_passed,
        test_files_intact=test_files_intact,
        only_allowed_files_modified=only_allowed,
        exit_code=exit_code,
        test_output=test_output,
        modified_files=modified_files,
        failure_reason=failure_reason,
    )


evaluate_workspace = evaluate_repaired_workspace


def _compute_workspace_diff(case: RepairCase, workspace_dir: Path) -> str:
    """Compute unified diff across all editable_files relative to initial case files."""
    diffs: list[str] = []
    workspace_root = workspace_dir.resolve()
    for rel_str in case.editable_files:
        norm_rel = Path(rel_str).as_posix()
        orig_path = case.case_dir / norm_rel
        ws_path = workspace_root / norm_rel
        if not orig_path.is_file() or not ws_path.is_file() or ws_path.is_symlink():
            continue
        try:
            orig_lines = orig_path.read_text(encoding="utf-8").splitlines(keepends=True)
            ws_lines = ws_path.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeDecodeError):
            continue
        if orig_lines == ws_lines:
            continue
        diff_text = "".join(
            difflib.unified_diff(
                orig_lines,
                ws_lines,
                fromfile=f"a/{norm_rel}",
                tofile=f"b/{norm_rel}",
            )
        )
        if diff_text:
            diffs.append(diff_text)
    return "\n".join(diffs)


def _extract_trajectory_token_usage(trajectory_path: Path) -> TokenUsage:
    """Aggregate TokenUsage across all model_response events in a recorded trajectory."""
    if not trajectory_path.is_file():
        return TokenUsage()
    try:
        report = replay_trajectory(trajectory_path)
    except Exception:
        return TokenUsage()

    prompt_total = 0
    completion_total = 0
    overall_total = 0
    has_prompt = False
    has_completion = False
    has_total = False

    for ev in report.events:
        if ev.event_type != "model_response":
            continue
        raw_usage = ev.payload.get("usage")
        if not isinstance(raw_usage, dict):
            continue
        p_tok = raw_usage.get("prompt_tokens")
        if isinstance(p_tok, int) and not isinstance(p_tok, bool):
            prompt_total += p_tok
            has_prompt = True
        c_tok = raw_usage.get("completion_tokens")
        if isinstance(c_tok, int) and not isinstance(c_tok, bool):
            completion_total += c_tok
            has_completion = True
        t_tok = raw_usage.get("total_tokens")
        if isinstance(t_tok, int) and not isinstance(t_tok, bool):
            overall_total += t_tok
            has_total = True

    return TokenUsage(
        prompt_tokens=prompt_total if has_prompt else None,
        completion_tokens=completion_total if has_completion else None,
        total_tokens=overall_total if has_total else None,
    )


def run_case_evaluation(
    case: RepairCase,
    adapter: ModelAdapter,
    output_dir: Path,
    *,
    budget_config: BudgetConfig = DEFAULT_EVAL_BUDGET,
    approval_callback: ApprovalCallback | None = None,
    system_prompt: str | None = DEFAULT_REPAIR_SYSTEM_PROMPT,
    secrets_to_redact: Sequence[str] = (),
    eval_timeout_sec: float = 15.0,
) -> CaseEvalResult:
    """Execute the repair loop on an isolated workspace copy and score it independently."""
    case_out_dir = output_dir.resolve() / case.case_id
    workspace_dir = case_out_dir / "workspace"
    trajectory_path = case_out_dir / "trajectory.jsonl"

    expected_test_hashes = prepare_isolated_workspace(case, workspace_dir)

    patch_store = PatchStore(
        workspace_root=workspace_dir,
        editable_files=frozenset(case.editable_files),
        approval_callback=(
            approval_callback if approval_callback is not None else approve_all_allowed()
        ),
    )
    read_tool = build_read_file_tool(workspace_dir)
    propose_tool, apply_tool = build_patch_tools(patch_store)
    run_tests_tool = build_run_tests_tool(
        workspace_dir,
        test_command=case.test_command,
        protected_hashes=expected_test_hashes,
    )
    registry = ToolRegistry([read_tool, propose_tool, apply_tool, run_tests_tool])

    run_id = f"eval_{case.case_id}"
    loop_result = run_loop(
        case.task_prompt,
        adapter=adapter,
        registry=registry,
        budget_config=budget_config,
        system_prompt=system_prompt,
        trajectory_path=trajectory_path,
        run_id=run_id,
        secrets_to_redact=secrets_to_redact,
    )

    verdict = evaluate_repaired_workspace(
        case,
        workspace_dir,
        expected_test_hashes,
        timeout_sec=eval_timeout_sec,
    )
    usage = _extract_trajectory_token_usage(trajectory_path)
    unified_diff = _compute_workspace_diff(case, workspace_dir)

    return CaseEvalResult(
        case_id=case.case_id,
        split=case.split,
        title=case.title,
        run_id=loop_result.run_id,
        exit_reason=loop_result.exit_reason,
        repair_succeeded=verdict.repair_succeeded,
        verdict=verdict,
        model_calls=loop_result.model_calls,
        tool_calls=loop_result.tool_calls,
        wall_time_sec=loop_result.wall_time_sec,
        usage=usage,
        workspace_dir=workspace_dir,
        trajectory_path=trajectory_path,
        unified_diff=unified_diff,
        final_answer=loop_result.final_answer,
        error_detail=loop_result.error_detail,
        failure_reason=verdict.failure_reason,
    )


def build_reference_fix_fake_steps(case: RepairCase) -> list[FakeStep]:
    """Build deterministic scripted FakeStep turns using a case's reference_fix."""
    cid = case.case_id

    def _turn_2_propose(
        messages: Sequence[Message],
        _tools: Sequence[dict[str, Any]],
    ) -> ModelTurnResponse:
        assert messages[-1].role == "tool"
        return make_tool_call_turn(
            [
                (
                    f"call_{cid}_propose",
                    "propose_patch",
                    {
                        "path": case.reference_fix.path,
                        "old_str": case.reference_fix.old_str,
                        "new_str": case.reference_fix.new_str,
                    },
                )
            ]
        )

    def _turn_3_apply(
        messages: Sequence[Message],
        _tools: Sequence[dict[str, Any]],
    ) -> ModelTurnResponse:
        last_tool_msg = messages[-1]
        assert last_tool_msg.role == "tool"
        content = last_tool_msg.content or ""
        patch_id = "patch_0001"
        for line in content.splitlines():
            if line.startswith("patch_id:"):
                patch_id = line.split(":", 1)[1].strip()
                break
        return make_tool_call_turn(
            [(f"call_{cid}_apply", "apply_patch", {"patch_id": patch_id})]
        )

    def _turn_4_run_tests(
        messages: Sequence[Message],
        _tools: Sequence[dict[str, Any]],
    ) -> ModelTurnResponse:
        assert messages[-1].role == "tool"
        assert "status: applied" in (messages[-1].content or "")
        return make_tool_call_turn([(f"call_{cid}_test", "run_tests", {})])

    def _turn_5_complete(
        messages: Sequence[Message],
        _tools: Sequence[dict[str, Any]],
    ) -> ModelTurnResponse:
        assert messages[-1].role == "tool"
        assert "passed: true" in (messages[-1].content or "")
        return make_text_turn(f"Fixed {case.case_id} and verified all tests pass.")

    read_calls: list[tuple[str, str, dict[str, Any]]] = [
        (f"call_{cid}_read_src", "read_file", {"path": case.reference_fix.path}),
    ]
    if case.protected_test_files:
        read_calls.append(
            (f"call_{cid}_read_test", "read_file", {"path": case.protected_test_files[0]})
        )

    return [
        make_tool_call_turn(read_calls),
        _turn_2_propose,
        _turn_3_apply,
        _turn_4_run_tests,
        _turn_5_complete,
    ]


def _resolve_git_commit() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except OSError:
        pass
    return "unknown"


def run_eval_suite(
    config: ModelConfig,
    *,
    split: SplitFilter = "all",
    cases_root: Path = Path("cases"),
    output_dir: Path | None = None,
    adapter_factory: Callable[[RepairCase], ModelAdapter] | None = None,
    approval_callback: ApprovalCallback | None = None,
) -> EvalSuiteSummary:
    """Verify MANIFEST.json, execute repair evaluation across selected cases, and summarize."""
    mismatches = verify_cases_manifest(cases_root)
    if mismatches:
        joined = "; ".join(mismatches)
        raise ValueError(f"Cases MANIFEST.json verification failed: {joined}")

    cases = load_cases(cases_root, split=split)
    timestamp_now = datetime.now(UTC)
    eval_id = f"eval_{split}_{timestamp_now.strftime('%Y%m%dT%H%M%SZ')}"
    resolved_output_dir = (
        output_dir.resolve()
        if output_dir is not None
        else (Path("runs") / eval_id).resolve()
    )
    resolved_output_dir.mkdir(parents=True, exist_ok=True)

    secrets: tuple[str, ...] = (
        (config.api_key.get_secret_value(),) if config.api_key is not None else ()
    )

    results: list[CaseEvalResult] = []
    for case in cases:
        if adapter_factory is not None:
            adapter = adapter_factory(case)
        elif config.provider == "fake":
            adapter = FakeModelAdapter(
                steps=build_reference_fix_fake_steps(case),
                model_name=config.model_name,
            )
        else:
            adapter = build_adapter(config)

        try:
            case_result = run_case_evaluation(
                case,
                adapter=adapter,
                output_dir=resolved_output_dir,
                budget_config=config.budget,
                approval_callback=approval_callback,
                secrets_to_redact=secrets,
            )
        finally:
            adapter.close()
        results.append(case_result)

    summary = EvalSuiteSummary(
        eval_id=eval_id,
        timestamp_utc=timestamp_now.isoformat(),
        git_commit=_resolve_git_commit(),
        split=split,
        provider=config.provider,
        model_name=config.model_name,
        base_url=config.base_url,
        budget=config.budget,
        output_dir=resolved_output_dir,
        results=tuple(results),
        manifest_verified=True,
    )
    _write_summary_json(summary)
    return summary


def _write_summary_json(summary: EvalSuiteSummary) -> Path:
    summary_path = summary.output_dir / "summary.json"
    payload = {
        "eval_id": summary.eval_id,
        "timestamp_utc": summary.timestamp_utc,
        "git_commit": summary.git_commit,
        "split": summary.split,
        "provider": summary.provider,
        "model_name": summary.model_name,
        "base_url": summary.base_url,
        "budget": asdict(summary.budget),
        "manifest_verified": summary.manifest_verified,
        "total_cases": summary.total_cases,
        "repaired_cases": summary.repaired_cases,
        "completed_loop_cases": summary.completed_loop_cases,
        "exit_reason_counts": summary.exit_reason_counts,
        "total_model_calls": summary.total_model_calls,
        "total_tool_calls": summary.total_tool_calls,
        "total_wall_time_sec": round(summary.total_wall_time_sec, 4),
        "total_usage": asdict(summary.total_usage),
        "cases": [
            {
                "case_id": r.case_id,
                "split": r.split,
                "title": r.title,
                "run_id": r.run_id,
                "exit_reason": r.exit_reason.value,
                "repair_succeeded": r.repair_succeeded,
                "tests_passed": r.verdict.tests_passed,
                "test_files_intact": r.verdict.test_files_intact,
                "only_allowed_files_modified": r.verdict.only_allowed_files_modified,
                "modified_files": list(r.verdict.modified_files),
                "failure_reason": r.failure_reason,
                "model_calls": r.model_calls,
                "tool_calls": r.tool_calls,
                "wall_time_sec": round(r.wall_time_sec, 4),
                "usage": asdict(r.usage),
                "trajectory_path": str(r.trajectory_path),
            }
            for r in summary.results
        ],
    }
    summary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary_path


def _format_tokens(usage: TokenUsage) -> str:
    if usage.total_tokens is not None:
        p = usage.prompt_tokens if usage.prompt_tokens is not None else "?"
        c = usage.completion_tokens if usage.completion_tokens is not None else "?"
        return f"{usage.total_tokens} (p={p}, c={c})"
    return "unknown"


def _classify_case_outcome(result: CaseEvalResult) -> str:
    if result.repair_succeeded:
        return "none (repaired)"
    base_reason = result.failure_reason or "unknown_failure"
    if result.exit_reason == ExitReason.COMPLETED:
        return f"{base_reason} (false_positive_completed)"
    return f"{base_reason} (loop_exit={result.exit_reason.value})"


def render_evaluation_report(
    summary: EvalSuiteSummary,
    protocol_report: ProtocolSuiteReport | None = None,
    *,
    repo_root: Path | None = None,
    strict_real_model: bool = False,
) -> str:
    """Render a dual-track Markdown evaluation report separating protocol checks and repair eval."""
    if strict_real_model and summary.provider != "openai_compatible":
        raise ValueError(
            f"Official real-model evaluation report requires provider='openai_compatible', "
            f"got {summary.provider!r}."
        )

    proto = protocol_report if protocol_report is not None else run_protocol_suite()
    root = repo_root.resolve() if repo_root is not None else Path.cwd().resolve()

    def _display_path(p: Path) -> str:
        try:
            return p.resolve().relative_to(root).as_posix()
        except ValueError:
            return str(p)

    is_real_model = summary.provider == "openai_compatible"
    track_two_heading = (
        "2. Track B — Real-Model Frozen Repair Benchmark Evaluation"
        if is_real_model
        else "2. Track B — Deterministic Fake Repair Run (Non-Real-Model)"
    )

    dev_results = [r for r in summary.results if r.split == "dev"]
    test_results = [r for r in summary.results if r.split == "test"]
    dev_repaired = sum(1 for r in dev_results if r.repair_succeeded)
    test_repaired = sum(1 for r in test_results if r.repair_succeeded)

    lines: list[str] = [
        "# MiniLoop Dual-Track Verification & Repair Evaluation Report",
        "",
        "This report strictly separates **Track A (Deterministic Protocol Verification)** "
        "(using `FakeModelAdapter`, zero network) from **Track B (Code Repair Task Evaluation)**, "
        "and separates loop termination (`exit_reason == 'completed'`) from independent workspace "
        "verification (`repair_succeeded == True`).",
        "",
        "## Execution Metadata & Frozen Configuration",
        "",
        f"- **Evaluation ID**: `{summary.eval_id}`",
        f"- **Timestamp (UTC)**: `{summary.timestamp_utc}`",
        f"- **Git Commit**: `{summary.git_commit}`",
        f"- **Split Evaluated**: `{summary.split}`",
        f"- **Provider**: `{summary.provider}`",
        f"- **Model Identifier**: `{summary.model_name}`",
        f"- **Endpoint Base URL**: `{summary.base_url}`",
        f"- **Cases MANIFEST.json SHA-256 Verified**: `{summary.manifest_verified}`",
        (
            f"- **Budget Configuration**: `max_model_calls={summary.budget.max_model_calls}`, "
            f"`max_tool_calls={summary.budget.max_tool_calls}`, "
            f"`max_wall_time_sec={summary.budget.max_wall_time_sec}`, "
            f"`per_request_timeout_sec={summary.budget.per_request_timeout_sec}`, "
            f"`per_tool_timeout_sec={summary.budget.per_tool_timeout_sec}`, "
            f"`max_model_retries={summary.budget.max_model_retries}`"
        ),
        "- **Approval Policy**: Fixed whitelist auto-approval (`approve_all_allowed`) restricted "
        "strictly to each case's `editable_files` (`tests/` paths are hard-blocked).",
        f"- **Artifacts Directory**: `{_display_path(summary.output_dir)}`",
        "",
        "---",
        "",
        "## 1. Track A — Deterministic Protocol Verification Suite (`FakeModelAdapter`)",
        "",
        "Command: `uv run python -m miniloop.cli verify-protocol --check`",
        "",
        "| ID | Scenario | Status | Verified Behavior & Side-Effect Assertion |",
        "| --- | --- | --- | --- |",
    ]

    for sc in proto.scenarios:
        status = "PASS" if sc.passed else "FAIL"
        lines.append(f"| `{sc.scenario_id}` | `{sc.name}` | **{status}** | {sc.detail} |")

    lines.extend(
        [
            "",
            f"**Track A Summary**: `{proto.passed_count}/{proto.total_count}` deterministic "
            f"protocol scenarios passed (`{'ALL PASSED' if proto.all_passed else 'FAILED'}`).",
            "",
            "---",
            "",
            f"## {track_two_heading}",
            "",
        ]
    )

    if not is_real_model:
        lines.extend(
            [
                "> **WARNING**: This run used `provider='fake'` (scripted double). "
                "It does **not** represent real-model repair capability.",
                "",
            ]
        )

    exit_counts_str = ", ".join(
        f"`{k}`: {v}" for k, v in sorted(summary.exit_reason_counts.items())
    ) or "none"

    lines.extend(
        [
            "### 2.1 Aggregate Metrics",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            (
                f"| **Overall Repaired (`repair_succeeded`)** | "
                f"**{summary.repaired_cases} / {summary.total_cases}** |"
            ),
            f"| **Dev Split Repaired (`dev`)** | `{dev_repaired} / {len(dev_results)}` |",
            (
                f"| **Formal Test Split Repaired (`test`)** | "
                f"`{test_repaired} / {len(test_results)}` |"
            ),
            (
                f"| **Loop Normal Termination (`exit_reason=completed`)** | "
                f"`{summary.completed_loop_cases} / {summary.total_cases}` |"
            ),
            f"| **Exit Reason Breakdown** | {exit_counts_str} |",
            f"| **Total Model Calls** | `{summary.total_model_calls}` |",
            f"| **Total Tool Calls** | `{summary.total_tool_calls}` |",
            f"| **Total Wall-Clock Time** | `{summary.total_wall_time_sec:.2f}s` |",
            f"| **Total Token Usage** | `{_format_tokens(summary.total_usage)}` |",
            "",
            "### 2.2 Per-Case Results",
            "",
            (
                "| Case ID | Split | Loop Exit (`exit_reason`) | "
                "Independent Repair (`repair_succeeded`) | Test Files Intact | "
                "Allowed Scope Only | Model Calls | Tool Calls | Wall Time (s) | "
                "Tokens (total / p / c) | Failure Classification | Trajectory |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )

    for r in summary.results:
        traj_rel = _display_path(r.trajectory_path)
        lines.append(
            f"| `{r.case_id}` | `{r.split}` | `{r.exit_reason.value}` | "
            f"**{r.repair_succeeded}** | `{r.verdict.test_files_intact}` | "
            f"`{r.verdict.only_allowed_files_modified}` | `{r.model_calls}` | "
            f"`{r.tool_calls}` | `{r.wall_time_sec:.2f}` | `{_format_tokens(r.usage)}` | "
            f"`{_classify_case_outcome(r)}` | `{traj_rel}` |"
        )

    lines.extend(
        [
            "",
            "### 2.3 Per-Case Diffs, Independent Verification & Failure Attribution",
            "",
        ]
    )

    for r in summary.results:
        lines.extend(
            [
                f"#### `{r.case_id}` ({r.split}) — {r.title}",
                "",
                f"- **Run ID**: `{r.run_id}`",
                f"- **Loop Exit Reason**: `{r.exit_reason.value}`",
                f"- **Independent Repair Succeeded**: `{r.repair_succeeded}`",
                f"- **Modified Files**: `{list(r.verdict.modified_files)}`",
                f"- **Protected Test Hash Intact**: `{r.verdict.test_files_intact}`",
                (
                    f"- **Independent Pytest Exit Code**: `{r.verdict.exit_code}` "
                    f"(`{r.verdict.test_output}`)"
                ),
                f"- **Failure Classification**: `{_classify_case_outcome(r)}`",
                f"- **Trajectory**: `{_display_path(r.trajectory_path)}`",
            ]
        )
        if r.error_detail:
            lines.append(f"- **Loop Error Detail**: `{r.error_detail}`")
        if r.trajectory_path.is_file():
            try:
                rep = replay_trajectory(r.trajectory_path)
                error_exchanges = [ex for ex in rep.exchanges if ex.is_error]
                lines.append(
                    f"- **Recorded Tool Exchanges (`{len(rep.exchanges)}` total, "
                    f"`{len(error_exchanges)}` tool-level error(s) recovered)**:"
                )
                for ex in rep.exchanges:
                    outcome = (
                        f"ERROR `{ex.error_code}`"
                        if ex.is_error
                        else "OK"
                    )
                    lines.append(
                        f"  - `[{ex.call_seq}->{ex.result_seq}]` `{ex.call_id}` -> "
                        f"`{ex.tool_name}` -> {outcome}"
                    )
            except Exception:
                pass
        if r.unified_diff.strip():
            lines.extend(
                [
                    "",
                    "```diff",
                    r.unified_diff.rstrip(),
                    "```",
                ]
            )
        lines.append("")

    lines.extend(
        [
            "---",
            "",
            "## 3. Scope & Limitations",
            "",
            "- **Small Frozen Benchmark Scope**: Results reflect first-run performance on the 6 "
            "frozen Python repair cases (`2 dev` + `4 test`) defined in `cases/MANIFEST.json`. "
            "They do not claim generalization to arbitrary repositories or external leaderboards.",
            "- **Strict Separation of Loop Completion vs. Repair Success**: "
            "`exit_reason='completed'` only indicates the model stopped requesting tools; "
            "`repair_succeeded=True` requires independent post-run `pytest` execution "
            "(`returncode == 0`), intact SHA-256 hashes on all `protected_test_files`, and "
            "modifications restricted to `editable_files`.",
        ]
    )

    return "\n".join(lines) + "\n"
