# ruff: noqa: RUF001

from __future__ import annotations

import json
import multiprocessing
import os
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ai_news.models import (
    Analysis,
    Candidate,
    Category,
    DailyReport,
    NewsItem,
    RawItem,
    RunRecord,
    RunStatus,
    SourceRun,
)
from ai_news.pipeline.normalize import to_candidate
from ai_news.storage import (
    load_history_urls,
    load_reports,
    load_run_records,
    save_publication,
    save_report,
)
from ai_news.storage import repository as storage_repository

RUN_DATE = date(2026, 7, 26)
GENERATED_AT = datetime(2026, 7, 26, 10, 30, tzinfo=UTC)
TRANSACTION_JOURNAL = ".ai-news-transaction.json"


def _crash_after_first_report_replace(root_text: str, report_json: str) -> None:
    root = Path(root_text)
    report = DailyReport.model_validate_json(report_json)
    report_path = _target_paths(root, report.date)[0]
    original_replace = Path.replace

    def crash_after_replace(self: Path, target: Path) -> Path:
        result = original_replace(self, target)
        if target == report_path:
            os._exit(91)
        return result

    Path.replace = crash_after_replace
    save_report(root, report)


def _run_crashing_save(root: Path, report: DailyReport) -> None:
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_after_first_report_replace,
        args=(str(root), report.model_dump_json()),
    )
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        pytest.fail("crashing save process did not exit")
    assert process.exitcode == 91


def _crash_after_publication_replace(
    root_text: str,
    report_json: str,
    record_json: str,
    crash_after: int,
) -> None:
    root = Path(root_text)
    report = DailyReport.model_validate_json(report_json)
    record = RunRecord.model_validate_json(record_json)
    targets = set(_publication_target_paths(root, report.date))
    original_replace = Path.replace
    replacement_count = 0

    def crash_after_selected_replace(self: Path, target: Path) -> Path:
        nonlocal replacement_count
        result = original_replace(self, target)
        if target in targets:
            replacement_count += 1
            if replacement_count == crash_after:
                os._exit(95)
        return result

    Path.replace = crash_after_selected_replace
    save_publication(root, report, record)


def _run_crashing_publication(
    root: Path,
    report: DailyReport,
    record: RunRecord,
    crash_after: int,
) -> None:
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_after_publication_replace,
        args=(
            str(root),
            report.model_dump_json(),
            record.model_dump_json(),
            crash_after,
        ),
    )
    process.start()
    _join_process(process)
    assert process.exitcode == 95


def _coordinated_process_save(
    root_text: str,
    report_json: str,
    first_replaced: Any,
    release_first: Any,
    save_finished: Any,
    pause_after_first: bool,
) -> None:
    root = Path(root_text)
    report = DailyReport.model_validate_json(report_json)
    report_path = _target_paths(root, report.date)[0]
    original_replace = Path.replace

    def pause_selected_writer(self: Path, target: Path) -> Path:
        result = original_replace(self, target)
        if pause_after_first and target == report_path:
            first_replaced.set()
            if not release_first.wait(timeout=10):
                os._exit(92)
        return result

    Path.replace = pause_selected_writer
    save_report(root, report)
    save_finished.set()


def _crash_in_orphan_window(root_text: str, report_json: str, window: str) -> None:
    root = Path(root_text)
    report = DailyReport.model_validate_json(report_json)
    if window == "pre-journal":
        original_write_artifact = storage_repository._write_artifact
        artifact_count = 0

        def crash_after_first_artifact(
            root_boundary: Path,
            path: Path,
            content: bytes,
        ) -> None:
            nonlocal artifact_count
            original_write_artifact(root_boundary, path, content)
            artifact_count += 1
            if artifact_count == 1:
                os._exit(93)

        storage_repository._write_artifact = crash_after_first_artifact
    elif window == "post-commit":

        def crash_before_cleanup(*args: Any, **kwargs: Any) -> None:
            os._exit(94)

        storage_repository._cleanup_transaction_artifacts = crash_before_cleanup
    else:
        raise ValueError("unsupported crash window")
    save_report(root, report)


def _run_orphan_window_crash(root: Path, report: DailyReport, window: str) -> None:
    context = multiprocessing.get_context("fork")
    process = context.Process(
        target=_crash_in_orphan_window,
        args=(str(root), report.model_dump_json(), window),
    )
    process.start()
    _join_process(process)
    assert process.exitcode == (93 if window == "pre-journal" else 94)


def _join_process(process: multiprocessing.Process) -> None:
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        pytest.fail("coordinated save process did not exit")


def _candidate(
    slug: str,
    *,
    original_title: str | None = None,
    source: str | None = None,
    published_at: datetime | None = None,
) -> Candidate:
    return to_candidate(
        RawItem(
            source_id=f"source-{slug}",
            source_name=source or f"Source {slug}",
            source_weight=7,
            title=original_title or f"Original {slug}",
            url=f"https://example.com/news/{slug}",
            published_at=published_at or datetime(2026, 7, 26, 8, 30, tzinfo=UTC),
            excerpt="Factual excerpt.",
            category_hint=Category.MODEL,
            is_official_source=True,
        )
    )


def _analysis(
    slug: str,
    *,
    title: str | None = None,
    summary: str | None = None,
    why_it_matters: str | None = None,
    tracking_signal: str | None = None,
) -> Analysis:
    return Analysis(
        title=title or f"中文标题 {slug}",
        category=Category.MODEL,
        summary=summary or "这是一段满足最小长度要求且完全基于候选事实的中文摘要内容。",
        importance=8,
        why_it_matters=why_it_matters or "这项进展可能影响后续产品能力与行业采用。",
        tags=["模型", slug],
        is_official=True,
        marketing_risk="low",
        tracking_signal=tracking_signal or "关注官方后续评测和更新。",
    )


