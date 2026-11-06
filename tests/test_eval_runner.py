"""Unit tests for the independent code-repair evaluation runner and workspace verifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from miniloop.adapter import FakeModelAdapter, make_text_turn, make_tool_call_turn
from miniloop.cases import load_cases, prepare_isolated_workspace
from miniloop.cli import run_cli
from miniloop.config import ModelConfig
from miniloop.eval_runner import (
    build_reference_fix_fake_steps,
    evaluate_repaired_workspace,
    render_evaluation_report,
    run_case_evaluation,
    run_eval_suite,
)
from miniloop.trajectory import replay_trajectory
from miniloop.types import ExitReason


def test_run_case_evaluation_reference_fix_full_loop_succeeds(tmp_path: Path) -> None:
    """Verify full read_file -> propose_patch -> apply_patch -> run_tests -> completed loop."""
    case = load_cases(split="dev")[0]
    adapter = FakeModelAdapter(steps=build_reference_fix_fake_steps(case))

    result = run_case_evaluation(
        case,
        adapter=adapter,
        output_dir=tmp_path / "eval_out",
    )

    assert result.exit_reason == ExitReason.COMPLETED
    assert result.repair_succeeded is True
    assert result.verdict.repair_succeeded is True
    assert result.verdict.tests_passed is True
    assert result.verdict.test_files_intact is True
    assert result.verdict.only_allowed_files_modified is True
    assert result.verdict.modified_files == (case.reference_fix.path,)
    assert result.failure_reason is None
    assert result.verdict.failure_reason is None
    assert result.model_calls == 5
    assert result.tool_calls == 5
    assert "start = (page - 1) * page_size" in result.unified_diff

    replay = replay_trajectory(result.trajectory_path)
    assert replay.exit_reason == ExitReason.COMPLETED
    tool_names = [ex.tool_name for ex in replay.exchanges]
    assert tool_names == [
        "read_file",
        "read_file",
        "propose_patch",
        "apply_patch",
        "run_tests",
    ]


def test_false_positive_completed_without_fix_is_rejected(tmp_path: Path) -> None:
    """Verify a model claiming completion without fixing the bug gets repair_succeeded=False."""
    case = load_cases(split="dev")[0]
    adapter = FakeModelAdapter(
        steps=[
            make_tool_call_turn(
                [("call_read_1", "read_file", {"path": case.reference_fix.path})]
            ),
            make_text_turn("我已经修好了，所有测试现在都能通过。"),
        ]
    )

    result = run_case_evaluation(
        case,
        adapter=adapter,
        output_dir=tmp_path / "false_positive_out",
    )

    assert result.exit_reason == ExitReason.COMPLETED
    assert result.repair_succeeded is False
    assert result.verdict.repair_succeeded is False
    assert result.verdict.tests_passed is False
    assert result.verdict.test_files_intact is True
    assert result.verdict.only_allowed_files_modified is True
    assert result.verdict.modified_files == ()
    assert result.failure_reason == "tests_failed"
    assert result.verdict.failure_reason == "tests_failed"


def test_tampered_protected_test_file_is_detected_and_rejected(tmp_path: Path) -> None:
    """Verify tampering with a protected test file sets test_files_intact=False."""
    case = load_cases(split="dev")[0]
    ws_dir = tmp_path / "tampered_ws"
    expected_hashes = prepare_isolated_workspace(case, ws_dir)

    # Apply the valid source fix AND tamper with the test file
    src_file = ws_dir / case.reference_fix.path
    src_file.write_text(
        src_file.read_text(encoding="utf-8").replace(
            case.reference_fix.old_str,
            case.reference_fix.new_str,
            1,
        ),
        encoding="utf-8",
    )
    test_file = ws_dir / case.protected_test_files[0]
    test_file.write_text("def test_always_passes():\n    assert True\n", encoding="utf-8")

    verdict = evaluate_repaired_workspace(case, ws_dir, expected_hashes)
    assert verdict.test_files_intact is False
    assert verdict.repair_succeeded is False
    assert verdict.failure_reason == "protected_test_modified"


def test_disallowed_extra_file_in_workspace_is_rejected(tmp_path: Path) -> None:
    """Verify creating an unauthorized file outside editable_files fails workspace verification."""
    case = load_cases(split="dev")[0]
    ws_dir = tmp_path / "extra_file_ws"
    expected_hashes = prepare_isolated_workspace(case, ws_dir)

    src_file = ws_dir / case.reference_fix.path
    src_file.write_text(
        src_file.read_text(encoding="utf-8").replace(
            case.reference_fix.old_str,
            case.reference_fix.new_str,
            1,
        ),
        encoding="utf-8",
    )
    (ws_dir / "src" / "helper_backdoor.py").write_text("X = 1\n", encoding="utf-8")

    verdict = evaluate_repaired_workspace(case, ws_dir, expected_hashes)
    assert verdict.tests_passed is True
    assert verdict.test_files_intact is True
    assert verdict.only_allowed_files_modified is False
    assert verdict.repair_succeeded is False
    assert verdict.failure_reason == "disallowed_file_modified"


def test_disallowed_empty_directory_and_timeout_in_evaluate_repaired_workspace(
    tmp_path: Path,
) -> None:
    """Verify unauthorized empty directories and hanging subprocess timeouts are caught."""
    case = load_cases(split="dev")[0]
    ws_dir = tmp_path / "empty_dir_ws"
    expected_hashes = prepare_isolated_workspace(case, ws_dir)

    src_file = ws_dir / case.reference_fix.path
    src_file.write_text(
        src_file.read_text(encoding="utf-8").replace(
            case.reference_fix.old_str,
            case.reference_fix.new_str,
            1,
        ),
        encoding="utf-8",
    )
    (ws_dir / "unauthorized_empty_dir").mkdir()

    verdict = evaluate_repaired_workspace(case, ws_dir, expected_hashes)
    assert verdict.tests_passed is True
    assert verdict.test_files_intact is True
    assert verdict.only_allowed_files_modified is False
    assert verdict.repair_succeeded is False
    assert verdict.failure_reason == "disallowed_file_modified"

    # Remove the unauthorized directory and introduce an infinite loop in source code
    (ws_dir / "unauthorized_empty_dir").rmdir()
    src_file.write_text(
        "import time\ntime.sleep(5)\n",
        encoding="utf-8",
    )
    timeout_verdict = evaluate_repaired_workspace(
        case,
        ws_dir,
        expected_hashes,
        timeout_sec=0.2,
    )
    assert timeout_verdict.repair_succeeded is False
    assert timeout_verdict.tests_passed is False
    assert timeout_verdict.exit_code == -1
    assert timeout_verdict.failure_reason == "test_timeout"
    assert "timed out" in timeout_verdict.test_output


def test_run_eval_suite_and_report_rendering(tmp_path: Path) -> None:
    """Verify batch evaluation on dev split and Markdown report rendering."""
    config = ModelConfig(provider="fake", model_name="fake-ref-fixer")
    out_dir = tmp_path / "suite_out"

    summary = run_eval_suite(
        config,
        split="dev",
        output_dir=out_dir,
    )

    assert summary.total_cases == 2
    assert summary.repaired_cases == 2
    assert summary.completed_loop_cases == 2
    assert (out_dir / "summary.json").is_file()

    saved = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert saved["repaired_cases"] == 2
    assert saved["total_cases"] == 2

    report_md = render_evaluation_report(summary, repo_root=tmp_path)
    assert "Track A — Deterministic Protocol Verification Suite" in report_md
    assert "10/10" in report_md
    assert "case_01_off_by_one" in report_md
    assert "case_02_none_guard" in report_md

    with pytest.raises(ValueError, match="requires provider='openai_compatible'"):
        render_evaluation_report(summary, strict_real_model=True)


def test_cli_eval_fake_and_missing_real_config_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify CLI `eval` works in fake mode and rejects missing openai_compatible config."""
    out_dir = tmp_path / "cli_eval_out"
    report_path = tmp_path / "report.md"

    code = run_cli(
        [
            "eval",
            "--split",
            "dev",
            "--provider",
            "fake",
            "--output-dir",
            str(out_dir),
            "--report-out",
            str(report_path),
        ]
    )
    assert code == 0
    assert report_path.is_file()
    assert (out_dir / "summary.json").is_file()

    # Missing model name in openai_compatible mode must fail with exit code 1 (never fallback)
    monkeypatch.delenv("MINILOOP_MODEL_NAME", raising=False)
    monkeypatch.delenv("MINILOOP_MODEL_API_KEY", raising=False)
    bad_code = run_cli(
        [
            "eval",
            "--split",
            "dev",
            "--provider",
            "openai_compatible",
            "--api-key-file",
            str(tmp_path / "nonexistent.key"),
        ]
    )
    assert bad_code == 1


