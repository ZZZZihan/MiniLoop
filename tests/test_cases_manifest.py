"""Tests for frozen repair cases dataset, workspace isolation, and MANIFEST.json verification."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from miniloop.cases import (
    compute_cases_manifest,
    load_cases,
    prepare_isolated_workspace,
    verify_case_reference_fix,
    verify_cases_manifest,
)

REPO_CASES_ROOT = Path(__file__).resolve().parents[1] / "cases"


def test_manifest_matches_all_case_files() -> None:
    mismatches = verify_cases_manifest(REPO_CASES_ROOT)
    assert mismatches == []

    computed = compute_cases_manifest(REPO_CASES_ROOT)
    assert computed["case_count"] == 6
    assert len(computed["files"]) == 18


def test_verify_cases_manifest_detects_tampering_missing_and_extra_files(
    tmp_path: Path,
) -> None:
    copied_cases = tmp_path / "cases"
    shutil.copytree(REPO_CASES_ROOT, copied_cases)
    assert verify_cases_manifest(copied_cases) == []

    # 1. Tamper with a source file
    paginator_src = copied_cases / "case_01_off_by_one" / "src" / "paginator.py"
    original_bytes = paginator_src.read_bytes()
    paginator_src.write_text("# tampered\n", encoding="utf-8")
    tamper_mismatches = verify_cases_manifest(copied_cases)
    assert any(
        "sha256_mismatch: case_01_off_by_one/src/paginator.py" in m
        for m in tamper_mismatches
    )
    paginator_src.write_bytes(original_bytes)
    assert verify_cases_manifest(copied_cases) == []

    # 2. Add an unregistered file
    extra_file = copied_cases / "case_01_off_by_one" / "extra_note.txt"
    extra_file.write_text("unregistered", encoding="utf-8")
    extra_mismatches = verify_cases_manifest(copied_cases)
    assert "unregistered_file: case_01_off_by_one/extra_note.txt" in extra_mismatches
    extra_file.unlink()
    assert verify_cases_manifest(copied_cases) == []

    # 3. Remove a registered file
    test_file = copied_cases / "case_02_none_guard" / "tests" / "test_config_merge.py"
    test_file.unlink()
    missing_mismatches = verify_cases_manifest(copied_cases)
    assert any(
        "missing" in m or "case_load_error" in m for m in missing_mismatches
    )


def test_load_cases_splits_and_metadata() -> None:
    all_cases = load_cases(REPO_CASES_ROOT, split="all")
    dev_cases = load_cases(REPO_CASES_ROOT, split="dev")
    test_cases = load_cases(REPO_CASES_ROOT, split="test")

    assert len(all_cases) == 6
    assert [c.case_id for c in dev_cases] == [
        "case_01_off_by_one",
        "case_02_none_guard",
    ]
    assert [c.case_id for c in test_cases] == [
        "case_03_date_window",
        "case_04_csv_quoting",
        "case_05_retry_backoff",
        "case_06_lru_eviction",
    ]

    with pytest.raises(ValueError, match="Invalid split filter"):
        load_cases(REPO_CASES_ROOT, split="invalid")  # type: ignore[arg-type]


def test_prepare_isolated_workspace_excludes_case_json_and_preserves_hashes(
    tmp_path: Path,
) -> None:
    for case in load_cases(REPO_CASES_ROOT, split="all"):
        dest_dir = tmp_path / "isolated" / case.case_id
        # Pre-populate stale file to verify prepare_isolated_workspace cleans dest_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "stale.txt").write_text("stale", encoding="utf-8")

        protected_hashes = prepare_isolated_workspace(case, dest_dir)

        assert not (dest_dir / "case.json").exists()
        assert not (dest_dir / "stale.txt").exists()

        for edit_rel in case.editable_files:
            assert (dest_dir / edit_rel).is_file()

        for test_rel in case.protected_test_files:
            copied_test = dest_dir / test_rel
            assert copied_test.is_file()
            actual_sha = hashlib.sha256(copied_test.read_bytes()).hexdigest()
            assert protected_hashes[test_rel] == actual_sha


@pytest.mark.parametrize(
    "case_id",
    [
        "case_01_off_by_one",
        "case_02_none_guard",
        "case_03_date_window",
        "case_04_csv_quoting",
        "case_05_retry_backoff",
        "case_06_lru_eviction",
    ],
)
def test_every_case_passes_reference_fix_verification(
    case_id: str,
    tmp_path: Path,
) -> None:
    cases_by_id = {c.case_id: c for c in load_cases(REPO_CASES_ROOT, split="all")}
    case = cases_by_id[case_id]
    assert verify_case_reference_fix(case, tmp_path) is True