def _item(slug: str, **candidate_overrides: Any) -> NewsItem:
    return NewsItem.from_candidate_analysis(
        _candidate(slug, **candidate_overrides),
        _analysis(slug),
    )


def _report(
    run_date: date = RUN_DATE,
    *,
    slugs: tuple[str, ...] = ("alpha", "beta", "gamma", "delta"),
    model: str = "claude-test",
) -> DailyReport:
    return DailyReport(
        date=run_date,
        generated_at=datetime.combine(run_date, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=10),
        model=model,
        degraded=False,
        candidate_count=len(slugs),
        items=[_item(slug) for slug in slugs],
        source_runs=[
            SourceRun.succeeded(
                source_id="official-feed",
                source_name="Official Feed",
                item_count=len(slugs),
                elapsed_ms=125,
            )
        ],
    )


def _target_paths(root: Path, run_date: date = RUN_DATE) -> tuple[Path, Path, Path]:
    return (
        root / f"data/{run_date:%Y/%m}/{run_date.isoformat()}.json",
        root / f"content/{run_date.isoformat()}.md",
        root / "data/index.json",
    )


def _publication_target_paths(
    root: Path,
    run_date: date = RUN_DATE,
) -> tuple[Path, Path, Path, Path]:
    return (
        *_target_paths(root, run_date),
        root / f"data/runs/{run_date:%Y/%m}/{run_date.isoformat()}.json",
    )


def _published_record(report: DailyReport) -> RunRecord:
    return RunRecord(
        date=report.date,
        cutoff_at=report.generated_at,
        status=RunStatus.PUBLISHED,
        source_runs=report.source_runs,
        safe_error_code="none",
    )


def _repository_artifacts(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file()
        and (
            path.name.endswith(".tmp")
            or path.name.endswith(".bak")
            or path.name == TRANSACTION_JOURNAL
        )
    ]


def test_news_item_factory_is_flat_json_safe_and_preserves_public_fields() -> None:
    candidate = _candidate("factory")
    analysis = _analysis("factory")

    item = NewsItem.from_candidate_analysis(candidate, analysis)
    payload = item.model_dump(mode="json")

    assert payload == {
        "id": candidate.id,
        "canonical_url": str(candidate.canonical_url),
        "original_title": candidate.raw.title,
        "source": candidate.raw.source_name,
        "source_id": candidate.raw.source_id,
        "source_role": "primary",
        "discovery_verified": False,
        "published_at": "2026-07-26T08:30:00Z",
        "title": analysis.title,
        "category": Category.MODEL.value,
        "editorial_lane": "产品工程",
        "summary": analysis.summary,
        "importance": 8,
        "why_it_matters": analysis.why_it_matters,
        "tags": ["模型", "factory"],
        "is_official": True,
        "marketing_risk": "low",
        "tracking_signal": analysis.tracking_signal,
    }
    assert "raw" not in payload
    assert "analysis" not in payload
    json.dumps(payload, ensure_ascii=False)


def test_news_item_infers_legacy_editorial_lane_from_category() -> None:
    payload = _item("legacy-lane").model_dump(mode="json")
    payload.pop("editorial_lane", None)
    payload["category"] = Category.RESEARCH.value

    item = NewsItem.model_validate(payload)

    assert item.editorial_lane.value == "前沿研究"


def test_legacy_news_item_without_source_provenance_remains_loadable() -> None:
    payload = _item("legacy-provenance").model_dump(mode="json")
    for field_name in ("source_id", "source_role", "discovery_verified"):
        payload.pop(field_name, None)

    item = NewsItem.model_validate(payload)

    assert item.source_id is None
    assert item.source_role is None
    assert item.discovery_verified is None


@pytest.mark.parametrize("missing_field", ["source_id", "source_role", "discovery_verified"])
def test_schema_1_3_report_requires_complete_item_provenance(missing_field: str) -> None:
    payload = _report(slugs=("v13",)).model_dump(mode="json")
    payload["schema_version"] = "1.3"
    payload["items"][0].pop(missing_field, None)

    with pytest.raises(ValidationError, match="provenance"):
        DailyReport.model_validate(payload)


def test_new_daily_report_serializes_schema_1_1() -> None:
    payload = _report().model_dump(mode="json")

    assert payload["schema_version"] == "1.1"
    assert all(item["editorial_lane"] == "产品工程" for item in payload["items"])


def test_schema_1_0_report_without_editorial_lanes_remains_loadable() -> None:
    payload = _report().model_dump(mode="json")
    payload["schema_version"] = "1.0"
    for item in payload["items"]:
        item.pop("editorial_lane", None)

    loaded = DailyReport.model_validate(payload)

    assert loaded.schema_version == "1.0"
    assert all(item.editorial_lane.value == "产品工程" for item in loaded.items)


def test_schema_1_1_report_requires_explicit_editorial_lanes() -> None:
    payload = _report().model_dump(mode="json")
    payload["items"][0].pop("editorial_lane")

    with pytest.raises(ValidationError, match="editorial_lane"):
        DailyReport.model_validate(payload)


def test_public_models_reject_extra_fields_insecure_urls_and_naive_datetimes() -> None:
    item_payload = _item("validation").model_dump(mode="json")
    report_payload = _report(slugs=("validation",)).model_dump(mode="json")

    with pytest.raises(ValidationError):
        NewsItem.model_validate({**item_payload, "raw": {}})
    with pytest.raises(ValidationError, match="HTTPS"):
        NewsItem.model_validate({**item_payload, "canonical_url": "http://example.com/news"})
    with pytest.raises(ValidationError, match="timezone"):
        NewsItem.model_validate({**item_payload, "published_at": datetime(2026, 7, 26, 8, 30)})
    with pytest.raises(ValidationError, match="timezone"):
        DailyReport.model_validate(
            {**report_payload, "generated_at": datetime(2026, 7, 26, 10, 30)}
        )
    with pytest.raises(ValidationError):
        DailyReport.model_validate({**report_payload, "schema_version": "2.0"})