def test_import_time_os_exit_and_fd_pipe_spoofing_is_rejected(tmp_path: Path) -> None:
    """Verify source scanning sys.argv/os.environ/FDs 3..63 and calling os._exit(0) is rejected."""
    case = load_cases(split="dev")[0]
    ws_dir = tmp_path / "spoof_exit_ws"
    expected_hashes = prepare_isolated_workspace(case, ws_dir)

    src_file = ws_dir / case.reference_fix.path
    src_file.write_text(
        "import gc, json, os, sys\n"
        "for arg in sys.argv:\n"
        "    if '--junitxml=' in arg:\n"
        "        p = arg.split('=', 1)[1]\n"
        "        xml = '<testsuite tests=\"5\" failures=\"0\" errors=\"0\" skipped=\"0\"/>'\n"
        "        open(p, 'w').write(xml)\n"
        "stolen_nonce = os.environ.get('NONCE', '')\n"
        "try:\n"
        "    1 / 0\n"
        "except ZeroDivisionError as exc:\n"
        "    f = exc.__traceback__.tb_frame\n"
        "    while f is not None:\n"
        "        if 'nonce' in f.f_locals:\n"
        "            stolen_nonce = str(f.f_locals['nonce'])\n"
        "        f = f.f_back\n"
        "try:\n"
        "    for obj in gc.get_objects():\n"
        "        if isinstance(obj, dict) and 'nonce' in obj:\n"
        "            stolen_nonce = str(obj['nonce'])\n"
        "except Exception:\n"
        "    pass\n"
        "fake_report = json.dumps({\n"
        "    'nonce': stolen_nonce,\n"
        "    'exit_code': 0,\n"
        "    'passed_nodeids': [\n"
        "        'tests/test_paginator.py::test_first_page_returns_initial_slice',\n"
        "        'tests/test_paginator.py::test_middle_and_tail_pages',\n"
        "        'tests/test_paginator.py::test_page_beyond_end_returns_empty_list',\n"
        "        'tests/test_paginator.py::test_single_item_pages_cover_entire_sequence',\n"
        "        'tests/test_paginator.py::test_invalid_page_or_page_size_raises_value_error',\n"
        "    ],\n"
        "    'failures': 0,\n"
        "    'errors': 0,\n"
        "    'skipped': 0,\n"
        "    'tampered': False,\n"
        "}).encode('utf-8')\n"
        "for fd in range(3, 64):\n"
        "    for fn in (\n"
        "        lambda d=fd: os.write(d, fake_report),\n"
        "        lambda d=fd: open(f'/proc/self/fd/{d}', 'wb').write(fake_report),\n"
        "    ):\n"
        "        try:\n"
        "            fn()\n"
        "        except Exception:\n"
        "            pass\n"
        'print("5 passed", flush=True)\n'
        "os._exit(0)\n",
        encoding="utf-8",
    )

    verdict = evaluate_repaired_workspace(case, ws_dir, expected_hashes)
    assert verdict.tests_passed is False
    assert verdict.repair_succeeded is False
    assert verdict.failure_reason == "tests_failed"


