from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from ai_news.models import Category, DailyReport, NewsItem
from ai_news.storage import load_reports

_SITE_DIRECTORY = Path(__file__).parent
_TEMPLATE_DIRECTORY = _SITE_DIRECTORY / "templates"
_STATIC_DIRECTORY = _SITE_DIRECTORY / "static"
_SHANGHAI = ZoneInfo("Asia/Shanghai")

CATEGORY_SLUGS: dict[Category, str] = {
    Category.MODEL: "model",
    Category.AGENT: "agent",
    Category.CODING_AGENT: "coding-agent",
    Category.TOOL: "ai-tools",
    Category.OPEN_SOURCE: "open-source",
    Category.RESEARCH: "research",
}


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


def _existing_path_chain(path: Path) -> Iterable[Path]:
    absolute = path.expanduser().absolute()
    yield absolute
    yield from absolute.parents


def _validate_output(root: Path, output: Path) -> tuple[Path, Path]:
    root_resolved = _resolved(root, label="root")
    output_input = output.expanduser()
    for candidate in _existing_path_chain(output_input):
        if candidate.is_symlink():
            raise ValueError("output must not use symlinked paths")

    output_resolved = _resolved(output_input, label="output")
    home_resolved = _resolved(Path.home(), label="home")
    filesystem_root = Path(output_resolved.anchor)

    if (
        output_resolved in (filesystem_root, root_resolved, home_resolved)
        or root_resolved.is_relative_to(output_resolved)
        or home_resolved.is_relative_to(output_resolved)
    ):
        raise ValueError("output path is unsafe")
    if output_input.is_symlink():
        raise ValueError("output must not be a symlink")
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
    return environment


def _category_navigation(categories: Iterable[Category]) -> list[dict[str, str]]:
    category_set = set(categories)
    return [
        {"name": category.value, "slug": slug}
        for category, slug in CATEGORY_SLUGS.items()
        if category in category_set
    ]


def _report_context(report: DailyReport) -> dict[str, object]:
    return {
        "date": report.date.isoformat(),
        "generated_at": report.generated_at,
        "source_count": len(report.source_runs),
        "candidate_count": report.candidate_count,
        "included_count": len(report.items),
    }


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
    report_date: str,
    page_url: Callable[[str], str],
) -> dict[str, object]:
    payload = item.model_dump(mode="json")
    payload["date"] = report_date
    payload["page_url"] = page_url(f"days/{report_date}")
    return payload


def _build_staging(
    staging: Path,
    reports: list[DailyReport],
    base_path: str,
) -> None:
    environment = _environment(base_path)

    def page_url(page: str = "") -> str:
        return _page_url(base_path, page)

    latest = reports[0]
    all_pairs = [(report, item) for report in reports for item in report.items]
    categories = _category_navigation(item.category for _report, item in all_pairs)
    common: dict[str, object] = {
        "site_name": "AI 情报雷达",
        "latest_report": _report_context(latest),
        "categories": categories,
    }

    shutil.copytree(_STATIC_DIRECTORY, staging / "assets")

    latest_items = _sort_items(latest.items)
    _render(
        environment,
        staging,
        "index.html",
        "index.html",
        {
            **common,
            "page_title": "今日情报",
            "report": _report_context(latest),
            "items": latest_items,
            "top_items": latest_items[:3],
            "signals": latest_items[:3],
        },
    )

    for report in reports:
        report_items = _sort_items(report.items)
        _render(
            environment,
            staging,
            "day.html",
            f"days/{report.date.isoformat()}/index.html",
            {
                **common,
                "page_title": f"{report.date.isoformat()} 情报",
                "report": _report_context(report),
                "items": report_items,
                "signals": report_items[:3],
            },
        )

    _render(
        environment,
        staging,
        "archive.html",
        "archive/index.html",
        {
            **common,
            "page_title": "日期归档",
            "reports": [_report_context(report) for report in reports],
        },
    )

    category_items: dict[Category, list[NewsItem]] = defaultdict(list)
    for _report, item in all_pairs:
        category_items[item.category].append(item)
    for category, items in category_items.items():
        ordered_items = _sort_items(items)
        _render(
            environment,
            staging,
            "category.html",
            f"categories/{CATEGORY_SLUGS[category]}/index.html",
            {
                **common,
                "page_title": f"{category.value} 情报",
                "category_name": category.value,
                "items": ordered_items,
                "signals": ordered_items[:3],
            },
        )

    search_payload = [
        _public_search_entry(item, report.date.isoformat(), page_url) for report, item in all_pairs
    ]
    search_payload.sort(
        key=lambda entry: (
            -int(entry["importance"]),
            -datetime.fromisoformat(str(entry["published_at"]).replace("Z", "+00:00")).timestamp(),
            str(entry["id"]),
        )
    )
    _write_text(
        staging / "search.json",
        json.dumps(search_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _validate_staging(staging: Path, reports: list[DailyReport]) -> None:
    required = {
        staging / "index.html",
        staging / "archive/index.html",
        staging / "assets/styles.css",
        staging / "assets/app.js",
        staging / "search.json",
        *(staging / f"days/{report.date.isoformat()}/index.html" for report in reports),
    }
    category_slugs = {CATEGORY_SLUGS[item.category] for report in reports for item in report.items}
    required.update(staging / f"categories/{slug}/index.html" for slug in category_slugs)
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
            _remove_owned_directory(
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
    _root_resolved, output_resolved = _validate_output(Path(root), Path(output))
    reports = sorted(load_reports(Path(root)), key=lambda report: report.date, reverse=True)
    if not reports:
        raise ValueError("at least one report is required to build the site")

    output_resolved.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_resolved.name}.ai-news-stage-",
            dir=output_resolved.parent,
        )
    )
    try:
        _build_staging(staging, reports, base_path)
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
