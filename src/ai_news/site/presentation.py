from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

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


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    slug: str
    name: str


CHANNELS = (
    ChannelSpec("model", "大模型"),
    ChannelSpec("agent", "Agent"),
    ChannelSpec("ai-tools", "AI 产品"),
    ChannelSpec("open-source", "开源生态"),
    ChannelSpec("industry-policy", "行业政策"),
)
_CHANNEL_BY_SLUG = {channel.slug: channel for channel in CHANNELS}
_CATEGORY_CHANNEL = {
    Category.MODEL: "model",
    Category.AGENT: "agent",
    Category.CODING_AGENT: "agent",
    Category.TOOL: "ai-tools",
    Category.OPEN_SOURCE: "open-source",
}


@dataclass(frozen=True, slots=True)
class EditionNeighbors:
    older: DailyReport | None
    newer: DailyReport | None


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    date: date
    status: Literal[
        "published",
        "no_eligible_content",
        "failed_before_publish",
        "never_run",
    ]
    report: DailyReport | None
    run_record: RunRecord | None
    latest_run_status: RunStatus | None


@dataclass(frozen=True, slots=True)
class SourceStatus:
    source_id: str
    source_name: str
    source_role: SourceRole
    request_status: Literal["ok", "failed", "never_run"]
    item_count: int | None
    latest_published_at: datetime | None
    recent_count: int | None
    new_count: int | None
    eligible_count: int | None
    candidate_count: int | None
    selected_count: int | None
    diagnostic_label: str | None


def channel_for_item(item: NewsItem) -> ChannelSpec | None:
    if item.editorial_lane is EditorialLane.PLATFORM_POLICY:
        return _CHANNEL_BY_SLUG["industry-policy"]
    if item.category is Category.RESEARCH:
        return None
    slug = _CATEGORY_CHANNEL.get(item.category)
    return _CHANNEL_BY_SLUG[slug] if slug is not None else None


def item_fragment(item: NewsItem, report_date: date) -> str:
    return f"item-{item.id}-{report_date.isoformat()}"


def item_day_url(item: NewsItem, report_date: date, base_path: str) -> str:
    if not base_path.startswith("/") or not base_path.endswith("/"):
        raise ValueError("base_path must have boundary slashes")
    return f"{base_path}days/{report_date.isoformat()}/#{item_fragment(item, report_date)}"


def issue_numbers(reports: Sequence[DailyReport]) -> dict[date, int]:
    ordered_dates = sorted({report.date for report in reports})
    return {report_date: index for index, report_date in enumerate(ordered_dates, start=1)}


def paginate[T](values: Sequence[T], page_size: int = 20) -> list[list[T]]:
    if isinstance(page_size, bool) or page_size < 1:
        raise ValueError("page_size must be at least one")
    pages = [list(values[index : index + page_size]) for index in range(0, len(values), page_size)]
    return pages or [[]]


def edition_neighbors(
    reports: Sequence[DailyReport],
    report_date: date,
) -> EditionNeighbors:
    ordered = sorted(reports, key=lambda report: report.date)
    positions = {report.date: index for index, report in enumerate(ordered)}
    if report_date not in positions:
        raise ValueError("report date is not present")
    index = positions[report_date]
    return EditionNeighbors(
        older=ordered[index - 1] if index > 0 else None,
        newer=ordered[index + 1] if index + 1 < len(ordered) else None,
    )


def archive_entries(
    reports: Sequence[DailyReport],
    run_records: Sequence[RunRecord],
) -> list[ArchiveEntry]:
    report_by_date = {report.date: report for report in reports}
    record_by_date = {record.date: record for record in run_records}
    if len(report_by_date) != len(reports) or len(record_by_date) != len(run_records):
        raise ValueError("archive inputs must contain unique dates")
    represented_dates = set(report_by_date) | set(record_by_date)
    if not represented_dates:
        return []

    newest = max(represented_dates)
    oldest = min(represented_dates)
    entries: list[ArchiveEntry] = []
    current = newest
    while current >= oldest:
        report = report_by_date.get(current)
        record = record_by_date.get(current)
        latest_status = record.status if record is not None else None
        if record is not None and record.status is RunStatus.NO_ELIGIBLE_CONTENT:
            status = "no_eligible_content"
            active_report = None
        elif report is not None:
            status = "published"
            active_report = report
        elif record is not None and record.status is RunStatus.FAILED_BEFORE_PUBLISH:
            status = "failed_before_publish"
            active_report = None
        elif record is not None and record.status is RunStatus.PUBLISHED:
            raise ValueError("published run record is missing its report")
        else:
            status = "never_run"
            active_report = None
        entries.append(
            ArchiveEntry(
                date=current,
                status=status,
                report=active_report,
                run_record=record,
                latest_run_status=latest_status,
            )
        )
        current -= timedelta(days=1)
    return entries


def _sum_known(source_runs: Sequence[SourceRun], field_name: str) -> int | None:
    values = [getattr(source_run, field_name) for source_run in source_runs]
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def source_statuses(
    source_specs: Sequence[SourceSpec],
    run_records: Sequence[RunRecord],
) -> list[SourceStatus]:
    selected_dates = set(sorted({record.date for record in run_records}, reverse=True)[:7])
    selected_records = [record for record in run_records if record.date in selected_dates]
    statuses: list[SourceStatus] = []
    for source in source_specs:
        if not source.enabled:
            continue
        dated_runs = [
            (record.date, source_run)
            for record in selected_records
            for source_run in record.source_runs
            if source_run.source_id == source.id
        ]
        dated_runs.sort(key=lambda value: value[0], reverse=True)
        runs = [source_run for _run_date, source_run in dated_runs]
        if not runs:
            statuses.append(
                SourceStatus(
                    source_id=source.id,
                    source_name=source.name,
                    source_role=source.role,
                    request_status="never_run",
                    item_count=None,
                    latest_published_at=None,
                    recent_count=None,
                    new_count=None,
                    eligible_count=None,
                    candidate_count=None,
                    selected_count=None,
                    diagnostic_label="暂无运行数据",
                )
            )
            continue

        latest_run = runs[0]
        latest_dates = [
            source_run.latest_published_at
            for source_run in runs
            if source_run.latest_published_at is not None
        ]
        counts = {
            field_name: _sum_known(runs, field_name)
            for field_name in (
                "recent_count",
                "new_count",
                "eligible_count",
                "candidate_count",
                "selected_count",
            )
        }
        diagnostics_available = any(value is not None for value in counts.values())
        statuses.append(
            SourceStatus(
                source_id=source.id,
                source_name=source.name,
                source_role=source.role,
                request_status="ok" if latest_run.success else "failed",
                item_count=latest_run.item_count,
                latest_published_at=max(latest_dates) if latest_dates else None,
                recent_count=counts["recent_count"],
                new_count=counts["new_count"],
                eligible_count=counts["eligible_count"],
                candidate_count=counts["candidate_count"],
                selected_count=counts["selected_count"],
                diagnostic_label=None if diagnostics_available else "历史诊断不可用",
            )
        )
    return statuses


__all__ = [
    "CHANNELS",
    "ArchiveEntry",
    "ChannelSpec",
    "EditionNeighbors",
    "SourceStatus",
    "archive_entries",
    "channel_for_item",
    "edition_neighbors",
    "issue_numbers",
    "item_day_url",
    "item_fragment",
    "paginate",
    "source_statuses",
]