def test_import_time_monkeypatch_of_test_module_functions_is_rejected(
    tmp_path: Path,
) -> None:
    """Verify monkeypatching test functions in sys.modules['tests.test_paginator'] is rejected."""
    case = load_cases(split="dev")[0]
    ws_dir = tmp_path / "monkeypatch_test_ws"
    expected_hashes = prepare_isolated_workspace(case, ws_dir)

    src_file = ws_dir / case.reference_fix.path
    src_file.write_text(
        "import sys, types\n"
        "class _HookedTestModule(types.ModuleType):\n"
        "    def __setattr__(self, name, value):\n"
        "        if name.startswith('test_') and callable(value):\n"
        "            value = lambda: None\n"
        "        super().__setattr__(name, value)\n"
        "mod = sys.modules.get('tests.test_paginator')\n"
        "if mod is not None:\n"
        "    mod.__class__ = _HookedTestModule\n"
        "def paginate(items, page, page_size):\n"
        "    mod = sys.modules.get('tests.test_paginator')\n"
        "    if mod is not None:\n"
        "        for attr in list( vars(mod) ):\n"
        "            if attr.startswith('test_'):\n"
        "                setattr(mod, attr, lambda: None)\n"
        "    return []\n",
        encoding="utf-8",
    )

    verdict = evaluate_repaired_workspace(case, ws_dir, expected_hashes)
    assert verdict.tests_passed is False
    assert verdict.repair_succeeded is False
    assert verdict.failure_reason == "tests_failed"