def test_news_item_rejects_id_that_does_not_match_canonical_url() -> None:
    item_payload = _item("identity").model_dump(mode="json")
    forged_id = "0" * 16
    assert forged_id != item_payload["id"]

    with pytest.raises(ValidationError, match="id"):
        NewsItem.model_validate({**item_payload, "id": forged_id})


def test_save_report_revalidates_news_item_identity(tmp_path: Path) -> None:
    report = _report(slugs=("identity",))
    forged_item = report.items[0].model_copy(update={"id": "0" * 16})
    forged_report = report.model_copy(update={"items": [forged_item]})

    with pytest.raises(ValidationError, match="id"):
        save_report(tmp_path, forged_report)

    assert all(not path.exists() for path in _target_paths(tmp_path))


def test_load_reports_rejects_news_item_identity_corrupted_on_disk(tmp_path: Path) -> None:
    save_report(tmp_path, _report(slugs=("identity",)))
    report_path = _target_paths(tmp_path)[0]
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["items"][0]["id"] = "0" * 16
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="id"):
        load_reports(tmp_path)


def test_source_run_factories_enforce_safe_error_metadata() -> None:
    success = SourceRun.succeeded(
        source_id="official-feed",
        source_name="Official Feed",
        item_count=3,
        elapsed_ms=10,
    )
    failure = SourceRun.failed(
        source_id="official-feed",
        source_name="Official Feed",
        elapsed_ms=12,
        error=RuntimeError("token=do-not-copy"),
    )

    assert success.error_type is None
    assert failure.model_dump() == {
        "source_id": "official-feed",
        "source_name": "Official Feed",
        "success": False,
        "item_count": None,
        "elapsed_ms": 12,
        "error_type": "RuntimeError",
        "recent_count": None,
        "new_count": None,
        "eligible_count": None,
        "candidate_count": None,
        "selected_count": None,
        "latest_published_at": None,
    }
    assert "do-not-copy" not in failure.model_dump_json()
    with pytest.raises(TypeError, match="item_count"):
        SourceRun.failed(
            source_id="official-feed",
            source_name="Official Feed",
            elapsed_ms=12,
            error=RuntimeError("safe"),
            item_count=0,
        )
    with pytest.raises(ValidationError):
        SourceRun(
            source_id="official-feed",
            source_name="Official Feed",
            success=True,
            item_count=1,
            elapsed_ms=10,
            error_type="RuntimeError",
        )
    with pytest.raises(ValidationError):
        SourceRun(
            source_id="official-feed",
            source_name="Official Feed",
            success=False,
            item_count=0,
            elapsed_ms=10,
            error_type="RuntimeError: token=secret",
        )


@pytest.mark.parametrize("item_count", [None, "missing"])
def test_successful_source_run_requires_an_item_count(item_count: object) -> None:
    payload = {
        "source_id": "official-feed",
        "source_name": "Official Feed",
        "success": True,
        "elapsed_ms": 10,
        "error_type": None,
    }
    if item_count != "missing":
        payload["item_count"] = item_count

    with pytest.raises(ValidationError, match="successful source runs require an item_count"):
        SourceRun.model_validate(payload)


def test_legacy_source_run_payloads_default_new_diagnostics_to_none() -> None:
    source_run = SourceRun.model_validate(
        {
            "source_id": "official-feed",
            "source_name": "Official Feed",
            "success": True,
            "item_count": 3,
            "elapsed_ms": 10,
            "error_type": None,
        }
    )
    failed_source_run = SourceRun.model_validate(
        {
            "source_id": "legacy-failure",
            "source_name": "Legacy Failure",
            "success": False,
            "item_count": 0,
            "elapsed_ms": 12,
            "error_type": "RuntimeError",
        }
    )

    assert source_run.item_count == 3
    assert source_run.recent_count is None
    assert source_run.new_count is None
    assert source_run.eligible_count is None
    assert source_run.candidate_count is None
    assert source_run.selected_count is None
    assert source_run.latest_published_at is None
    assert failed_source_run.item_count == 0
    assert failed_source_run.recent_count is None
    assert failed_source_run.new_count is None
    assert failed_source_run.eligible_count is None
    assert failed_source_run.candidate_count is None
    assert failed_source_run.selected_count is None
    assert failed_source_run.latest_published_at is None


def test_source_run_with_diagnostics_returns_a_validated_immutable_copy() -> None:
    source_run = SourceRun.succeeded(
        source_id="official-feed",
        source_name="Official Feed",
        item_count=4,
        elapsed_ms=10,
    )
    latest = datetime(2026, 8, 30, 1, tzinfo=UTC)

    enriched = source_run.with_diagnostics(
        recent_count=3,
        new_count=2,
        eligible_count=1,
        candidate_count=1,
        selected_count=1,
        latest_published_at=latest,
    )

    assert source_run.recent_count is None
    assert source_run.latest_published_at is None
    assert enriched.model_dump(mode="json") == {
        "source_id": "official-feed",
        "source_name": "Official Feed",
        "success": True,
        "item_count": 4,
        "elapsed_ms": 10,
        "error_type": None,
        "recent_count": 3,
        "new_count": 2,
        "eligible_count": 1,
        "candidate_count": 1,
        "selected_count": 1,
        "latest_published_at": "2026-08-30T01:00:00Z",
    }
    with pytest.raises(ValidationError, match="frozen"):
        enriched.recent_count = 2
    with pytest.raises(ValidationError, match="Extra inputs"):
        source_run.with_diagnostics(unknown_count=1)
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        source_run.with_diagnostics(recent_count=-1)


