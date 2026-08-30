from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from ai_news.config import load_source_config
from ai_news.models import (
    Category,
    DailyReport,
    NewsItem,
    RunRecord,
    RunStatus,
    SourceRole,
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
from ai_news.storage import load_reports, load_run_records

_SITE_DIRECTORY = Path(__file__).parent
_TEMPLATE_DIRECTORY = _SITE_DIRECTORY / "templates"
_STATIC_DIRECTORY = _SITE_DIRECTORY / "static"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RESERVED_ROOT_DIRECTORIES = ("data", "content", "sources")


def _display_category(category: Category) -> Category:
    """Collapse the legacy Coding Agent label into the Agent section."""
    return Category.AGENT if category is Category.CODING_AGENT else category


def _validate_base_path(base_path: str) -> str:
    if not isinstance(base_path, str):
        raise ValueError("base_path must be a string")
    parsed = urlsplit(base_path)
    decoded_path = unquote(base_path)
    if (
        not base_path
        or "\\" in decoded_path
        or not base_path.startswith("/")
        or not base_path.endswith("/")
        or decoded_path.startswith("//")
        or "//" in decoded_path[1:]
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.path != base_path
        or any(part in {".", ".."} for part in decoded_path.split("/"))
    ):
        raise ValueError("base_path must be one absolute site path with boundary slashes")
    return base_path


def _asset_url(base_path: str, asset: str) -> str:
    clean_asset = asset.strip("/")
    if not clean_asset or any(part in {".", ".."} for part in clean_asset.split("/")):
        raise ValueError("asset path is invalid")
    return f"{base_path}assets/{clean_asset}"


def _page_url(base_path: str, page: str = "") -> str:
    clean_page = page.strip("/")
    if not clean_page:
        return base_path
    if any(part in {".", ".."} for part in clean_page.split("/")):
        raise ValueError("page path is invalid")
    return f"{base_path}{clean_page}/"


def _resolved(path: Path, *, label: str) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{label} cannot be resolved safely") from error


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _validate_output(root: Path, output: Path) -> tuple[Path, Path]:
    root_resolved = _resolved(root, label="root")
    output_input = output.expanduser().absolute()
    if output_input.is_symlink():
        raise ValueError("output must not be a symlink")
    output_resolved = _resolved(output_input, label="output")
    home_resolved = _resolved(Path.home(), label="home")
    filesystem_root = Path(output_resolved.anchor)

    if (
        output_resolved in (filesystem_root, root_resolved, home_resolved)
        or root_resolved.is_relative_to(output_resolved)
        or home_resolved.is_relative_to(output_resolved)
    ):
        raise ValueError("output path is unsafe")
    for directory_name in _RESERVED_ROOT_DIRECTORIES:
        reserved_path = root_resolved / directory_name
        reserved_boundaries = {
            reserved_path,
            _resolved(reserved_path, label="reserved storage path"),
        }
        if any(
            _paths_overlap(output_resolved, reserved_boundary)
            for reserved_boundary in reserved_boundaries
        ):
            raise ValueError("output overlaps a reserved storage path")
    output_parent = output_resolved.parent
    if output_parent.resolve(strict=False) != output_parent:
        raise ValueError("resolved output parent is unsafe")
    if output_parent.exists() and not output_parent.is_dir():
        raise ValueError("resolved output parent must be a directory")
    if output_resolved.is_symlink():
        raise ValueError("resolved output must not be a symlink")
    if output_resolved.exists() and not output_resolved.is_dir():
        raise ValueError("output must be a directory")
    return root_resolved, output_resolved


def _sort_items(items: Iterable[NewsItem]) -> list[NewsItem]:
    return sorted(
        items,
        key=lambda item: (
            -item.importance,
            -item.published_at.timestamp(),
            item.id,
        ),
    )


def _sort_category_entries(
    entries: Iterable[tuple[DailyReport, NewsItem]],
) -> list[tuple[DailyReport, NewsItem]]:
    return sorted(
        entries,
        key=lambda entry: (
            -entry[1].published_at.timestamp(),
            entry[1].id,
            -entry[0].date.toordinal(),
        ),
    )


def _beijing_datetime(value: datetime) -> str:
    return f"{value.astimezone(_SHANGHAI):%Y-%m-%d %H:%M} 北京时间"


def _is_github(item: NewsItem) -> bool:
    return (urlsplit(str(item.canonical_url)).hostname or "").lower() == "github.com"


def _environment(base_path: str) -> Environment:
    environment = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIRECTORY),
        autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    environment.globals.update(
        asset_url=lambda asset: _asset_url(base_path, asset),
        page_url=lambda page="": _page_url(base_path, page),
    )
    environment.filters["beijing_datetime"] = _beijing_datetime
    environment.filters["github_url"] = _is_github
    environment.filters["display_category"] = lambda category: _display_category(
        Category(category)
    ).value
    return environment


