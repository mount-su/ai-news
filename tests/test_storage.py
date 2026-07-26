# ruff: noqa: RUF001

from __future__ import annotations

import json
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
    SourceRun,
)
from ai_news.pipeline.normalize import to_candidate
from ai_news.storage import load_history_urls, load_reports, save_report

RUN_DATE = date(2026, 7, 26)
GENERATED_AT = datetime(2026, 7, 26, 10, 30, tzinfo=UTC)


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


def _repository_artifacts(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and (path.name.endswith(".tmp") or path.name.endswith(".bak"))
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
        "published_at": "2026-07-26T08:30:00Z",
        "title": analysis.title,
        "category": Category.MODEL.value,
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
        "item_count": 0,
        "elapsed_ms": 12,
        "error_type": "RuntimeError",
    }
    assert "do-not-copy" not in failure.model_dump_json()
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
        if self.name.endswith(".tmp"):
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
        if self.name.endswith(".tmp"):
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
