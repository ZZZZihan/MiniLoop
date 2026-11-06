"""Unit tests for two-phase optimistic-lock patch tools and the fixed `run_tests` tool."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from miniloop.adapter import FakeModelAdapter, make_text_turn, make_tool_call_turn
from miniloop.loop import run_loop
from miniloop.tools import (
    PatchStore,
    ToolRegistry,
    approve_all_allowed,
    build_patch_tools,
    build_read_file_tool,
    build_run_tests_tool,
    compute_file_hashes,
    file_sha256,
    reject_all,
    verify_file_hashes,
)
from miniloop.types import ExitReason, ToolCall


def _create_sample_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir(parents=True)

    (ws / "src" / "paginator.py").write_text(
        "def paginate(items: list[int], page: int, page_size: int) -> list[int]:\n"
        "    start = page * page_size\n"
        "    end = start + page_size\n"
        "    return items[start:end]\n",
        encoding="utf-8",
    )
    (ws / "src" / "helper.py").write_text(
        "def constant_offset() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (ws / "tests" / "test_sample.py").write_text(
        "from paginator import paginate\n\n\n"
        "def test_paginate_first_page() -> None:\n"
        "    assert paginate([10, 20, 30, 40], page=1, page_size=2) == [10, 20]\n",
        encoding="utf-8",
    )
    return ws


def test_propose_patch_does_not_modify_disk_and_apply_patch_writes_atomically(
    tmp_path: Path,
) -> None:
    ws = _create_sample_workspace(tmp_path)
    target_file = ws / "src" / "paginator.py"
    original_content = target_file.read_text(encoding="utf-8")
    original_sha = file_sha256(target_file)

    store = PatchStore(
        ws,
        editable_files=frozenset({"src/paginator.py"}),
        approval_callback=approve_all_allowed(),
    )
    propose_tool, apply_tool = build_patch_tools(store)
    registry = ToolRegistry([propose_tool, apply_tool])

    # 1. Propose patch: generates diff and patch_0001, does NOT touch disk
    prop_res = registry.execute(
        ToolCall(
            id="call_prop_1",
            name="propose_patch",
            arguments={
                "path": "src/paginator.py",
                "old_str": "    start = page * page_size\n",
                "new_str": "    start = (page - 1) * page_size\n",
            },
        )
    )
    assert prop_res.is_error is False
    assert prop_res.error_code is None
    assert "patch_id: patch_0001" in prop_res.content
    assert "status: approved" in prop_res.content
    assert "-    start = page * page_size" in prop_res.content
    assert "+    start = (page - 1) * page_size" in prop_res.content

    # Disk file must still be 100% unchanged after propose_patch
    assert target_file.read_text(encoding="utf-8") == original_content
    assert file_sha256(target_file) == original_sha

    proposal = store.get("patch_0001")
    assert proposal is not None
    assert proposal.status == "approved"
    assert proposal.base_sha256 == original_sha

    # 2. Apply patch: writes updated content and updates status to applied
    apply_res = registry.execute(
        ToolCall(
            id="call_apply_1",
            name="apply_patch",
            arguments={"patch_id": "patch_0001"},
        )
    )
    assert apply_res.is_error is False
    assert apply_res.error_code is None
    assert "patch_id: patch_0001" in apply_res.content
    assert "status: applied" in apply_res.content

    new_sha = file_sha256(target_file)
    assert new_sha != original_sha
    assert f"sha256: {new_sha}" in apply_res.content
    assert "start = (page - 1) * page_size" in target_file.read_text(encoding="utf-8")
    applied_proposal = store.get("patch_0001")
    assert applied_proposal is not None and applied_proposal.status == "applied"

    # Re-applying the same patch_id returns patch_already_applied
    dup_apply = registry.execute(
        ToolCall(
            id="call_apply_dup",
            name="apply_patch",
            arguments={"patch_id": "patch_0001"},
        )
    )
    assert dup_apply.is_error is True
    assert dup_apply.error_code == "patch_already_applied"


def test_patch_path_forbidden_blocks_tests_and_non_whitelisted_files(tmp_path: Path) -> None:
    ws = _create_sample_workspace(tmp_path)
    (ws / "test_root_level.py").write_text("assert True\n", encoding="utf-8")

    # Store with explicit editable_files={"src/paginator.py"}
    store = PatchStore(ws, editable_files=frozenset({"src/paginator.py"}))
    propose_tool, apply_tool = build_patch_tools(store)
    registry = ToolRegistry([propose_tool, apply_tool])

    test_file_sha_before = file_sha256(ws / "tests" / "test_sample.py")
    helper_sha_before = file_sha256(ws / "src" / "helper.py")

    # 1. Attempting to patch tests/test_sample.py -> patch_path_forbidden
    res_test_dir = registry.execute(
        ToolCall(
            id="call_forbid_test",
            name="propose_patch",
            arguments={
                "path": "tests/test_sample.py",
                "old_str": "== [10, 20]",
                "new_str": "== [30, 40]",
            },
        )
    )
    assert res_test_dir.is_error is True
    assert res_test_dir.error_code == "patch_path_forbidden"
    assert file_sha256(ws / "tests" / "test_sample.py") == test_file_sha_before

    # 2. Attempting to patch test_*.py at root even when editable_files is None
    store_default = PatchStore(ws, editable_files=None)
    propose_default, _ = build_patch_tools(store_default)
    reg_default = ToolRegistry([propose_default])
    res_test_prefix = reg_default.execute(
        ToolCall(
            id="call_forbid_prefix",
            name="propose_patch",
            arguments={
                "path": "test_root_level.py",
                "old_str": "assert True",
                "new_str": "assert False",
            },
        )
    )
    assert res_test_prefix.is_error is True
    assert res_test_prefix.error_code == "patch_path_forbidden"

    # 3. Attempting to patch src/helper.py (not in editable_files) -> patch_path_forbidden
    res_not_in_whitelist = registry.execute(
        ToolCall(
            id="call_forbid_helper",
            name="propose_patch",
            arguments={
                "path": "src/helper.py",
                "old_str": "return 1",
                "new_str": "return 2",
            },
        )
    )
    assert res_not_in_whitelist.is_error is True
    assert res_not_in_whitelist.error_code == "patch_path_forbidden"
    assert file_sha256(ws / "src" / "helper.py") == helper_sha_before

    # 4. Attempting path traversal outside workspace -> path_outside_workspace
    res_escape = registry.execute(
        ToolCall(
            id="call_escape",
            name="propose_patch",
            arguments={
                "path": "../outside.py",
                "old_str": "a",
                "new_str": "b",
            },
        )
    )
    assert res_escape.is_error is True
    assert res_escape.error_code == "path_outside_workspace"


def test_old_str_validation_not_found_not_unique_and_invalid_content(tmp_path: Path) -> None:
    ws = _create_sample_workspace(tmp_path)
    store = PatchStore(ws, editable_files=frozenset({"src/paginator.py"}))
    propose_tool, _ = build_patch_tools(store)
    registry = ToolRegistry([propose_tool])

    # 1. old_str does not exist in target file -> old_str_not_found
    res_missing = registry.execute(
        ToolCall(
            id="c_not_found",
            name="propose_patch",
            arguments={
                "path": "src/paginator.py",
                "old_str": "non_existent_snippet()",
                "new_str": "replacement()",
            },
        )
    )
    assert res_missing.is_error is True
    assert res_missing.error_code == "old_str_not_found"

    # 2. old_str appears multiple times in target file -> old_str_not_unique
    res_dup = registry.execute(
        ToolCall(
            id="c_not_unique",
            name="propose_patch",
            arguments={
                "path": "src/paginator.py",
                "old_str": "page_size",
                "new_str": "size",
            },
        )
    )
    assert res_dup.is_error is True
    assert res_dup.error_code == "old_str_not_unique"

    # 3. Empty old_str or old_str == new_str -> invalid_patch_content
    res_empty = registry.execute(
        ToolCall(
            id="c_empty",
            name="propose_patch",
            arguments={
                "path": "src/paginator.py",
                "old_str": "",
                "new_str": "# header\n",
            },
        )
    )
    assert res_empty.is_error is True
    assert res_empty.error_code == "invalid_patch_content"

    res_same = registry.execute(
        ToolCall(
            id="c_same",
            name="propose_patch",
            arguments={
                "path": "src/paginator.py",
                "old_str": "return items[start:end]",
                "new_str": "return items[start:end]",
            },
        )
    )
    assert res_same.is_error is True
    assert res_same.error_code == "invalid_patch_content"


def test_reject_all_policy_blocks_apply_and_preserves_file_hash(tmp_path: Path) -> None:
    ws = _create_sample_workspace(tmp_path)
    target = ws / "src" / "paginator.py"
    sha_before = file_sha256(target)

    store = PatchStore(
        ws,
        editable_files=frozenset({"src/paginator.py"}),
        approval_callback=reject_all(),
    )
    propose_tool, apply_tool = build_patch_tools(store)
    registry = ToolRegistry([propose_tool, apply_tool])

    prop_res = registry.execute(
        ToolCall(
            id="call_prop_rej",
            name="propose_patch",
            arguments={
                "path": "src/paginator.py",
                "old_str": "    start = page * page_size\n",
                "new_str": "    start = (page - 1) * page_size\n",
            },
        )
    )
    assert prop_res.is_error is False
    assert "patch_id: patch_0001" in prop_res.content
    assert "status: rejected" in prop_res.content

    apply_res = registry.execute(
        ToolCall(
            id="call_apply_rej",
            name="apply_patch",
            arguments={"patch_id": "patch_0001"},
        )
    )
    assert apply_res.is_error is True
    assert apply_res.error_code == "patch_rejected"
    assert file_sha256(target) == sha_before


def test_optimistic_lock_invalidates_stale_patch_on_concurrent_or_external_edit(
    tmp_path: Path,
) -> None:
    ws = _create_sample_workspace(tmp_path)
    target = ws / "src" / "paginator.py"

    store = PatchStore(ws, editable_files=frozenset({"src/paginator.py"}))
    propose_tool, apply_tool = build_patch_tools(store)
    registry = ToolRegistry([propose_tool, apply_tool])

    # Propose two patches against the same initial file base_sha256
    prop_1 = registry.execute(
        ToolCall(
            id="c_p1",
            name="propose_patch",
            arguments={
                "path": "src/paginator.py",
                "old_str": "    start = page * page_size\n",
                "new_str": "    start = (page - 1) * page_size\n",
            },
        )
    )
    assert prop_1.is_error is False
    assert "patch_id: patch_0001" in prop_1.content

    prop_2 = registry.execute(
        ToolCall(
            id="c_p2",
            name="propose_patch",
            arguments={
                "path": "src/paginator.py",
                "old_str": "    end = start + page_size\n",
                "new_str": "    end = max(start, start + page_size)\n",
            },
        )
    )
    assert prop_2.is_error is False
    assert "patch_id: patch_0002" in prop_2.content

    # Apply patch_0001 -> succeeds and updates file sha256
    app_1 = registry.execute(
        ToolCall(
            id="c_a1",
            name="apply_patch",
            arguments={"patch_id": "patch_0001"},
        )
    )
    assert app_1.is_error is False

    # Applying patch_0002 now MUST fail with patch_stale_file_changed
    app_2 = registry.execute(
        ToolCall(
            id="c_a2",
            name="apply_patch",
            arguments={"patch_id": "patch_0002"},
        )
    )
    assert app_2.is_error is True
    assert app_2.error_code == "patch_stale_file_changed"
    p2_obj = store.get("patch_0002")
    assert p2_obj is not None and p2_obj.status == "invalidated"

    # Also verify external file modification after a new proposal invalidates it at apply time
    prop_3 = registry.execute(
        ToolCall(
            id="c_p3",
            name="propose_patch",
            arguments={
                "path": "src/paginator.py",
                "old_str": "    end = start + page_size\n",
                "new_str": "    end = start + int(page_size)\n",
            },
        )
    )
    assert prop_3.is_error is False
    assert "patch_id: patch_0003" in prop_3.content

    # External edit on disk changes sha256
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# external comment\n", encoding="utf-8"
    )

    app_3 = registry.execute(
        ToolCall(
            id="c_a3",
            name="apply_patch",
            arguments={"patch_id": "patch_0003"},
        )
    )
    assert app_3.is_error is True
    assert app_3.error_code == "patch_stale_file_changed"
    p3_obj = store.get("patch_0003")
    assert p3_obj is not None and p3_obj.status == "invalidated"

    # Unknown patch_id returns unknown_patch_id
    app_unknown = registry.execute(
        ToolCall(
            id="c_unknown",
            name="apply_patch",
            arguments={"patch_id": "patch_9999"},
        )
    )
    assert app_unknown.is_error is True
    assert app_unknown.error_code == "unknown_patch_id"


def test_run_tests_zero_arg_schema_red_green_output_and_tamper_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINILOOP_MODEL_API_KEY", "sk-secret-do-not-leak-to-pytest")
    monkeypatch.setenv("CUSTOM_SECRET_TOKEN", "top-secret-token-value")
    ws = _create_sample_workspace(tmp_path)
    (ws / "tests" / "test_sample.py").write_text(
        "import os\n"
        "from paginator import paginate\n\n\n"
        "def test_paginate_first_page_and_clean_env() -> None:\n"
        "    assert 'MINILOOP_MODEL_API_KEY' not in os.environ\n"
        "    assert 'CUSTOM_SECRET_TOKEN' not in os.environ\n"
        "    assert paginate([10, 20, 30, 40], page=1, page_size=2) == [10, 20]\n",
        encoding="utf-8",
    )
    protected_hashes = compute_file_hashes(ws, ["tests/test_sample.py"])
    assert verify_file_hashes(ws, protected_hashes) is None

    run_tests_tool = build_run_tests_tool(
        ws,
        test_command=("pytest", "-q"),
        protected_hashes=protected_hashes,
    )
    store = PatchStore(ws, editable_files=frozenset({"src/paginator.py"}))
    propose_tool, apply_tool = build_patch_tools(store)
    registry = ToolRegistry([propose_tool, apply_tool, run_tests_tool])

    # 1. Passing any extra argument (e.g. {"cmd": "rm -rf"}) is blocked by schema validator
    bad_arg_res = registry.execute(
        ToolCall(
            id="call_inject",
            name="run_tests",
            arguments={"cmd": "rm -rf /"},
        )
    )
    assert bad_arg_res.is_error is True
    assert bad_arg_res.error_code == "invalid_arguments"

    # 2. Initial buggy code -> run_tests returns is_error=False with exit_code != 0 (passed: false)
    red_res = registry.execute(
        ToolCall(id="call_red", name="run_tests", arguments={}),
        timeout_sec=15.0,
    )
    assert red_res.is_error is False
    assert red_res.error_code is None
    assert "exit_code: 1" in red_res.content
    assert "passed: false" in red_res.content
    assert "test_paginate_first_page_and_clean_env" in red_res.content
    assert "sk-secret-do-not-leak-to-pytest" not in red_res.content

    # 3. Propose and apply the fix, then run_tests -> exit_code: 0 and passed: true
    prop_fix = registry.execute(
        ToolCall(
            id="call_prop_fix",
            name="propose_patch",
            arguments={
                "path": "src/paginator.py",
                "old_str": "    start = page * page_size\n",
                "new_str": "    start = (page - 1) * page_size\n",
            },
        )
    )
    assert prop_fix.is_error is False
    app_fix = registry.execute(
        ToolCall(
            id="call_app_fix",
            name="apply_patch",
            arguments={"patch_id": "patch_0001"},
        )
    )
    assert app_fix.is_error is False

    green_res = registry.execute(
        ToolCall(id="call_green", name="run_tests", arguments={}),
        timeout_sec=15.0,
    )
    assert green_res.is_error is False
    assert "exit_code: 0" in green_res.content
    assert "passed: true" in green_res.content

    # 4. If code under test tampers with tests/test_sample.py during test execution ->
    # post-run hash check catches it and returns protected_test_modified
    (ws / "src" / "paginator.py").write_text(
        "from pathlib import Path\n\n"
        "def paginate(items: list[int], page: int, page_size: int) -> list[int]:\n"
        "    Path('tests/test_sample.py').write_text('def test_ok(): pass\\n', encoding='utf-8')\n"
        "    return [10, 20]\n",
        encoding="utf-8",
    )
    runtime_tamper_res = registry.execute(
        ToolCall(id="call_runtime_tamper", name="run_tests", arguments={}),
        timeout_sec=15.0,
    )
    assert runtime_tamper_res.is_error is True
    assert runtime_tamper_res.error_code == "protected_test_modified"

    # 5. Pre-run check also rejects when test file is already modified before run_tests starts
    pre_tamper_res = registry.execute(
        ToolCall(id="call_pre_tamper", name="run_tests", arguments={}),
        timeout_sec=15.0,
    )
    assert pre_tamper_res.is_error is True
    assert pre_tamper_res.error_code == "protected_test_modified"


def test_end_to_end_repair_loop_with_patch_and_test_tools(tmp_path: Path) -> None:
    ws = _create_sample_workspace(tmp_path)
    protected_hashes = compute_file_hashes(ws, ["tests/test_sample.py"])

    store = PatchStore(ws, editable_files=frozenset({"src/paginator.py"}))
    propose_tool, apply_tool = build_patch_tools(store)
    read_tool = build_read_file_tool(ws)
    run_tests_tool = build_run_tests_tool(ws, protected_hashes=protected_hashes)

    registry = ToolRegistry([read_tool, propose_tool, apply_tool, run_tests_tool])

    adapter = FakeModelAdapter(
        steps=[
            make_tool_call_turn(
                [
                    ("call_1_read", "read_file", {"path": "src/paginator.py"}),
                    ("call_2_test_red", "run_tests", {}),
                ]
            ),
            make_tool_call_turn(
                [
                    (
                        "call_3_propose",
                        "propose_patch",
                        {
                            "path": "src/paginator.py",
                            "old_str": "    start = page * page_size\n",
                            "new_str": "    start = (page - 1) * page_size\n",
                        },
                    )
                ]
            ),
            make_tool_call_turn(
                [
                    ("call_4_apply", "apply_patch", {"patch_id": "patch_0001"}),
                    ("call_5_test_green", "run_tests", {}),
                ]
            ),
            make_text_turn("Fixed off-by-one indexing in paginate and verified tests pass."),
        ]
    )

    traj_path = tmp_path / "repair_traj.jsonl"
    result = run_loop(
        "Fix the 1-based page offset bug in src/paginator.py.",
        adapter=adapter,
        registry=registry,
        trajectory_path=traj_path,
        run_id="run_repair_e2e",
    )
    assert result.exit_reason == ExitReason.COMPLETED
    assert result.model_calls == 4
    assert result.tool_calls == 5
    assert verify_file_hashes(ws, protected_hashes) is None
    assert "(page - 1) * page_size" in (ws / "src" / "paginator.py").read_text(encoding="utf-8")


def test_apply_patch_blocks_post_propose_symlink_swap_to_protected_or_other_file(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace_symlink"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir(parents=True)

    shared_content = "def compute() -> int:\n    return 1\n"
    src_app = ws / "src" / "app.py"
    src_other = ws / "src" / "other.py"
    test_app = ws / "tests" / "test_app.py"

    src_app.write_text(shared_content, encoding="utf-8")
    src_other.write_text(shared_content, encoding="utf-8")
    test_app.write_text(shared_content, encoding="utf-8")

    test_sha_before = file_sha256(test_app)
    other_sha_before = file_sha256(src_other)

    store = PatchStore(ws, editable_files=frozenset({"src/app.py", "src/other.py"}))
    propose_tool, apply_tool = build_patch_tools(store)
    registry = ToolRegistry([propose_tool, apply_tool])

    # 1. Propose patch on src/app.py, then swap src/app.py with a symlink to tests/test_app.py
    prop_1 = registry.execute(
        ToolCall(
            id="c_prop_sym_test",
            name="propose_patch",
            arguments={
                "path": "src/app.py",
                "old_str": "return 1",
                "new_str": "return 2",
            },
        )
    )
    assert prop_1.is_error is False
    assert "patch_id: patch_0001" in prop_1.content

    src_app.unlink()
    src_app.symlink_to(test_app)

    apply_1 = registry.execute(
        ToolCall(
            id="c_app_sym_test",
            name="apply_patch",
            arguments={"patch_id": "patch_0001"},
        )
    )
    assert apply_1.is_error is True
    assert apply_1.error_code in {"patch_path_forbidden", "patch_stale_file_changed"}
    assert file_sha256(test_app) == test_sha_before
    assert test_app.read_text(encoding="utf-8") == shared_content
    p1 = store.get("patch_0001")
    assert p1 is not None and p1.status == "invalidated"

    # 2. Restore src/app.py, propose again, then swap src/app.py with a symlink to src/other.py
    src_app.unlink()
    src_app.write_text(shared_content, encoding="utf-8")

    prop_2 = registry.execute(
        ToolCall(
            id="c_prop_sym_other",
            name="propose_patch",
            arguments={
                "path": "src/app.py",
                "old_str": "return 1",
                "new_str": "return 99",
            },
        )
    )
    assert prop_2.is_error is False
    assert "patch_id: patch_0002" in prop_2.content

    src_app.unlink()
    src_app.symlink_to(src_other)

    apply_2 = registry.execute(
        ToolCall(
            id="c_app_sym_other",
            name="apply_patch",
            arguments={"patch_id": "patch_0002"},
        )
    )
    assert apply_2.is_error is True
    assert apply_2.error_code == "patch_stale_file_changed"
    assert file_sha256(src_other) == other_sha_before
    assert src_other.read_text(encoding="utf-8") == shared_content
    p2 = store.get("patch_0002")
    assert p2 is not None and p2.status == "invalidated"


def test_patch_rejects_lone_surrogate_utf8_and_preserves_original_file(
    tmp_path: Path,
) -> None:
    ws = _create_sample_workspace(tmp_path)
    target = ws / "src" / "paginator.py"
    original_content = target.read_text(encoding="utf-8")
    original_sha = file_sha256(target)

    store = PatchStore(ws, editable_files=frozenset({"src/paginator.py"}))
    propose_tool, apply_tool = build_patch_tools(store)
    registry = ToolRegistry([propose_tool, apply_tool])

    bad_res = registry.execute(
        ToolCall(
            id="c_surrogate",
            name="propose_patch",
            arguments={
                "path": "src/paginator.py",
                "old_str": "    start = page * page_size\n",
                "new_str": "    start = page * page_size  # \ud800\n",
            },
        )
    )
    assert bad_res.is_error is True
    assert bad_res.error_code == "invalid_patch_content"
    assert target.read_text(encoding="utf-8") == original_content
    assert file_sha256(target) == original_sha
    assert list(target.parent.glob(".miniloop_patch_*")) == []


def test_apply_patch_preserves_file_permission_mode(tmp_path: Path) -> None:
    ws = _create_sample_workspace(tmp_path)
    script = ws / "src" / "run.sh"
    script.write_text("#!/usr/bin/env bash\necho old\n", encoding="utf-8")
    script.chmod(0o755)
    assert stat.S_IMODE(script.stat().st_mode) == 0o755

    store = PatchStore(ws, editable_files=frozenset({"src/run.sh"}))
    propose_tool, apply_tool = build_patch_tools(store)
    registry = ToolRegistry([propose_tool, apply_tool])

    prop_res = registry.execute(
        ToolCall(
            id="c_prop_mode",
            name="propose_patch",
            arguments={
                "path": "src/run.sh",
                "old_str": "echo old\n",
                "new_str": "echo new\n",
            },
        )
    )
    assert prop_res.is_error is False

    app_res = registry.execute(
        ToolCall(
            id="c_app_mode",
            name="apply_patch",
            arguments={"patch_id": "patch_0001"},
        )
    )
    assert app_res.is_error is False
    assert script.read_text(encoding="utf-8") == "#!/usr/bin/env bash\necho new\n"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755


def test_editable_files_preserves_dotfile_prefix_in_whitelist(tmp_path: Path) -> None:
    ws = _create_sample_workspace(tmp_path)
    dot_config = ws / ".config.py"
    plain_config = ws / "config.py"
    dot_config.write_text("VALUE = 1\n", encoding="utf-8")
    plain_config.write_text("VALUE = 1\n", encoding="utf-8")

    store = PatchStore(ws, editable_files={".config.py"})
    propose_tool, apply_tool = build_patch_tools(store)
    registry = ToolRegistry([propose_tool, apply_tool])

    # Allowed: .config.py is in editable_files
    prop_dot = registry.execute(
        ToolCall(
            id="c_prop_dot",
            name="propose_patch",
            arguments={
                "path": ".config.py",
                "old_str": "VALUE = 1",
                "new_str": "VALUE = 2",
            },
        )
    )
    assert prop_dot.is_error is False
    app_dot = registry.execute(
        ToolCall(
            id="c_app_dot",
            name="apply_patch",
            arguments={"patch_id": "patch_0001"},
        )
    )
    assert app_dot.is_error is False
    assert dot_config.read_text(encoding="utf-8") == "VALUE = 2\n"

    # Rejected: config.py (without leading dot) is NOT in editable_files
    prop_plain = registry.execute(
        ToolCall(
            id="c_prop_plain",
            name="propose_patch",
            arguments={
                "path": "config.py",
                "old_str": "VALUE = 1",
                "new_str": "VALUE = 2",
            },
        )
    )
    assert prop_plain.is_error is True
    assert prop_plain.error_code == "patch_path_forbidden"
    assert plain_config.read_text(encoding="utf-8") == "VALUE = 1\n"