def _category_navigation() -> list[dict[str, str]]:
    return [{"name": channel.name, "slug": channel.slug} for channel in CHANNELS]


def _report_context(report: DailyReport, issue_number: int | None = None) -> dict[str, object]:
    return {
        "date": report.date.isoformat(),
        "generated_at": report.generated_at,
        "issue_number": issue_number,
        "source_count": len(report.source_runs),
        "healthy_source_count": sum(run.success for run in report.source_runs),
        "candidate_count": report.candidate_count,
        "included_count": len(report.items),
    }


def _story_entries(
    report: DailyReport,
    items: Iterable[NewsItem],
    base_path: str,
    *,
    start: int = 1,
) -> list[dict[str, object]]:
    return [
        {
            "item": item,
            "number": f"{index:02d}",
            "report_date": report.date.isoformat(),
            "fragment": item_fragment(item, report.date),
            "day_url": item_day_url(item, report.date, base_path),
        }
        for index, item in enumerate(items, start=start)
    ]


def _fallback_source_specs(
    reports: Iterable[DailyReport],
    run_records: Iterable[RunRecord],
) -> list[SourceSpec]:
    names: dict[str, str] = {}
    for source_run in (
        source_run for container in (*reports, *run_records) for source_run in container.source_runs
    ):
        names.setdefault(source_run.source_id, source_run.source_name)
    return [
        SourceSpec(
            id=source_id,
            name=source_name,
            kind="feed",
            url=f"https://example.invalid/{source_id}.xml",
            category=Category.MODEL,
            official=False,
            role=SourceRole.TRUSTED_MEDIA,
        )
        for source_id, source_name in sorted(names.items())
    ]


def _load_source_specs(
    root: Path,
    reports: list[DailyReport],
    run_records: list[RunRecord],
) -> list[SourceSpec]:
    try:
        return load_source_config(root / "sources/feeds.yaml").sources
    except FileNotFoundError:
        return _fallback_source_specs(reports, run_records)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _render(
    environment: Environment,
    staging: Path,
    template_name: str,
    relative_path: str,
    context: dict[str, object],
) -> None:
    rendered = environment.get_template(template_name).render(**context)
    _write_text(staging / relative_path, rendered)


def _public_search_entry(
    item: NewsItem,
    report: DailyReport,
    base_path: str,
) -> dict[str, object]:
    channel = channel_for_item(item)
    if channel is None:
        raise ValueError("excluded items cannot enter public search")
    return {
        "id": item.id,
        "title": item.title,
        "summary": item.summary,
        "why_it_matters": item.why_it_matters,
        "tags": item.tags,
        "display_category": channel.name,
        "source": item.source,
        "published_at": item.published_at.isoformat(),
        "report_date": report.date.isoformat(),
        "is_official": item.is_official,
        "marketing_risk": item.marketing_risk,
        "page_url": item_day_url(item, report.date, base_path),
    }


