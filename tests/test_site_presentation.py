# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

from ai_news.models import (
    Category,
    DailyReport,
    EditorialLane,
    NewsItem,
    RunRecord,
    RunStatus,
    SourceRole,
    SourceRun,
    SourceSpec,
)
from ai_news.site.presentation import (
    CHANNELS,
    archive_entries,
    channel_for_item,
    edition_neighbors,
    issue_numbers,
    item_day_url,
    item_fragment,
    paginate,
    source_statuses,
)


def _item(
    slug: str,
    *,
    category: Category,
    lane: EditorialLane | None = None,
) -> NewsItem:
    canonical_url = f"https://example.com/{slug}"
    values = {
        "id": hashlib.sha256(canonical_url.encode()).hexdigest()[:16],
        "canonical_url": canonical_url,
        "original_title": f"Original {slug}",
        "source": "Official Source",
        "published_at": datetime(2026, 8, 30, 8, tzinfo=UTC),
        "title": f"标题 {slug}",
        "category": category,
        "summary": f"这是关于 {slug} 的事实摘要，长度足够用于展示层测试。",
        "importance": 8,
        "why_it_matters": f"{slug} 会影响产品选择和后续行业采用。",
        "tags": ["AI", slug],
        "is_official": True,
        "marketing_risk": "low",
        "tracking_signal": f"继续跟踪 {slug} 的正式更新。",
    }
    if lane is not None:
        values["editorial_lane"] = lane
    return NewsItem.model_validate(values)


def _report(run_date: date) -> DailyReport:
    return DailyReport(
        date=run_date,
        generated_at=datetime.combine(run_date, datetime.min.time(), tzinfo=UTC),
        model="test-model",
        degraded=False,
        candidate_count=0,
        items=[],
        source_runs=[],
    )


def _record(
    run_date: date,
    *,
    status: RunStatus,
    source_runs: list[SourceRun] | None = None,
) -> RunRecord:
    safe_error_code = {
        RunStatus.PUBLISHED: "none",
        RunStatus.NO_ELIGIBLE_CONTENT: "no_eligible_candidates",
        RunStatus.FAILED_BEFORE_PUBLISH: "analysis_failed",
    }[status]
    return RunRecord(
        date=run_date,
        cutoff_at=datetime.combine(run_date, datetime.max.time(), tzinfo=UTC),
        status=status,
        source_runs=source_runs or [],
        safe_error_code=safe_error_code,
    )


def test_fixed_channels_keep_order_and_map_policy_before_category() -> None:
    assert [(channel.slug, channel.name) for channel in CHANNELS] == [
        ("model", "大模型"),
        ("agent", "Agent"),
        ("ai-tools", "AI 产品"),
        ("open-source", "开源生态"),
        ("industry-policy", "行业政策"),
    ]

    assert channel_for_item(_item("model", category=Category.MODEL)).slug == "model"
    assert channel_for_item(_item("agent", category=Category.AGENT)).slug == "agent"
    assert channel_for_item(_item("coding", category=Category.CODING_AGENT)).slug == "agent"
    assert channel_for_item(_item("tool", category=Category.TOOL)).slug == "ai-tools"
    assert channel_for_item(_item("open", category=Category.OPEN_SOURCE)).slug == "open-source"
    assert channel_for_item(_item("research", category=Category.RESEARCH)) is None
    assert (
        channel_for_item(
            _item(
                "policy-research",
                category=Category.RESEARCH,
                lane=EditorialLane.PLATFORM_POLICY,
            )
        ).slug
        == "industry-policy"
    )


def test_item_fragments_and_day_urls_are_stable_and_report_specific() -> None:
    item = _item("stable", category=Category.MODEL)
    first_date = date(2026, 8, 29)
    second_date = date(2026, 8, 30)

    first_fragment = item_fragment(item, first_date)
    second_fragment = item_fragment(item, second_date)

    assert first_fragment == f"item-{item.id}-2026-08-29"
    assert second_fragment == f"item-{item.id}-2026-08-30"
    assert first_fragment != second_fragment
    assert item_day_url(item, first_date, "/ai-news/") == (
        f"/ai-news/days/2026-08-29/#{first_fragment}"
    )


def test_issue_numbers_are_continuous_in_chronological_order() -> None:
    reports = [
        _report(date(2026, 8, 30)),
        _report(date(2026, 8, 28)),
        _report(date(2026, 8, 29)),
    ]

    assert issue_numbers(reports) == {
        date(2026, 8, 28): 1,
        date(2026, 8, 29): 2,
        date(2026, 8, 30): 3,
    }


