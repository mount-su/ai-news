# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import html
import json
import os
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ai_news.models import Category, DailyReport, NewsItem, SourceRun
from ai_news.site import builder
from ai_news.site.builder import build_site
from ai_news.storage import save_report

BASE_PATH = "/ai-news/"
CATEGORY_SLUGS = {
    Category.MODEL: "model",
    Category.AGENT: "agent",
    Category.CODING_AGENT: "coding-agent",
    Category.TOOL: "ai-tools",
    Category.OPEN_SOURCE: "open-source",
    Category.RESEARCH: "research",
}
PUBLIC_SEARCH_FIELDS = {
    "id",
    "canonical_url",
    "original_title",
    "source",
    "published_at",
    "title",
    "category",
    "summary",
    "importance",
    "why_it_matters",
    "tags",
    "is_official",
    "marketing_risk",
    "tracking_signal",
    "date",
    "page_url",
}


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.ids: set[str] = set()
        self.article_ids: list[str] = []
        self.script_starts = 0
        self._article_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if element_id := attributes.get("id"):
            self.ids.add(element_id)
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
) -> NewsItem:
    canonical_url = f"https://github.com/example/{slug}"
    item_id = hashlib.sha256(canonical_url.encode()).hexdigest()[:16]
    return NewsItem(
        id=item_id,
        canonical_url=canonical_url,
        original_title=f"Original **{slug}**",
        source=source,
        published_at=published_at,
        title=title or f"中文情报 {slug}",
        category=category,
        summary=f"这是关于 {slug} 的事实摘要，长度足够用于验证站点构建和安全转义。",
        importance=importance,
        why_it_matters=f"{slug} 会影响开发团队的技术选择与未来产品路线。",
        tags=["AI", slug],
        is_official=slug != "old-research",
        marketing_risk="high" if slug == "agent" else "low",
        tracking_signal=f"持续跟踪 {slug} 的公开基准与正式发布说明。",
    )


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


@pytest.fixture
def report_root(tmp_path: Path) -> tuple[Path, dict[str, NewsItem]]:
    return _saved_report_root(tmp_path)


def _parse(path: Path) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


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
        "days/2026-07-26/index.html",
        "days/2026-07-25/index.html",
        "assets/styles.css",
        "assets/app.js",
        "search.json",
        *(f"categories/{slug}/index.html" for slug in CATEGORY_SLUGS.values()),
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
    assert homepage.article_ids[-3:] == latest_expected
    assert items["old-coding"].id not in homepage.article_ids
    coding_page = _parse(output / "categories/coding-agent/index.html")
    assert coding_page.article_ids == [items["coding"].id, items["old-coding"].id]

    archive_text = (output / "archive/index.html").read_text(encoding="utf-8")
    assert archive_text.index("2026-07-26") < archive_text.index("2026-07-25")
    assert "最后成功" in (output / "index.html").read_text(encoding="utf-8")
    assert "北京时间" in (output / "index.html").read_text(encoding="utf-8")


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
    assert len(payload) == 7
    assert all(set(entry) == PUBLIC_SEARCH_FIELDS for entry in payload)
    assert all(entry["page_url"].startswith(f"{BASE_PATH}days/") for entry in payload)
    assert all(
        (output / entry["page_url"].removeprefix(BASE_PATH) / "index.html").is_file()
        for entry in payload
    )
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