def _build_staging(
    staging: Path,
    reports: list[DailyReport],
    archive_reports: list[DailyReport],
    run_records: list[RunRecord],
    source_specs: list[SourceSpec],
    base_path: str,
) -> None:
    environment = _environment(base_path)
    latest = reports[0]
    issue_by_date = issue_numbers(reports)
    all_pairs = [(report, item) for report in reports for item in report.items]
    categories = _category_navigation()
    common: dict[str, object] = {
        "site_name": "AI 情报雷达",
        "latest_report": _report_context(latest, issue_by_date[latest.date]),
        "categories": categories,
    }

    shutil.copytree(_STATIC_DIRECTORY, staging / "assets")

    latest_items = _sort_items(latest.items)
    latest_entries = _story_entries(latest, latest_items, base_path)
    _render(
        environment,
        staging,
        "index.html",
        "index.html",
        {
            **common,
            "page_title": "今日情报",
            "current_page": "home",
            "report": _report_context(latest, issue_by_date[latest.date]),
            "items": latest_items,
            "entries": latest_entries,
            "lead": latest_entries[0] if latest_entries else None,
            "briefs": latest_entries[1:4],
            "remaining_entries": latest_entries[4:],
        },
    )

    for report in reports:
        report_items = _sort_items(report.items)
        report_entries = _story_entries(report, report_items, base_path)
        neighbors = edition_neighbors(reports, report.date)
        _render(
            environment,
            staging,
            "day.html",
            f"days/{report.date.isoformat()}/index.html",
            {
                **common,
                "page_title": f"{report.date.isoformat()} 情报",
                "current_page": "archive",
                "report": _report_context(report, issue_by_date[report.date]),
                "items": report_items,
                "entries": report_entries,
                "older_report": (
                    _report_context(neighbors.older, issue_by_date[neighbors.older.date])
                    if neighbors.older is not None
                    else None
                ),
                "newer_report": (
                    _report_context(neighbors.newer, issue_by_date[neighbors.newer.date])
                    if neighbors.newer is not None
                    else None
                ),
            },
        )

    archive_rows = archive_entries(archive_reports, run_records)
    _render(
        environment,
        staging,
        "archive.html",
        "archive/index.html",
        {
            **common,
            "page_title": "日期归档",
            "current_page": "archive",
            "entries": archive_rows,
            "reports": [
                _report_context(report, issue_by_date.get(report.date)) for report in reports
            ],
        },
    )

    channel_items: dict[str, list[tuple[DailyReport, NewsItem]]] = {
        channel.slug: [] for channel in CHANNELS
    }
    for report, item in all_pairs:
        channel = channel_for_item(item)
        if channel is not None:
            channel_items[channel.slug].append((report, item))
    for channel in CHANNELS:
        ordered_pairs = _sort_category_entries(channel_items[channel.slug])
        pair_pages = paginate(ordered_pairs)
        total_pages = len(pair_pages)
        for page_number, page_pairs in enumerate(pair_pages, start=1):
            ordered_entries = [
                {
                    "report_date": report.date.isoformat(),
                    "item": item,
                    "number": f"{index:02d}",
                    "fragment": item_fragment(item, report.date),
                    "day_url": item_day_url(item, report.date, base_path),
                }
                for index, (report, item) in enumerate(
                    page_pairs,
                    start=(page_number - 1) * 20 + 1,
                )
            ]
            relative_path = (
                f"categories/{channel.slug}/index.html"
                if page_number == 1
                else f"categories/{channel.slug}/page/{page_number}/index.html"
            )
            _render(
                environment,
                staging,
                "category.html",
                relative_path,
                {
                    **common,
                    "page_title": f"{channel.name} 情报",
                    "current_page": channel.slug,
                    "category_name": channel.name,
                    "entries": ordered_entries,
                    "pagination": {
                        "current": page_number,
                        "total": total_pages,
                        "previous_url": (
                            _page_url(
                                base_path,
                                f"categories/{channel.slug}"
                                if page_number == 2
                                else f"categories/{channel.slug}/page/{page_number - 1}",
                            )
                            if page_number > 1
                            else None
                        ),
                        "next_url": (
                            _page_url(
                                base_path,
                                f"categories/{channel.slug}/page/{page_number + 1}",
                            )
                            if page_number < total_pages
                            else None
                        ),
                    },
                },
            )

    _render(
        environment,
        staging,
        "search.html",
        "search/index.html",
        {
            **common,
            "page_title": "搜索",
            "current_page": "search",
        },
    )
    _render(
        environment,
        staging,
        "sources.html",
        "sources/index.html",
        {
            **common,
            "page_title": "来源状态",
            "current_page": "sources",
            "source_statuses": source_statuses(source_specs, run_records),
        },
    )

    search_cutoff = latest.date - timedelta(days=179)
    seen_item_ids: set[str] = set()
    search_pairs: list[tuple[DailyReport, NewsItem]] = []
    for report in reports:
        if report.date < search_cutoff:
            continue
        for item in report.items:
            if channel_for_item(item) is None or item.id in seen_item_ids:
                continue
            seen_item_ids.add(item.id)
            search_pairs.append((report, item))
    search_pairs.sort(
        key=lambda pair: (
            -pair[1].published_at.timestamp(),
            -pair[0].date.toordinal(),
            pair[1].id,
        )
    )
    search_payload = [
        _public_search_entry(item, report, base_path) for report, item in search_pairs
    ]
    _write_text(
        staging / "search.json",
        json.dumps(search_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _validate_staging(staging: Path, reports: list[DailyReport]) -> None:
    required = {
        staging / "index.html",
        staging / "archive/index.html",
        staging / "search/index.html",
        staging / "sources/index.html",
        staging / "assets/styles.css",
        staging / "assets/app.js",
        staging / "search.json",
        *(staging / f"days/{report.date.isoformat()}/index.html" for report in reports),
        *(staging / f"categories/{channel.slug}/index.html" for channel in CHANNELS),
    }
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("site staging validation failed")
    for path in staging.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("site staging must not contain symlinks")


def _remove_owned_directory(path: Path, parent: Path, prefix: str) -> None:
    if not path.exists():
        return
    resolved = path.resolve(strict=False)
    if (
        path.is_symlink()
        or resolved.parent != parent
        or not resolved.name.startswith(prefix)
        or not stat.S_ISDIR(path.lstat().st_mode)
    ):
        raise RuntimeError("refusing to remove an unexpected build artifact")
    shutil.rmtree(resolved)


def _remove_owned_directory_best_effort(path: Path, parent: Path, prefix: str) -> None:
    try:
        _remove_owned_directory(path, parent, prefix)
    except Exception:
        # Installation is already committed. A uniquely named remainder is safer
        # than reporting failure after rmtree may have changed the old backup.
        return


def _install_staging(staging: Path, output: Path) -> None:
    parent = output.parent
    backup = parent / f".{output.name}.ai-news-backup-{uuid4().hex}"
    moved_old_output = False
    try:
        if output.exists():
            os.replace(output, backup)
            moved_old_output = True
        try:
            os.replace(staging, output)
        except BaseException:
            if moved_old_output:
                os.replace(backup, output)
                moved_old_output = False
            raise
        if moved_old_output:
            _remove_owned_directory_best_effort(
                backup,
                parent,
                f".{output.name}.ai-news-backup-",
            )
    finally:
        if staging.exists():
            _remove_owned_directory(
                staging,
                parent,
                f".{output.name}.ai-news-stage-",
            )


def build_site(
    root: Path,
    output: Path,
    base_path: str = "/ai-news/",
) -> None:
    """Build an atomic, subpath-safe static site from stored daily reports."""

    base_path = _validate_base_path(base_path)
    root_path = Path(root)
    output_path = Path(output)
    root_resolved, output_resolved = _validate_output(root_path, output_path)
    archive_reports = sorted(
        load_reports(root_path),
        key=lambda report: report.date,
        reverse=True,
    )
    if not archive_reports:
        raise ValueError("at least one report is required to build the site")
    run_records = load_run_records(root_path)
    record_by_date = {record.date: record for record in run_records}
    reports = [
        report
        for report in archive_reports
        if record_by_date.get(report.date) is None
        or record_by_date[report.date].status is not RunStatus.NO_ELIGIBLE_CONTENT
    ]
    if not reports:
        raise ValueError("at least one published report is required to build the site")
    source_specs = _load_source_specs(root_path, archive_reports, run_records)

    output_resolved.parent.mkdir(parents=True, exist_ok=True)
    confirmed_root, confirmed_output = _validate_output(root_path, output_path)
    if confirmed_root != root_resolved or confirmed_output != output_resolved:
        raise ValueError("root or output path changed during site build")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_resolved.name}.ai-news-stage-",
            dir=output_resolved.parent,
        )
    )
    try:
        _build_staging(
            staging,
            reports,
            archive_reports,
            run_records,
            source_specs,
            base_path,
        )
        _validate_staging(staging, reports)
        _install_staging(staging, output_resolved)
    except BaseException:
        if staging.exists():
            _remove_owned_directory(
                staging,
                output_resolved.parent,
                f".{output_resolved.name}.ai-news-stage-",
            )
        raise