def test_paginate_always_returns_a_first_page_and_never_loses_entries() -> None:
    assert paginate([]) == [[]]
    assert [len(page) for page in paginate(list(range(20)))] == [20]
    assert [len(page) for page in paginate(list(range(21)))] == [20, 1]
    pages = paginate(list(range(41)))
    assert [len(page) for page in pages] == [20, 20, 1]
    assert [value for page in pages for value in page] == list(range(41))


def test_edition_neighbors_handle_middle_and_boundaries() -> None:
    reports = [
        _report(date(2026, 8, 30)),
        _report(date(2026, 8, 28)),
        _report(date(2026, 8, 29)),
    ]

    middle = edition_neighbors(reports, date(2026, 8, 29))
    oldest = edition_neighbors(reports, date(2026, 8, 28))
    newest = edition_neighbors(reports, date(2026, 8, 30))

    assert middle.older.date == date(2026, 8, 28)
    assert middle.newer.date == date(2026, 8, 30)
    assert oldest.older is None and oldest.newer.date == date(2026, 8, 29)
    assert newest.older.date == date(2026, 8, 29) and newest.newer is None


def test_archive_entries_fill_date_gaps_and_apply_run_record_authority() -> None:
    old_report = _report(date(2026, 8, 27))
    shadowed_report = _report(date(2026, 8, 29))
    failed_retry_report = _report(date(2026, 8, 30))
    entries = archive_entries(
        [failed_retry_report, shadowed_report, old_report],
        [
            _record(date(2026, 8, 30), status=RunStatus.FAILED_BEFORE_PUBLISH),
            _record(date(2026, 8, 29), status=RunStatus.NO_ELIGIBLE_CONTENT),
            _record(date(2026, 8, 28), status=RunStatus.FAILED_BEFORE_PUBLISH),
        ],
    )

    assert [(entry.date, entry.status) for entry in entries] == [
        (date(2026, 8, 30), "published"),
        (date(2026, 8, 29), "no_eligible_content"),
        (date(2026, 8, 28), "failed_before_publish"),
        (date(2026, 8, 27), "published"),
    ]
    assert entries[0].latest_run_status is RunStatus.FAILED_BEFORE_PUBLISH
    assert entries[1].report is None

    missing = archive_entries(
        [_report(date(2026, 8, 30)), _report(date(2026, 8, 28))],
        [],
    )
    assert [(entry.date, entry.status) for entry in missing] == [
        (date(2026, 8, 30), "published"),
        (date(2026, 8, 29), "never_run"),
        (date(2026, 8, 28), "published"),
    ]


def test_source_statuses_use_latest_seven_run_dates_and_keep_missing_sources() -> None:
    official = SourceSpec(
        id="official",
        name="Official",
        kind="feed",
        url="https://official.example/feed.xml",
        category=Category.MODEL,
        role=SourceRole.PRIMARY,
    )
    media = SourceSpec(
        id="media",
        name="Media",
        kind="feed",
        url="https://media.example/feed.xml",
        category=Category.TOOL,
        official=False,
        role=SourceRole.TRUSTED_MEDIA,
    )
    missing = SourceSpec(
        id="missing",
        name="Missing",
        kind="feed",
        url="https://missing.example/feed.xml",
        category=Category.AGENT,
        role=SourceRole.PRIMARY,
    )
    records: list[RunRecord] = []
    for age in range(8):
        run_date = date(2026, 8, 30) - timedelta(days=age)
        source_run = SourceRun.succeeded(
            source_id="official",
            source_name="Official",
            item_count=age + 1,
            elapsed_ms=10,
        ).with_diagnostics(
            recent_count=1,
            new_count=2,
            eligible_count=3,
            candidate_count=4,
            selected_count=1,
            latest_published_at=datetime.combine(run_date, datetime.min.time(), tzinfo=UTC),
        )
        media_run = SourceRun.succeeded(
            source_id="media",
            source_name="Media",
            item_count=1,
            elapsed_ms=10,
        )
        records.append(
            _record(
                run_date,
                status=RunStatus.PUBLISHED,
                source_runs=[source_run, media_run],
            )
        )

    statuses = source_statuses([official, media, missing], list(reversed(records)))
    by_id = {status.source_id: status for status in statuses}

    assert [status.source_id for status in statuses] == ["official", "media", "missing"]
    assert by_id["official"].recent_count == 7
    assert by_id["official"].new_count == 14
    assert by_id["official"].eligible_count == 21
    assert by_id["official"].candidate_count == 28
    assert by_id["official"].selected_count == 7
    assert by_id["official"].latest_published_at == datetime(2026, 8, 30, tzinfo=UTC)
    assert by_id["media"].diagnostic_label == "历史诊断不可用"
    assert by_id["media"].recent_count is None
    assert by_id["missing"].request_status == "never_run"
    assert by_id["missing"].diagnostic_label == "暂无运行数据"
