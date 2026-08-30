# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import html
import json
import os
import tomllib
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ai_news import cli
from ai_news.models import (
    Category,
    DailyReport,
    EditorialLane,
    NewsItem,
    RunRecord,
    RunStatus,
    SourceRun,
)
from ai_news.site import builder
from ai_news.site.builder import build_site
from ai_news.storage import save_report, save_run_record

BASE_PATH = "/ai-news/"
FIXED_CHANNEL_SLUGS = (
    "model",
    "agent",
    "ai-tools",
    "open-source",
    "industry-policy",
)
PUBLIC_SEARCH_FIELDS = {
    "id",
    "title",
    "summary",
    "why_it_matters",
    "tags",
    "display_category",
    "source",
    "published_at",
    "report_date",
    "is_official",
    "marketing_risk",
    "page_url",
}


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.ids: set[str] = set()
        self.element_ids: list[str] = []
        self.article_ids: list[str] = []
        self.script_starts = 0
        self._article_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if element_id := attributes.get("id"):
            self.ids.add(element_id)
            self.element_ids.append(element_id)
        if tag in {"a", "link", "script"}:
            self.links.append({"tag": tag, **attributes})
        if tag == "article":
            self._article_depth += 1
            if item_id := attributes.get("data-item-id"):
                self.article_ids.append(item_id)
        if tag == "script":
            self.script_starts += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self._article_depth -= 1


def _news_item(
    slug: str,
    *,
    category: Category,
    importance: int,
    published_at: datetime,
    title: str | None = None,
    source: str = "官方实验室",
    lane: EditorialLane | None = None,
) -> NewsItem:
    canonical_url = f"https://github.com/example/{slug}"
    item_id = hashlib.sha256(canonical_url.encode()).hexdigest()[:16]
    values: dict[str, object] = {
        "id": item_id,
        "canonical_url": canonical_url,
        "original_title": f"Original **{slug}**",
        "source": source,
        "published_at": published_at,
        "title": title or f"中文情报 {slug}",
        "category": category,
        "summary": f"这是关于 {slug} 的事实摘要，长度足够用于验证站点构建和安全转义。",
        "importance": importance,
        "why_it_matters": f"{slug} 会影响开发团队的技术选择与未来产品路线。",
        "tags": ["AI", slug],
        "is_official": slug != "old-research",
        "marketing_risk": "high" if slug == "agent" else "low",
        "tracking_signal": f"持续跟踪 {slug} 的公开基准与正式发布说明。",
    }
    if lane is not None:
        values["editorial_lane"] = lane
    return NewsItem.model_validate(values)


def _report(
    run_date: date,
    generated_hour: int,
    items: list[NewsItem],
    *,
    candidate_count: int,
) -> DailyReport:
    return DailyReport(
        date=run_date,
        generated_at=datetime(
            run_date.year, run_date.month, run_date.day, generated_hour, tzinfo=UTC
        ),
        model="private-model-must-not-leak",
        degraded=False,
        candidate_count=candidate_count,
        items=items,
        source_runs=[
            SourceRun.succeeded(
                source_id="official-feed",
                source_name="Official Feed",
                item_count=len(items),
                elapsed_ms=80,
            ),
            SourceRun.failed(
                source_id="secondary-feed",
                source_name="Secondary Feed",
                elapsed_ms=25,
                error=RuntimeError("private-error-must-not-leak"),
            ),
        ],
    )