@pytest.mark.parametrize(
    "field_name",
    [
        "item_count",
        "recent_count",
        "new_count",
        "eligible_count",
        "candidate_count",
        "selected_count",
    ],
)
def test_source_run_rejects_negative_diagnostic_counts(field_name: str) -> None:
    payload = {
        "source_id": "official-feed",
        "source_name": "Official Feed",
        "success": True,
        "item_count": 1,
        "elapsed_ms": 10,
        "error_type": None,
        field_name: -1,
    }

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        SourceRun.model_validate(payload)


def test_source_run_rejects_naive_latest_published_at_on_create_and_copy() -> None:
    payload = {
        "source_id": "official-feed",
        "source_name": "Official Feed",
        "success": True,
        "item_count": 1,
        "elapsed_ms": 10,
        "error_type": None,
        "latest_published_at": datetime(2026, 8, 30, 1),
    }
    source_run = SourceRun.succeeded(
        source_id="official-feed",
        source_name="Official Feed",
        item_count=1,
        elapsed_ms=10,
    )

    with pytest.raises(ValidationError, match="timezone"):
        SourceRun.model_validate(payload)
    with pytest.raises(ValidationError, match="timezone"):
        source_run.with_diagnostics(latest_published_at=datetime(2026, 8, 30, 1))


def test_save_report_writes_utf8_valid_json_markdown_and_index(tmp_path: Path) -> None:
    report = _report()

    save_report(tmp_path, report)

    report_path, markdown_path, index_path = _target_paths(tmp_path)
    report_bytes = report_path.read_bytes()
    assert b"\xe4\xb8\xad\xe6\x96\x87\xe6\xa0\x87\xe9\xa2\x98" in report_bytes
    stored_report = DailyReport.model_validate_json(report_bytes)
    assert stored_report == report

    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# AI 资讯日报｜2026-07-26\n")
    assert markdown.endswith("\n")
    assert "## 今日最重要" in markdown
    top_three = markdown.split("## 全部条目", maxsplit=1)[0]
    assert all(f"中文标题 {slug}" in top_three for slug in ("alpha", "beta", "gamma"))
    assert "中文标题 delta" not in top_three
    assert "摘要：" in markdown
    assert "重要性：8/10" in markdown
    assert "价值：" in markdown
    assert "跟踪信号：" in markdown
    assert "[Source alpha](https://example.com/news/alpha)" in markdown

    assert json.loads(index_path.read_text(encoding="utf-8")) == {
        "reports": [
            {
                "date": "2026-07-26",
                "item_count": 4,
                "path": "data/2026/07/2026-07-26.json",
            }
        ]
    }
    assert not _repository_artifacts(tmp_path)


def test_save_publication_atomically_writes_report_markdown_index_and_run_record(
    tmp_path: Path,
) -> None:
    report = _report()
    record = _published_record(report)

    save_publication(tmp_path, report, record)

    report_path, markdown_path, index_path, record_path = _publication_target_paths(tmp_path)
    assert DailyReport.model_validate_json(report_path.read_bytes()) == report
    assert markdown_path.is_file()
    assert json.loads(index_path.read_text(encoding="utf-8"))["reports"][0]["item_count"] == 4
    assert RunRecord.model_validate_json(record_path.read_bytes()) == record
    assert load_reports(tmp_path) == [report]
    assert load_run_records(tmp_path) == [record]
    assert not _repository_artifacts(tmp_path)


def test_save_publication_rejects_mismatched_or_non_published_record(tmp_path: Path) -> None:
    report = _report()
    mismatched = _published_record(_report(date(2026, 7, 25)))
    failed = RunRecord(
        date=report.date,
        cutoff_at=report.generated_at,
        status=RunStatus.FAILED_BEFORE_PUBLISH,
        source_runs=report.source_runs,
        safe_error_code="analysis_failed",
    )

    with pytest.raises(ValueError, match="date"):
        save_publication(tmp_path, report, mismatched)
    with pytest.raises(ValueError, match="published"):
        save_publication(tmp_path, report, failed)

    assert all(not path.exists() for path in _publication_target_paths(tmp_path))


