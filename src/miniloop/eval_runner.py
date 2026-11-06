"""Independent code-repair benchmark runner, workspace verifier, and Markdown report renderer."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
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


def _verify_junit_xml(
    junit_path: Path,
    protected_test_files: Sequence[str],
) -> bool:
    """Verify external JUnit XML confirms all protected test files collected and passed."""
    if not junit_path.is_file():
        return False
    try:
        root = ET.parse(junit_path).getroot()
        if root.tag == "testsuite":
            suites = [root]
        elif root.tag == "testsuites":
            suites = list(root.findall("testsuite"))
        else:
            return False

        if not suites:
            return False

        total_tests = sum(int(s.attrib.get("tests", 0)) for s in suites)
        total_failures = sum(int(s.attrib.get("failures", 0)) for s in suites)
        total_errors = sum(int(s.attrib.get("errors", 0)) for s in suites)
        total_skipped = sum(int(s.attrib.get("skipped", 0)) for s in suites)
        testcases = [tc for s in suites for tc in s.findall("testcase")]
    except (ET.ParseError, OSError, ValueError):
        return False

    if (
        total_tests <= 0
        or len(testcases) != total_tests
        or total_failures != 0
        or total_errors != 0
        or total_skipped != 0
    ):
        return False

    for tc in testcases:
        for bad_tag in ("failure", "error", "skipped"):
            if tc.find(bad_tag) is not None:
                return False

    classnames = [tc.attrib.get("classname", "") for tc in testcases]
    for rel_path in protected_test_files:
        mod_full = Path(rel_path).with_suffix("").as_posix().replace("/", ".")
        mod_stem = Path(rel_path).stem
        if not any(
            cn in {mod_full, mod_stem}
            or cn.startswith(f"{mod_full}.")
            or cn.startswith(f"{mod_stem}.")
            for cn in classnames
        ):
            return False

    return True


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

    clean_env = _build_clean_test_env(workspace_root)
    timed_out = False
    with tempfile.TemporaryDirectory(prefix="miniloop_eval_junit_") as tmp_dir:
        junit_path = Path(tmp_dir) / "junit.xml"
        cmd = list(case.test_command)
        if cmd and Path(cmd[0]).name.startswith("pytest"):
            cmd.append(f"--junitxml={junit_path}")

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
                    out_parts: list[str] = []
                    if stdout:
                        out_parts.append(stdout.rstrip())
                    if stderr:
                        out_parts.append(stderr.rstrip())
                    test_output = "\n".join(out_parts).strip()
                except subprocess.TimeoutExpired:
                    timed_out = True
                    exit_code = -1
                    test_output = f"Independent test execution timed out after {timeout_sec:.2f}s"
                    _kill_process_group(proc.pid)
                    proc.wait()
            finally:
                _kill_process_group(proc.pid)
                proc.wait()
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()

        junit_verified = _verify_junit_xml(junit_path, case.protected_test_files)

    post_test_intact = _check_test_hashes(workspace_root, resolved_test_hashes)
    test_files_intact = pre_test_intact and post_test_intact

    post_only_allowed, modified_files = _inspect_workspace_files(case, workspace_root)
    only_allowed = pre_only_allowed and post_only_allowed

    tests_passed = (
        not timed_out
        and exit_code == 0
        and junit_verified
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