def _saved_report_root(tmp_path: Path) -> tuple[Path, dict[str, NewsItem]]:
    root = tmp_path / "workspace"
    latest_day = date(2026, 7, 26)
    old_day = date(2026, 7, 25)
    items = {
        "coding": _news_item(
            "coding",
            category=Category.CODING_AGENT,
            importance=10,
            published_at=datetime(2026, 7, 26, 9, 0, tzinfo=UTC),
            title="<script>alert('not-an-element')</script> 编码代理",
        ),
        "model": _news_item(
            "model",
            category=Category.MODEL,
            importance=8,
            published_at=datetime(2026, 7, 26, 11, 0, tzinfo=UTC),
        ),
        "agent": _news_item(
            "agent",
            category=Category.AGENT,
            importance=8,
            published_at=datetime(2026, 7, 26, 10, 0, tzinfo=UTC),
        ),
        "old-coding": _news_item(
            "old-coding",
            category=Category.CODING_AGENT,
            importance=7,
            published_at=datetime(2026, 7, 25, 12, 0, tzinfo=UTC),
        ),
        "tool": _news_item(
            "tool",
            category=Category.TOOL,
            importance=6,
            published_at=datetime(2026, 7, 25, 11, 0, tzinfo=UTC),
        ),
        "open": _news_item(
            "open",
            category=Category.OPEN_SOURCE,
            importance=5,
            published_at=datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
        ),
        "old-research": _news_item(
            "old-research",
            category=Category.RESEARCH,
            importance=4,
            published_at=datetime(2026, 7, 25, 9, 0, tzinfo=UTC),
        ),
    }
    save_report(
        root,
        _report(
            old_day,
            1,
            [items["old-coding"], items["tool"], items["open"], items["old-research"]],
            candidate_count=13,
        ),
    )
    save_report(
        root,
        _report(
            latest_day,
            2,
            [items["agent"], items["coding"], items["model"]],
            candidate_count=11,
        ),
    )
    return root, items


def _write_source_config(root: Path) -> None:
    sources = root / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    (sources / "feeds.yaml").write_text(
        """sources:
  - id: official-feed
    name: Official Feed
    kind: feed
    url: https://example.com/official.xml
    category: 大模型
    official: true
  - id: secondary-feed
    name: Secondary Feed
    kind: feed
    url: https://example.com/secondary.xml
    category: AI 工具
    official: false
    role: trusted_media
  - id: configured-missing
    name: Configured Missing
    kind: feed
    url: https://example.com/missing.xml
    category: Agent
    official: true
""",
        encoding="utf-8",
    )


@pytest.fixture
def report_root(tmp_path: Path) -> tuple[Path, dict[str, NewsItem]]:
    return _saved_report_root(tmp_path)


def _parse(path: Path) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


def _directory_bytes(path: Path) -> dict[str, bytes]:
    return {
        file.relative_to(path).as_posix(): file.read_bytes()
        for file in sorted(path.rglob("*"))
        if file.is_file()
    }


def _assert_internal_targets_exist(output: Path, html_path: Path) -> None:
    parser = _parse(html_path)
    for link in parser.links:
        value = link.get("href") or link.get("src")
        if not value or value.startswith(("http://", "https://", "mailto:")):
            continue
        parsed = urlsplit(value)
        assert parsed.scheme == parsed.netloc == ""
        if not parsed.path and parsed.fragment:
            assert parsed.fragment in parser.ids
            continue
        assert value.startswith(BASE_PATH)
        relative = parsed.path.removeprefix(BASE_PATH)
        target = output / relative
        if parsed.path.endswith("/"):
            target /= "index.html"
        assert target.is_file(), f"{html_path}: missing target for {value}"
        if parsed.fragment:
            assert parsed.fragment in _parse(target).ids