@pytest.mark.parametrize("failure_number", [1, 2, 3, 4])
def test_save_publication_rolls_back_all_four_targets_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_number: int,
) -> None:
    old_report = _report(slugs=("old",), model="old-model")
    old_record = _published_record(old_report)
    save_publication(tmp_path, old_report, old_record)
    targets = _publication_target_paths(tmp_path)
    old_bytes = {path: path.read_bytes() for path in targets}
    new_report = _report(slugs=("new", "items"), model="new-model")
    new_record = _published_record(new_report)
    original_replace = Path.replace
    replacement_count = 0

    def fail_selected_replace(self: Path, target: Path) -> Path:
        nonlocal replacement_count
        if target in targets:
            replacement_count += 1
            if replacement_count == failure_number:
                raise OSError("simulated publication failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_selected_replace)

    with pytest.raises(OSError, match="simulated publication failure"):
        save_publication(tmp_path, new_report, new_record)

    assert {path: path.read_bytes() for path in targets} == old_bytes
    assert not _repository_artifacts(tmp_path)


@pytest.mark.parametrize("crash_after", [1, 2, 3, 4])
def test_load_recovers_version_two_publication_crash_across_all_four_replacements(
    tmp_path: Path,
    crash_after: int,
) -> None:
    old_report = _report(slugs=("old",), model="old-model")
    old_record = _published_record(old_report)
    save_publication(tmp_path, old_report, old_record)
    old_bytes = {path: path.read_bytes() for path in _publication_target_paths(tmp_path)}
    crashing_report = _report(slugs=("crash",), model="crash-model")
    crashing_record = _published_record(crashing_report)

    _run_crashing_publication(tmp_path, crashing_report, crashing_record, crash_after)

    assert (tmp_path / TRANSACTION_JOURNAL).is_file()
    assert load_reports(tmp_path) == [old_report]
    assert load_run_records(tmp_path) == [old_record]
    assert {path: path.read_bytes() for path in _publication_target_paths(tmp_path)} == old_bytes
    assert not _repository_artifacts(tmp_path)


def test_markdown_escapes_untrusted_text_and_prevents_line_injection(tmp_path: Path) -> None:
    candidate = _candidate(
        "unsafe",
        original_title="<script>alert(1)</script>\n# forged heading",
        source="Source](javascript:alert(1))\n# injected",
    )
    analysis = _analysis(
        "unsafe",
        title="<img src=x onerror=alert(1)>\n## forged",
        summary="安全摘要内容足够长，但是包含 <script>alert(1)</script> 与\n# 注入标题。",
        why_it_matters="这个价值字段包含 [恶意链接](javascript:alert(1)) 和换行\n> 引用。",
        tracking_signal="跟踪 <b>HTML</b>\n- 伪造列表。",
    )
    report = DailyReport(
        date=RUN_DATE,
        generated_at=GENERATED_AT,
        model="claude-test",
        degraded=False,
        candidate_count=1,
        items=[NewsItem.from_candidate_analysis(candidate, analysis)],
        source_runs=[],
    )

    save_report(tmp_path, report)

    markdown = _target_paths(tmp_path)[1].read_text(encoding="utf-8")
    assert "<script>" not in markdown
    assert "<img " not in markdown
    assert "<b>" not in markdown
    assert "\n# forged" not in markdown
    assert "\n## forged" not in markdown
    assert "\n# injected" not in markdown
    assert "\n> 引用" not in markdown
    assert "\n- 伪造列表" not in markdown
    assert (
        r"[Source\]\(javascript:alert\(1\)\) \# injected]"
        "(https://example.com/news/unsafe)"
    ) in markdown


def test_same_date_save_replaces_report_and_index_entry_without_duplicates(
    tmp_path: Path,
) -> None:
    save_report(tmp_path, _report(slugs=("alpha",), model="first-model"))
    save_report(tmp_path, _report(slugs=("beta", "gamma"), model="second-model"))

    reports = load_reports(tmp_path)
    index = json.loads(_target_paths(tmp_path)[2].read_text(encoding="utf-8"))

    assert len(reports) == 1
    assert reports[0].model == "second-model"
    assert [item.title for item in reports[0].items] == ["中文标题 beta", "中文标题 gamma"]
    assert index["reports"] == [
        {
            "date": RUN_DATE.isoformat(),
            "item_count": 2,
            "path": "data/2026/07/2026-07-26.json",
        }
    ]


@pytest.mark.parametrize("failure_number", [1, 2, 3])
def test_save_report_rolls_back_all_existing_files_when_any_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_number: int,
) -> None:
    save_report(tmp_path, _report(slugs=("old",), model="old-model"))
    targets = _target_paths(tmp_path)
    old_bytes = {path: path.read_bytes() for path in targets}
    original_replace = Path.replace
    replacement_count = 0

    def fail_selected_replace(self: Path, target: Path) -> Path:
        nonlocal replacement_count
        if target in targets:
            replacement_count += 1
            if replacement_count == failure_number:
                raise OSError("simulated commit failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_selected_replace)

    with pytest.raises(OSError, match="simulated commit failure"):
        save_report(tmp_path, _report(slugs=("new", "items"), model="new-model"))

    assert {path: path.read_bytes() for path in targets} == old_bytes
    assert not _repository_artifacts(tmp_path)


@pytest.mark.parametrize("failure_number", [1, 2, 3])
def test_save_report_removes_partial_first_write_when_any_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_number: int,
) -> None:
    targets = _target_paths(tmp_path)
    original_replace = Path.replace
    replacement_count = 0

    def fail_selected_replace(self: Path, target: Path) -> Path:
        nonlocal replacement_count
        if target in targets:
            replacement_count += 1
            if replacement_count == failure_number:
                raise OSError("simulated first-write failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_selected_replace)

    with pytest.raises(OSError, match="simulated first-write failure"):
        save_report(tmp_path, _report())

    assert all(not path.exists() for path in targets)
    assert not _repository_artifacts(tmp_path)


def test_history_urls_uses_prior_calendar_day_window_and_rejects_invalid_days(
    tmp_path: Path,
) -> None:
    run_date = date(2026, 7, 31)
    for stored_date, slug in [
        (run_date - timedelta(days=31), "too-old"),
        (run_date - timedelta(days=30), "lower-bound"),
        (run_date - timedelta(days=1), "yesterday"),
        (run_date, "today"),
    ]:
        save_report(tmp_path, _report(stored_date, slugs=(slug,)))

    assert load_history_urls(tmp_path, run_date) == {
        "https://example.com/news/lower-bound",
        "https://example.com/news/yesterday",
    }
    assert load_history_urls(tmp_path / "missing", run_date) == set()
    with pytest.raises(ValueError, match="days"):
        load_history_urls(tmp_path, run_date, days=0)


def test_load_reports_returns_index_order_and_rejects_date_or_count_mismatch(
    tmp_path: Path,
) -> None:
    for stored_date in [date(2026, 7, 24), date(2026, 7, 26), date(2026, 7, 25)]:
        save_report(tmp_path, _report(stored_date, slugs=(stored_date.isoformat(),)))

    assert [report.date for report in load_reports(tmp_path)] == [
        date(2026, 7, 26),
        date(2026, 7, 25),
        date(2026, 7, 24),
    ]

    index_path = tmp_path / "data/index.json"
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    index_payload["reports"][0]["item_count"] = 99
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="item_count"):
        load_reports(tmp_path)

    index_payload["reports"][0]["item_count"] = 1
    index_payload["reports"][0]["date"] = "2026-07-23"
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="date"):
        load_reports(tmp_path)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.json",
        "/tmp/outside.json",
        "data/2026/07/../../../../outside.json",
    ],
)
def test_load_reports_rejects_index_path_escape(tmp_path: Path, unsafe_path: str) -> None:
    save_report(tmp_path, _report(slugs=("safe",)))
    index_path = tmp_path / "data/index.json"
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    index_payload["reports"][0]["path"] = unsafe_path
    index_path.write_text(json.dumps(index_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="path"):
        load_reports(tmp_path)


def test_load_reports_rejects_symlink_escape(tmp_path: Path) -> None:
    save_report(tmp_path, _report(slugs=("safe",)))
    report_path = _target_paths(tmp_path)[0]
    outside_path = tmp_path / "outside.json"
    report_path.replace(outside_path)
    report_path.symlink_to(outside_path)

    with pytest.raises(ValueError, match="path"):
        load_reports(tmp_path)


def test_loaders_reject_index_symlink_that_escapes_root(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    outside = tmp_path / "outside"
    (root / "data").mkdir(parents=True)
    outside.mkdir()
    outside_index = outside / "index.json"
    outside_index.write_text('{"reports": []}\n', encoding="utf-8")
    (root / "data/index.json").symlink_to(outside_index)

    with pytest.raises(ValueError, match="root"):
        load_reports(root)
    with pytest.raises(ValueError, match="root"):
        load_history_urls(root, RUN_DATE)


def test_data_directory_symlink_escape_is_rejected_without_external_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    outside = tmp_path / "outside-data"
    root.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"unchanged")
    (root / "data").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="root"):
        save_report(root, _report(slugs=("escape",)))

    assert {path.name: path.read_bytes() for path in outside.iterdir()} == {
        "sentinel.bin": b"unchanged"
    }
    with pytest.raises(ValueError, match="root"):
        load_reports(root)
    with pytest.raises(ValueError, match="root"):
        load_history_urls(root, RUN_DATE)