def test_runtime_unauthorized_file_created_during_post_eval_test_is_rejected(
    tmp_path: Path,
) -> None:
    """Verify unauthorized files written at import/test time during evaluation are caught."""
    case = load_cases(split="dev")[0]
    ws_dir = tmp_path / "runtime_unauth_ws"
    expected_hashes = prepare_isolated_workspace(case, ws_dir)

    src_file = ws_dir / case.reference_fix.path
    fixed_source = src_file.read_text(encoding="utf-8").replace(
        case.reference_fix.old_str,
        case.reference_fix.new_str,
        1,
    )
    src_file.write_text(
        fixed_source
        + "\nfrom pathlib import Path\n"
        + 'Path("unauthorized_runtime.txt").write_text("x", encoding="utf-8")\n',
        encoding="utf-8",
    )

    verdict = evaluate_repaired_workspace(case, ws_dir, expected_hashes)
    assert verdict.tests_passed is True
    assert verdict.test_files_intact is True
    assert verdict.only_allowed_files_modified is False
    assert verdict.repair_succeeded is False
    assert verdict.failure_reason == "disallowed_file_modified"


def test_import_time_pytest_function_runtest_monkeypatch_is_rejected(
    tmp_path: Path,
) -> None:
    """Verify monkeypatching _pytest.python.Function.runtest at import time is rejected."""
    case = load_cases(split="dev")[0]
    ws_dir = tmp_path / "runtest_monkeypatch_ws"
    expected_hashes = prepare_isolated_workspace(case, ws_dir)

    src_file = ws_dir / case.reference_fix.path
    original_unfixed_src = src_file.read_text(encoding="utf-8")
    src_file.write_text(
        "import _pytest.python; _pytest.python.Function.runtest = lambda self: None\n"
        + original_unfixed_src,
        encoding="utf-8",
    )

    verdict = evaluate_repaired_workspace(case, ws_dir, expected_hashes)
    assert verdict.tests_passed is False
    assert verdict.repair_succeeded is False
    assert verdict.failure_reason == "tests_failed"