def test_package_data_configuration_covers_all_site_templates_and_static_assets() -> None:
    repository = Path(__file__).parents[1]
    configuration = tomllib.loads((repository / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools_configuration = configuration["tool"]["setuptools"]

    assert "package-data" in setuptools_configuration
    package_data = setuptools_configuration["package-data"]
    assert set(package_data["ai_news.site"]) == {
        "templates/*.html",
        "static/*.css",
        "static/*.js",
        "static/*.svg",
    }

    site_directory = repository / "src/ai_news/site"
    configured_resources = {
        path.relative_to(site_directory).as_posix()
        for pattern in package_data["ai_news.site"]
        for path in site_directory.glob(pattern)
    }
    expected_resources = {
        path.relative_to(site_directory).as_posix()
        for directory in ("templates", "static")
        for path in (site_directory / directory).iterdir()
        if path.is_file()
    }
    assert configured_resources == expected_resources


def test_build_writes_complete_subpath_site_and_valid_internal_links(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
) -> None:
    root, _items = report_root
    output = tmp_path / "dist"

    build_site(root, output, BASE_PATH)

    expected = {
        "index.html",
        "archive/index.html",
        "search/index.html",
        "sources/index.html",
        "days/2026-07-26/index.html",
        "days/2026-07-25/index.html",
        "assets/styles.css",
        "assets/app.js",
        "search.json",
        *(f"categories/{slug}/index.html" for slug in FIXED_CHANNEL_SLUGS),
    }
    assert expected <= {
        path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()
    }
    html_files = sorted(output.rglob("*.html"))
    assert html_files
    for html_file in html_files:
        html_text = html_file.read_text(encoding="utf-8")
        assert f"{BASE_PATH}assets/styles.css" in html_text
        assert f"{BASE_PATH}assets/app.js" in html_text
        _assert_internal_targets_exist(output, html_file)


def test_home_uses_latest_report_and_feed_category_order_is_stable(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
) -> None:
    root, items = report_root
    output = tmp_path / "dist"

    build_site(root, output)

    homepage = _parse(output / "index.html")
    latest_expected = [
        items["coding"].id,
        items["model"].id,
        items["agent"].id,
    ]
    assert homepage.article_ids == latest_expected
    assert len(homepage.article_ids) == len(set(homepage.article_ids))
    assert items["old-coding"].id not in homepage.article_ids
    agent_page = _parse(output / "categories/agent/index.html")
    assert agent_page.article_ids == [
        items["agent"].id,
        items["coding"].id,
        items["old-coding"].id,
    ]
    assert not (output / "categories/coding-agent/index.html").exists()
    assert not (output / "categories/research/index.html").exists()
    assert (output / "categories/industry-policy/index.html").is_file()

    archive_text = (output / "archive/index.html").read_text(encoding="utf-8")
    assert archive_text.index("2026-07-26") < archive_text.index("2026-07-25")
    assert "最近生成" in (output / "index.html").read_text(encoding="utf-8")
    assert "北京时间" in (output / "index.html").read_text(encoding="utf-8")


def test_fixed_channels_paginate_without_losing_time_ordered_entries(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    newest = date(2026, 8, 30)
    expected_ids: list[str] = []
    for offset in range(41):
        run_date = newest - timedelta(days=offset)
        item = _news_item(
            f"model-{offset:02d}",
            category=Category.MODEL,
            importance=5,
            published_at=datetime.combine(run_date, datetime.min.time(), tzinfo=UTC),
        )
        expected_ids.append(item.id)
        save_report(root, _report(run_date, 1, [item], candidate_count=1))

    output = tmp_path / "dist"
    build_site(root, output)

    pages = [
        output / "categories/model/index.html",
        output / "categories/model/page/2/index.html",
        output / "categories/model/page/3/index.html",
    ]
    assert [len(_parse(page).article_ids) for page in pages] == [20, 20, 1]
    assert [item_id for page in pages for item_id in _parse(page).article_ids] == expected_ids
    assert all((output / f"categories/{slug}/index.html").is_file() for slug in FIXED_CHANNEL_SLUGS)
    assert not (output / "categories/agent/page/2/index.html").exists()


def test_day_entries_have_continuous_numbers_title_deep_links_and_edition_navigation(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
) -> None:
    root, items = report_root
    output = tmp_path / "dist"

    build_site(root, output)

    day_path = output / "days/2026-07-26/index.html"
    day_text = day_path.read_text(encoding="utf-8")
    day = _parse(day_path)
    assert day_text.count('class="story-number"') == 3
    assert all(f">{number:02d}<" in day_text for number in range(1, 4))
    for item in (items["coding"], items["model"], items["agent"]):
        fragment = f"item-{item.id}-2026-07-26"
        assert fragment in day.ids
        assert any(link.get("href") == f"#{fragment}" for link in day.links)
    assert any(link.get("href") == f"{BASE_PATH}days/2026-07-25/" for link in day.links)
    assert any(link.get("href") == f"{BASE_PATH}archive/" for link in day.links)


def test_archive_uses_run_records_to_show_gaps_and_shadow_stale_reports(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
) -> None:
    root, _items = report_root
    save_run_record(
        root,
        RunRecord(
            date=date(2026, 7, 25),
            cutoff_at=datetime(2026, 7, 25, 15, 59, 59, tzinfo=UTC),
            status=RunStatus.NO_ELIGIBLE_CONTENT,
            source_runs=[],
            safe_error_code="no_eligible_candidates",
        ),
    )
    save_run_record(
        root,
        RunRecord(
            date=date(2026, 7, 23),
            cutoff_at=datetime(2026, 7, 23, 15, 59, 59, tzinfo=UTC),
            status=RunStatus.FAILED_BEFORE_PUBLISH,
            source_runs=[],
            safe_error_code="analysis_failed",
        ),
    )
    output = tmp_path / "dist"

    build_site(root, output)

    archive = (output / "archive/index.html").read_text(encoding="utf-8")
    assert "无合格内容" in archive
    assert "未运行" in archive
    assert "生成失败" in archive
    assert not (output / "days/2026-07-25/index.html").exists()


def test_sources_page_uses_public_run_loader_and_keeps_configured_missing_sources(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
) -> None:
    root, _items = report_root
    _write_source_config(root)
    detailed_run = SourceRun.succeeded(
        source_id="official-feed",
        source_name="Official Feed",
        item_count=4,
        elapsed_ms=20,
    ).with_diagnostics(
        recent_count=3,
        new_count=2,
        eligible_count=2,
        candidate_count=1,
        selected_count=1,
        latest_published_at=datetime(2026, 7, 26, 9, tzinfo=UTC),
    )
    save_run_record(
        root,
        RunRecord(
            date=date(2026, 7, 26),
            cutoff_at=datetime(2026, 7, 26, 15, 59, 59, tzinfo=UTC),
            status=RunStatus.PUBLISHED,
            source_runs=[detailed_run],
        ),
    )
    output = tmp_path / "dist"

    build_site(root, output)

    sources = (output / "sources/index.html").read_text(encoding="utf-8")
    assert all(
        name in sources for name in ("Official Feed", "Secondary Feed", "Configured Missing")
    )
    assert all(
        metric in sources
        for metric in (
            "<dt>近窗</dt><dd>3</dd>",
            "<dt>新增</dt><dd>2</dd>",
            "<dt>合格</dt><dd>2</dd>",
            "<dt>候选</dt><dd>1</dd>",
            "<dt>入选</dt><dd>1</dd>",
        )
    )
    assert "暂无运行数据" in sources


def test_category_cards_have_unique_dom_ids_when_an_item_spans_multiple_reports(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    shared = _news_item(
        "shared-release",
        category=Category.OPEN_SOURCE,
        importance=8,
        published_at=datetime(2026, 8, 21, 8, 0, tzinfo=UTC),
    )
    save_report(
        root,
        _report(date(2026, 8, 22), 10, [shared], candidate_count=1),
    )
    save_report(
        root,
        _report(date(2026, 8, 21), 10, [shared], candidate_count=1),
    )

    output = tmp_path / "dist"
    build_site(root, output)

    page = _parse(output / "categories/open-source/index.html")
    card_ids = [
        element_id for element_id in page.element_ids if element_id.startswith(f"item-{shared.id}")
    ]
    assert len(card_ids) == 2
    assert len(set(card_ids)) == 2


def test_brief_pages_render_each_item_once_without_verbose_controls(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
) -> None:
    root, items = report_root
    output = tmp_path / "dist"

    build_site(root, output)

    for page, expected_ids in (
        (
            output / "index.html",
            [items["coding"].id, items["model"].id, items["agent"].id],
        ),
        (
            output / "days/2026-07-26/index.html",
            [items["coding"].id, items["model"].id, items["agent"].id],
        ),
    ):
        parser = _parse(page)
        text = page.read_text(encoding="utf-8")
        assert parser.article_ids == expected_ids
        assert "data-priority=" not in text
        assert 'type="search"' not in text
        assert "今日最重要" not in text
        assert "后续观察" not in text
        assert "营销风险" not in text
        assert "原始标题" not in text
        assert "展开研判细节" not in text
        assert "重要性 " not in text
        assert all(label in text for label in ("变化", "影响"))
        assert "行动" not in text


def test_external_links_are_hardened_and_untrusted_titles_are_escaped(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
) -> None:
    root, _items = report_root
    output = tmp_path / "dist"

    build_site(root, output)

    homepage_text = (output / "index.html").read_text(encoding="utf-8")
    parser = _parse(output / "index.html")
    external_links = [
        link
        for link in parser.links
        if (link.get("href") or "").startswith(("http://", "https://"))
    ]
    assert external_links
    assert all(link.get("target") == "_blank" for link in external_links)
    assert all(
        set(link.get("rel", "").split()) == {"noopener", "noreferrer"} for link in external_links
    )
    assert "alert('not-an-element')" in html.unescape(homepage_text)
    assert parser.script_starts == 1


def test_search_json_is_public_deterministic_and_points_to_real_day_pages(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
) -> None:
    root, _items = report_root
    output = tmp_path / "dist"

    build_site(root, output)
    first_bytes = (output / "search.json").read_bytes()
    payload = json.loads(first_bytes)
    build_site(root, output)

    assert (output / "search.json").read_bytes() == first_bytes
    assert len(payload) == 6
    assert all(entry["display_category"] != "论文研究" for entry in payload)
    assert all(set(entry) == PUBLIC_SEARCH_FIELDS for entry in payload)
    assert all(entry["page_url"].startswith(f"{BASE_PATH}days/") for entry in payload)
    for entry in payload:
        parsed = urlsplit(entry["page_url"])
        target = output / parsed.path.removeprefix(BASE_PATH) / "index.html"
        assert target.is_file()
        assert parsed.fragment in _parse(target).ids
    serialized = first_bytes.decode()
    for private_name in (
        "source_runs",
        "error_type",
        "private-error",
        "private-model",
        "filesystem",
        "analysis",
        "secret",
    ):
        assert private_name not in serialized


@pytest.mark.parametrize(
    "base_path",
    [
        "",
        "ai-news/",
        "/ai-news",
        "//ai-news/",
        "/a//b/",
        "/../ai-news/",
        "/ai-news/../x/",
        "/ai-news/%2e%2e/x/",
        "/ai-news/%5c/x/",
        "/ai-news/%2f/x/",
        "/ai\\news/",
        "https://example.com/ai-news/",
        "/ai-news/?query=yes",
        "/ai-news/#fragment",
    ],
)
def test_invalid_base_paths_are_rejected_before_output_changes(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
    base_path: str,
) -> None:
    root, _items = report_root
    output = tmp_path / "dist"
    output.mkdir()
    sentinel = output / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="base_path"):
        build_site(root, output, base_path)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_dangerous_outputs_and_symlinks_are_rejected_without_touching_sentinels(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
) -> None:
    root, _items = report_root
    source_sentinel = root / "source-sentinel.txt"
    source_sentinel.write_text("source", encoding="utf-8")
    ancestor_sentinel = tmp_path / "ancestor-sentinel.txt"
    ancestor_sentinel.write_text("ancestor", encoding="utf-8")
    actual_output = tmp_path / "actual-output"
    actual_output.mkdir()
    output_sentinel = actual_output / "sentinel.txt"
    output_sentinel.write_text("linked", encoding="utf-8")
    symlink_output = tmp_path / "linked-output"
    symlink_output.symlink_to(actual_output, target_is_directory=True)

    dangerous = [Path("/"), root, Path.home(), tmp_path, symlink_output]
    for output in dangerous:
        with pytest.raises(ValueError, match="output"):
            build_site(root, output)

    assert source_sentinel.read_text(encoding="utf-8") == "source"
    assert ancestor_sentinel.read_text(encoding="utf-8") == "ancestor"
    assert output_sentinel.read_text(encoding="utf-8") == "linked"


@pytest.mark.parametrize(
    "relative_output",
    [
        "data",
        "data/site",
        "content",
        "content/site",
        "sources",
        "sources/site",
    ],
)
def test_reserved_storage_paths_are_rejected_without_changing_persisted_bytes(
    report_root: tuple[Path, dict[str, NewsItem]],
    relative_output: str,
) -> None:
    root, _items = report_root
    sources = root / "sources"
    sources.mkdir()
    (sources / "feeds.yaml").write_bytes(b"sources:\n  - id: persisted\n")
    before = _directory_bytes(root)

    with pytest.raises(ValueError, match="reserved"):
        build_site(root, root / relative_output)

    assert _directory_bytes(root) == before


def test_root_dist_is_allowed_and_replaces_only_the_existing_output(
    report_root: tuple[Path, dict[str, NewsItem]],
) -> None:
    root, _items = report_root
    output = root / "dist"
    output.mkdir()
    stale = output / "stale.txt"
    stale.write_text("old", encoding="utf-8")
    source_sentinel = root / "source-sentinel.txt"
    source_sentinel.write_text("source", encoding="utf-8")

    build_site(root, output)

    assert not stale.exists()
    assert (output / "index.html").is_file()
    assert source_sentinel.read_text(encoding="utf-8") == "source"


def test_safe_symlink_ancestor_alias_builds_only_in_resolved_output(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    real_root, _items = _saved_report_root(real_parent)
    alias_root = tmp_path / "safe-root-alias"
    alias_root.symlink_to(real_root, target_is_directory=True)
    alias_output = alias_root / "dist"

    build_site(alias_root, alias_output)

    assert alias_output.resolve() == real_root / "dist"
    assert (real_root / "dist/index.html").is_file()
    assert (real_root / "dist/assets/styles.css").is_file()
    assert alias_root.is_symlink()


def test_cli_demo_builds_real_site_through_safe_alias_with_default_base_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for environment_name in (
        "LLM_PROTOCOL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_AUTH_SCHEME",
    ):
        monkeypatch.delenv(environment_name, raising=False)
    real_parent = tmp_path / "real-demo"
    real_parent.mkdir()
    alias_parent = tmp_path / "demo-alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    root = alias_parent / "workspace"
    output = root / "dist"

    result = cli.main(["demo", "--root", str(root), "--output", str(output)])

    assert result == 0
    assert (real_parent / "workspace/dist/index.html").is_file()
    homepage = (real_parent / "workspace/dist/index.html").read_text(encoding="utf-8")
    assert "/ai-news/assets/styles.css" in homepage
    assert "/ai-news/assets/app.js" in homepage


def test_empty_or_failed_build_preserves_existing_output(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _items = report_root
    output = tmp_path / "dist"
    output.mkdir()
    sentinel = output / "sentinel.txt"
    sentinel.write_text("old site", encoding="utf-8")
    empty_root = tmp_path / "empty"

    with pytest.raises(ValueError, match="report"):
        build_site(empty_root, output)
    assert sentinel.read_text(encoding="utf-8") == "old site"

    def fail_copytree(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated static copy failure")

    monkeypatch.setattr(builder.shutil, "copytree", fail_copytree)
    with pytest.raises(OSError, match="simulated"):
        build_site(root, output)
    assert sentinel.read_text(encoding="utf-8") == "old site"
    assert not any(path.name.startswith(".dist.ai-news-stage-") for path in tmp_path.iterdir())


def test_output_replacement_rolls_back_when_final_replace_fails(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _items = report_root
    output = tmp_path / "dist"
    output.mkdir()
    sentinel = output / "sentinel.txt"
    sentinel.write_text("old site", encoding="utf-8")
    real_replace = os.replace
    replacement_attempts = 0

    def fail_staging_install(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        nonlocal replacement_attempts
        replacement_attempts += 1
        if replacement_attempts == 2:
            raise OSError("simulated install failure")
        real_replace(source, target)

    monkeypatch.setattr(builder.os, "replace", fail_staging_install)

    with pytest.raises(OSError, match="simulated"):
        build_site(root, output)

    assert sentinel.read_text(encoding="utf-8") == "old site"
    assert not any("ai-news-" in path.name for path in tmp_path.iterdir() if path != output)


def test_partial_backup_cleanup_failure_keeps_successful_site_and_owned_remainder(
    report_root: tuple[Path, dict[str, NewsItem]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _items = report_root
    output = tmp_path / "dist"
    (output / "nested").mkdir(parents=True)
    (output / "sentinel.txt").write_bytes(b"old site\n")
    (output / "nested/preserve.bin").write_bytes(b"\x00old\xff")
    real_rmtree = builder.shutil.rmtree

    def partially_fail_backup_cleanup(
        path: str | os.PathLike[str],
        *args: object,
        **kwargs: object,
    ) -> None:
        selected_path = Path(path)
        if ".dist.ai-news-backup-" in selected_path.name:
            (selected_path / "nested/preserve.bin").unlink()
            raise PermissionError("simulated backup cleanup denial")
        real_rmtree(selected_path, *args, **kwargs)

    monkeypatch.setattr(builder.shutil, "rmtree", partially_fail_backup_cleanup)

    build_site(root, output)

    assert (output / "index.html").is_file()
    assert (output / "search.json").is_file()
    assert not (output / "sentinel.txt").exists()
    backup_artifacts = [
        path for path in tmp_path.iterdir() if path.name.startswith(".dist.ai-news-backup-")
    ]
    assert len(backup_artifacts) == 1
    backup = backup_artifacts[0]
    assert not backup.is_symlink()
    assert _directory_bytes(backup) == {"sentinel.txt": b"old site\n"}

    monkeypatch.setattr(builder.shutil, "rmtree", real_rmtree)
    builder._remove_owned_directory(backup, tmp_path, ".dist.ai-news-backup-")
    assert not backup.exists()
