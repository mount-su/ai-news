from __future__ import annotations

import json
import stat
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_news.models import RunRecord, RunStatus, SourceRun
from ai_news.storage import load_run_records, save_run_record

RUN_DATE = date(2026, 8, 30)
CUTOFF = datetime(2026, 8, 30, 15, 59, 59, 999999, tzinfo=UTC)


def _source_run() -> SourceRun:
    return SourceRun.succeeded(
        source_id="official-feed",
        source_name="Official Feed",
        item_count=1,
        elapsed_ms=10,
    ).with_diagnostics(
        recent_count=1,
        new_count=1,
        eligible_count=1,
        candidate_count=1,
        selected_count=0,
        latest_published_at=datetime(2026, 8, 30, 8, tzinfo=UTC),
    )


def _record(
    *,
    run_date: date = RUN_DATE,
    status: RunStatus = RunStatus.NO_ELIGIBLE_CONTENT,
    safe_error_code: str = "no_eligible_candidates",
) -> RunRecord:
    return RunRecord(
        date=run_date,
        cutoff_at=CUTOFF,
        status=status,
        source_runs=[_source_run()],
        safe_error_code=safe_error_code,
    )


def test_run_record_requires_safe_status_specific_error_codes() -> None:
    published = _record(status=RunStatus.PUBLISHED, safe_error_code="none")
    failed = _record(
        status=RunStatus.FAILED_BEFORE_PUBLISH,
        safe_error_code="analysis_failed",
    )

    assert published.safe_error_code == "none"
    assert failed.safe_error_code == "analysis_failed"
    with pytest.raises(ValidationError, match="safe_error_code"):
        _record(status=RunStatus.PUBLISHED, safe_error_code="analysis_failed")
    with pytest.raises(ValidationError, match="safe_error_code"):
        _record(status=RunStatus.NO_ELIGIBLE_CONTENT, safe_error_code="none")
    with pytest.raises(ValidationError, match="safe_error_code"):
        _record(status=RunStatus.FAILED_BEFORE_PUBLISH, safe_error_code="none")
    with pytest.raises(ValidationError):
        _record(status=RunStatus.FAILED_BEFORE_PUBLISH, safe_error_code="secret traceback")


def test_run_record_rejects_naive_cutoff_and_duplicate_sources() -> None:
    payload = _record().model_dump()
    payload["cutoff_at"] = CUTOFF.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone"):
        RunRecord.model_validate(payload)

    payload = _record().model_dump()
    payload["source_runs"] = [_source_run(), _source_run()]
    with pytest.raises(ValidationError, match="unique"):
        RunRecord.model_validate(payload)


def test_save_and_load_run_records_use_canonical_private_atomic_files(tmp_path: Path) -> None:
    older = _record(run_date=date(2026, 8, 29))
    latest = _record()

    save_run_record(tmp_path, older)
    save_run_record(tmp_path, latest)

    target = tmp_path / "data/runs/2026/08/2026-08-30.json"
    assert target.is_file()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert RunRecord.model_validate_json(target.read_bytes()) == latest
    assert load_run_records(tmp_path) == [latest, older]
    assert not list(tmp_path.rglob("*.tmp"))


def test_save_run_record_replaces_same_date_without_duplicates(tmp_path: Path) -> None:
    initial = _record()
    replacement = _record(
        status=RunStatus.FAILED_BEFORE_PUBLISH,
        safe_error_code="analysis_failed",
    )

    save_run_record(tmp_path, initial)
    save_run_record(tmp_path, replacement)

    assert load_run_records(tmp_path) == [replacement]


def test_load_run_records_rejects_path_date_mismatch(tmp_path: Path) -> None:
    save_run_record(tmp_path, _record())
    target = tmp_path / "data/runs/2026/08/2026-08-30.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["date"] = "2026-08-29"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="date"):
        load_run_records(tmp_path)


def test_run_record_storage_rejects_parent_and_leaf_symlink_escapes(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    parent_escape = tmp_path / "parent-escape"
    (parent_escape / "data").mkdir(parents=True)
    (parent_escape / "data/runs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        save_run_record(parent_escape, _record())
    assert not list(outside.iterdir())

    leaf_escape = tmp_path / "leaf-escape"
    target = leaf_escape / "data/runs/2026/08/2026-08-30.json"
    target.parent.mkdir(parents=True)
    outside_file = outside / "record.json"
    outside_file.write_text("outside", encoding="utf-8")
    target.symlink_to(outside_file)

    with pytest.raises(ValueError):
        save_run_record(leaf_escape, _record())
    assert outside_file.read_text(encoding="utf-8") == "outside"


def test_failed_atomic_replace_preserves_previous_run_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _record()
    replacement = _record(
        status=RunStatus.FAILED_BEFORE_PUBLISH,
        safe_error_code="analysis_failed",
    )
    save_run_record(tmp_path, initial)
    target = tmp_path / "data/runs/2026/08/2026-08-30.json"
    original_replace = Path.replace

    def fail_target_replace(self: Path, selected_target: Path) -> Path:
        if selected_target == target:
            raise OSError("simulated atomic replace failure")
        return original_replace(self, selected_target)

    monkeypatch.setattr(Path, "replace", fail_target_replace)

    with pytest.raises(OSError, match="simulated atomic replace failure"):
        save_run_record(tmp_path, replacement)

    assert RunRecord.model_validate_json(target.read_bytes()) == initial
    assert not list(tmp_path.rglob("*.tmp"))