def test_content_directory_symlink_escape_is_rejected_without_external_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    outside = tmp_path / "outside-content"
    root.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"unchanged")
    (root / "content").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="root"):
        save_report(root, _report(slugs=("escape",)))

    assert {path.name: path.read_bytes() for path in outside.iterdir()} == {
        "sentinel.bin": b"unchanged"
    }


def test_report_leaf_symlink_escape_is_rejected_without_external_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    outside = tmp_path / "outside-report.json"
    report_path = _target_paths(root)[0]
    report_path.parent.mkdir(parents=True)
    outside.write_bytes(b"external sentinel")
    report_path.symlink_to(outside)

    with pytest.raises(ValueError, match="root"):
        save_report(root, _report(slugs=("escape",)))

    assert outside.read_bytes() == b"external sentinel"
    assert report_path.is_symlink()


def test_root_symlink_uses_resolved_repository_as_storage_boundary(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual-repository"
    root_alias = tmp_path / "repository-alias"
    actual_root.mkdir()
    root_alias.symlink_to(actual_root, target_is_directory=True)

    save_report(root_alias, _report(slugs=("allowed",)))

    assert [report.date for report in load_reports(root_alias)] == [RUN_DATE]
    assert _target_paths(actual_root)[0].is_file()


def test_load_reports_recovers_old_transaction_after_process_crash(tmp_path: Path) -> None:
    old_report = _report(slugs=("old",), model="old-model")
    save_report(tmp_path, old_report)
    targets = _target_paths(tmp_path)
    old_bytes = {path: path.read_bytes() for path in targets}
    crashing_report = _report(slugs=("crash",), model="sensitive-crash-model")

    _run_crashing_save(tmp_path, crashing_report)

    assert targets[0].read_bytes() != old_bytes[targets[0]]
    journal = tmp_path / TRANSACTION_JOURNAL
    assert journal.is_file()
    assert "sensitive-crash-model" not in journal.read_text(encoding="utf-8")

    assert load_reports(tmp_path) == [old_report]
    assert {path: path.read_bytes() for path in targets} == old_bytes
    assert not _repository_artifacts(tmp_path)


def test_save_report_recovers_crash_before_starting_next_transaction(tmp_path: Path) -> None:
    save_report(tmp_path, _report(slugs=("old",), model="old-model"))
    _run_crashing_save(
        tmp_path,
        _report(slugs=("crash",), model="crash-model"),
    )
    final_report = _report(slugs=("final", "items"), model="final-model")

    save_report(tmp_path, final_report)

    assert load_reports(tmp_path) == [final_report]
    assert not _repository_artifacts(tmp_path)


@pytest.mark.parametrize(
    "journal_text",
    [
        "{not-json",
        json.dumps(
            {
                "version": "1.0",
                "transaction_id": "a" * 32,
                "entries": [
                    {
                        "target": "../../victim.txt",
                        "temporary": "../../victim.tmp",
                        "backup": "../../victim.bak",
                        "existed": True,
                    },
                    {
                        "target": "content/2026-07-26.md",
                        "temporary": "content/.2026-07-26.md." + "a" * 32 + ".tmp",
                        "backup": "content/.2026-07-26.md." + "a" * 32 + ".bak",
                        "existed": True,
                    },
                    {
                        "target": "data/index.json",
                        "temporary": "data/.index.json." + "a" * 32 + ".tmp",
                        "backup": "data/.index.json." + "a" * 32 + ".bak",
                        "existed": True,
                    },
                ],
            }
        ),
    ],
)
def test_corrupt_or_escaping_transaction_journal_is_rejected_without_writes(
    tmp_path: Path,
    journal_text: str,
) -> None:
    victim = tmp_path.parent / f"{tmp_path.name}-victim.txt"
    victim.write_bytes(b"untouched")
    (tmp_path / TRANSACTION_JOURNAL).write_text(journal_text, encoding="utf-8")

    with pytest.raises(ValueError):
        load_reports(tmp_path)

    assert victim.read_bytes() == b"untouched"


def test_cross_process_same_date_saves_are_serialized_and_consistent(
    tmp_path: Path,
) -> None:
    save_report(tmp_path, _report(slugs=("initial",), model="initial-model"))
    writer_a = _report(slugs=("writer-a",), model="writer-a-model")
    writer_b = _report(slugs=("writer-b", "second"), model="writer-b-model")
    context = multiprocessing.get_context("fork")
    first_replaced = context.Event()
    release_first = context.Event()
    first_finished = context.Event()
    second_finished = context.Event()
    process_a = context.Process(
        target=_coordinated_process_save,
        args=(
            str(tmp_path),
            writer_a.model_dump_json(),
            first_replaced,
            release_first,
            first_finished,
            True,
        ),
    )
    process_b = context.Process(
        target=_coordinated_process_save,
        args=(
            str(tmp_path),
            writer_b.model_dump_json(),
            first_replaced,
            release_first,
            second_finished,
            False,
        ),
    )

    process_a.start()
    assert first_replaced.wait(timeout=5)
    process_b.start()
    second_was_blocked = not second_finished.wait(timeout=0.4)
    release_first.set()
    _join_process(process_a)
    _join_process(process_b)

    assert second_was_blocked
    assert process_a.exitcode == 0
    assert process_b.exitcode == 0
    assert first_finished.is_set()
    assert second_finished.is_set()
    assert load_reports(tmp_path) == [writer_b]
    markdown = _target_paths(tmp_path)[1].read_text(encoding="utf-8")
    assert "writer\\-b\\-model" in markdown
    assert "writer\\-a\\-model" not in markdown
    assert not _repository_artifacts(tmp_path)


def test_cross_process_different_date_saves_do_not_lose_index_updates(
    tmp_path: Path,
) -> None:
    date_a = date(2026, 7, 26)
    date_b = date(2026, 7, 27)
    writer_a = _report(date_a, slugs=("writer-a",), model="writer-a-model")
    writer_b = _report(date_b, slugs=("writer-b",), model="writer-b-model")
    context = multiprocessing.get_context("fork")
    first_replaced = context.Event()
    release_first = context.Event()
    first_finished = context.Event()
    second_finished = context.Event()
    process_a = context.Process(
        target=_coordinated_process_save,
        args=(
            str(tmp_path),
            writer_a.model_dump_json(),
            first_replaced,
            release_first,
            first_finished,
            True,
        ),
    )
    process_b = context.Process(
        target=_coordinated_process_save,
        args=(
            str(tmp_path),
            writer_b.model_dump_json(),
            first_replaced,
            release_first,
            second_finished,
            False,
        ),
    )

    process_a.start()
    assert first_replaced.wait(timeout=5)
    process_b.start()
    second_was_blocked = not second_finished.wait(timeout=0.4)
    release_first.set()
    _join_process(process_a)
    _join_process(process_b)

    assert second_was_blocked
    assert process_a.exitcode == 0
    assert process_b.exitcode == 0
    assert load_reports(tmp_path) == [writer_b, writer_a]
    assert not _repository_artifacts(tmp_path)


def test_same_process_threads_are_serialized_before_recovery_or_index_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_report(tmp_path, _report(slugs=("initial",), model="initial-model"))
    writer_a = _report(slugs=("thread-a",), model="thread-a-model")
    writer_b = _report(slugs=("thread-b",), model="thread-b-model")
    report_path = _target_paths(tmp_path)[0]
    original_replace = Path.replace
    first_replaced = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    errors: list[BaseException] = []

    def pause_first_thread(self: Path, target: Path) -> Path:
        result = original_replace(self, target)
        if threading.current_thread().name == "writer-a" and target == report_path:
            first_replaced.set()
            if not release_first.wait(timeout=10):
                raise TimeoutError("writer-a was not released")
        return result

    def run_save(report: DailyReport, finished: threading.Event | None = None) -> None:
        try:
            save_report(tmp_path, report)
        except BaseException as error:
            errors.append(error)
        finally:
            if finished is not None:
                finished.set()

    monkeypatch.setattr(Path, "replace", pause_first_thread)
    thread_a = threading.Thread(target=run_save, args=(writer_a,), name="writer-a")
    thread_b = threading.Thread(
        target=run_save,
        args=(writer_b, second_finished),
        name="writer-b",
    )

    thread_a.start()
    assert first_replaced.wait(timeout=5)
    thread_b.start()
    second_was_blocked = not second_finished.wait(timeout=0.4)
    release_first.set()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert second_was_blocked
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert errors == []
    assert load_reports(tmp_path) == [writer_b]


def test_rollback_failure_preserves_journal_and_backups_for_next_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_report = _report(slugs=("old",), model="old-model")
    save_report(tmp_path, old_report)
    targets = _target_paths(tmp_path)
    old_bytes = {path: path.read_bytes() for path in targets}
    original_replace = Path.replace
    commit_count = 0
    restore_failed = False

    def fail_commit_then_restore(self: Path, target: Path) -> Path:
        nonlocal commit_count, restore_failed
        if self.name.endswith(".recover.tmp") and not restore_failed:
            restore_failed = True
            raise OSError("simulated restore failure")
        if target in targets and not self.name.endswith(".recover.tmp"):
            commit_count += 1
            if commit_count == 2:
                raise OSError("simulated second commit failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_commit_then_restore)

    with pytest.raises(RuntimeError, match="rollback failed"):
        save_report(tmp_path, _report(slugs=("new",), model="new-model"))

    assert (tmp_path / TRANSACTION_JOURNAL).is_file()
    assert any(path.name.endswith(".bak") for path in _repository_artifacts(tmp_path))
    monkeypatch.setattr(Path, "replace", original_replace)

    assert load_reports(tmp_path) == [old_report]
    assert {path: path.read_bytes() for path in targets} == old_bytes
    assert not _repository_artifacts(tmp_path)


def test_first_save_fsyncs_created_parent_chain_and_cleanup_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = storage_repository.os.open
    original_fsync = storage_repository.os.fsync
    directory_descriptors: dict[int, Path] = {}
    synced_directories: list[Path] = []

    def tracking_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        descriptor = original_open(path, flags, mode)
        if Path(path).is_dir():
            directory_descriptors[descriptor] = Path(path).resolve()
        return descriptor

    def tracking_fsync(descriptor: int) -> None:
        directory = directory_descriptors.get(descriptor)
        if directory is not None:
            synced_directories.append(directory)
        original_fsync(descriptor)

    monkeypatch.setattr(storage_repository.os, "open", tracking_open)
    monkeypatch.setattr(storage_repository.os, "fsync", tracking_fsync)

    save_report(tmp_path, _report(slugs=("fsync",)))

    root = tmp_path.resolve()
    report_parent = root / "data/2026/07"
    content_parent = root / "content"
    expected_parent_chain = {
        root,
        root / "data",
        root / "data/2026",
        report_parent,
        content_parent,
    }
    assert expected_parent_chain <= set(synced_directories)
    assert synced_directories.count(root) >= 2
    assert synced_directories.count(report_parent) >= 3
    assert synced_directories.count(content_parent) >= 3
    assert not _repository_artifacts(tmp_path)


def test_next_load_cleans_pre_journal_orphan_after_process_crash(
    tmp_path: Path,
) -> None:
    old_report = _report(slugs=("old",), model="old-model")
    save_report(tmp_path, old_report)
    targets = _target_paths(tmp_path)
    old_bytes = {path: path.read_bytes() for path in targets}

    _run_orphan_window_crash(
        tmp_path,
        _report(slugs=("pre-journal",), model="pre-journal-model"),
        "pre-journal",
    )

    assert not (tmp_path / TRANSACTION_JOURNAL).exists()
    assert _repository_artifacts(tmp_path)
    assert {path: path.read_bytes() for path in targets} == old_bytes

    assert load_reports(tmp_path) == [old_report]
    assert {path: path.read_bytes() for path in targets} == old_bytes
    assert not _repository_artifacts(tmp_path)


def test_next_load_cleans_post_commit_orphans_and_keeps_new_state(
    tmp_path: Path,
) -> None:
    save_report(tmp_path, _report(slugs=("old",), model="old-model"))
    new_report = _report(slugs=("committed", "items"), model="committed-model")

    _run_orphan_window_crash(tmp_path, new_report, "post-commit")

    report_path, markdown_path, index_path = _target_paths(tmp_path)
    assert not (tmp_path / TRANSACTION_JOURNAL).exists()
    assert _repository_artifacts(tmp_path)
    assert DailyReport.model_validate_json(report_path.read_bytes()) == new_report
    assert "committed\\-model" in markdown_path.read_text(encoding="utf-8")
    assert json.loads(index_path.read_text(encoding="utf-8"))["reports"][0]["item_count"] == 2

    assert load_reports(tmp_path) == [new_report]
    assert not _repository_artifacts(tmp_path)


def test_orphan_sweep_preserves_similar_but_invalid_regular_files(tmp_path: Path) -> None:
    save_report(tmp_path, _report(slugs=("safe",)))
    transaction_id = "b" * 32
    files = {
        tmp_path / f"content/.2026-7-26.md.{transaction_id}.tmp": b"short date",
        tmp_path / f"content/.2026-07-26.txt.{transaction_id}.bak": b"wrong target",
        tmp_path / ("data/.index.json." + "g" * 32 + ".tmp"): b"invalid uuid",
        tmp_path / f"data/2026/07/.2026-08-01.json.{transaction_id}.tmp": b"path mismatch",
        tmp_path / f"..ai-news-transaction.json.{transaction_id[:-1]}.tmp": b"short uuid",
    }
    for path, content in files.items():
        path.write_bytes(content)

    load_reports(tmp_path)

    assert {path: path.read_bytes() for path in files} == files


def test_orphan_sweep_rejects_matching_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    save_report(tmp_path, _report(slugs=("safe",)))
    outside = tmp_path.parent / f"{tmp_path.name}-orphan-sentinel"
    outside.write_bytes(b"untouched")
    disguised = tmp_path / "content" / f".2026-07-26.md.{'c' * 32}.bak"
    disguised.symlink_to(outside)

    with pytest.raises(ValueError, match="artifact"):
        load_reports(tmp_path)

    assert disguised.is_symlink()
    assert outside.read_bytes() == b"untouched"
