from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest
from test_site_builder import BASE_PATH, _saved_report_root

from ai_news.site.builder import build_site


@pytest.fixture
def accessibility_report_root(tmp_path: Path) -> tuple[Path, object]:
    return _saved_report_root(tmp_path)


class _AccessibilityParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.html_lang = ""
        self.main_count = 0
        self.main_ids: list[str] = []
        self.body_start_tags: list[tuple[str, dict[str, str]]] = []
        self.labels: list[dict[str, str]] = []
        self.inputs: list[dict[str, str]] = []
        self.buttons: list[dict[str, str]] = []
        self.details_count = 0
        self.summary_count = 0
        self.landmarks: set[str] = set()
        self.headings: list[int] = []
        self.h1_count = 0
        self.links: list[dict[str, str]] = []
        self.article_links: list[dict[str, str]] = []
        self.aria_live: list[str] = []
        self.text_parts: list[str] = []
        self._in_body = False
        self._article_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "html":
            self.html_lang = attributes.get("lang", "")
        if tag == "body":
            self._in_body = True
        elif self._in_body and len(self.body_start_tags) < 3:
            self.body_start_tags.append((tag, attributes))
        if tag == "main":
            self.main_count += 1
            self.main_ids.append(attributes.get("id", ""))
        if tag in {"header", "nav", "section", "article", "footer"}:
            self.landmarks.add(tag)
        if tag == "article":
            self._article_depth += 1
        if tag == "a":
            self.links.append(attributes)
            if self._article_depth:
                self.article_links.append(attributes)
        if tag == "label":
            self.labels.append(attributes)
        if tag == "input":
            self.inputs.append(attributes)
        if tag == "button":
            self.buttons.append(attributes)
        if tag == "details":
            self.details_count += 1
        if tag == "summary":
            self.summary_count += 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.headings.append(int(tag[1]))
            if tag == "h1":
                self.h1_count += 1
        if live := attributes.get("aria-live"):
            self.aria_live.append(live)

    def handle_endtag(self, tag: str) -> None:
        if tag == "body":
            self._in_body = False
        if tag == "article":
            self._article_depth -= 1

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)


def _parse(path: Path) -> _AccessibilityParser:
    parser = _AccessibilityParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


def test_all_pages_have_single_main_skip_link_landmarks_and_ordered_headings(
    accessibility_report_root: tuple[Path, object],
    tmp_path: Path,
) -> None:
    root, _items = accessibility_report_root
    output = tmp_path / "dist"
    build_site(root, output)

    for page in output.rglob("*.html"):
        parser = _parse(page)
        assert parser.html_lang == "zh-CN"
        assert parser.main_count == 1
        assert parser.h1_count == 1
        assert parser.main_ids == ["main-content"]
        assert parser.body_start_tags[0][0] == "a"
        assert parser.body_start_tags[0][1].get("href") == "#main-content"
        assert {"header", "nav", "section", "footer"} <= parser.landmarks
        assert parser.headings[0] == 1
        assert all(
            current <= previous + 1
            for previous, current in zip(parser.headings, parser.headings[1:], strict=False)
        )
        page_text = " ".join(parser.text_parts)
        assert all(
            channel in page_text
            for channel in ("大模型", "Agent", "AI 产品", "开源生态", "行业政策")
        )
        assert any(link.get("aria-current") == "page" for link in parser.links)


def test_story_titles_are_links_and_external_sources_announce_new_windows(
    accessibility_report_root: tuple[Path, object],
    tmp_path: Path,
) -> None:
    root, _items = accessibility_report_root
    output = tmp_path / "dist"
    build_site(root, output)

    for page in [
        output / "index.html",
        output / "days/2026-07-26/index.html",
        output / "categories/agent/index.html",
    ]:
        parser = _parse(page)
        visible_text = " ".join(parser.text_parts)
        assert parser.details_count == 0
        assert parser.summary_count == 0
        assert "发生了什么" in visible_text
        assert "为什么重要" in visible_text
        assert "行动" not in visible_text
        assert "原始来源" in visible_text
        assert "新窗口" in visible_text
        assert any(link.get("class") == "story-title-link" for link in parser.article_links)
        external_links = [link for link in parser.article_links if link.get("target") == "_blank"]
        assert external_links
        assert all("新窗口" in link.get("aria-label", "") for link in external_links)


def test_search_controls_are_labelled_and_day_archive_navigation_is_explicit(
    accessibility_report_root: tuple[Path, object],
    tmp_path: Path,
) -> None:
    root, _items = accessibility_report_root
    output = tmp_path / "dist"
    build_site(root, output)

    search = _parse(output / "search/index.html")
    assert search.inputs
    assert search.buttons
    assert any(label.get("for") == "search-query" for label in search.labels)
    assert "polite" in search.aria_live

    day_text = (output / "days/2026-07-26/index.html").read_text(encoding="utf-8")
    assert all(label in day_text for label in ("上一期", "返回归档"))
    archive_text = (output / "archive/index.html").read_text(encoding="utf-8")
    assert all(label in archive_text for label in ("条收录", "条候选"))


def test_styles_define_warm_responsive_newspaper_and_accessibility_contract(
    accessibility_report_root: tuple[Path, object],
    tmp_path: Path,
) -> None:
    root, _items = accessibility_report_root
    output = tmp_path / "dist"
    build_site(root, output)
    css = (output / "assets/styles.css").read_text(encoding="utf-8")

    required_variables = {
        "--paper": "#f3efe6",
        "--paper-deep": "#e8e0d2",
        "--ink": "#191713",
        "--muted": "#69635a",
        "--rule": "#b8afa0",
        "--editorial-red": "#9f2f2a",
        "--max-width": "1180px",
    }
    for name, value in required_variables.items():
        assert f"{name}: {value}" in css
    assert ":focus-visible" in css
    assert "font-size: 15px" in css
    assert ".story-meta" in css and "font-size: 12px" in css
    assert "min-height: 44px" in css
    assert "@media (max-width: 820px)" in css
    assert ".primary-nav::after" in css
    assert ":target" in css
    assert "scroll-margin-top" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "animation: none" in css
    assert "transition: none" in css
    assert "scroll-behavior: auto" in css
    assert "linear-gradient" not in css
    assert "radial-gradient" not in css
    assert "box-shadow" not in css
    assert "text-shadow" not in css
    assert ".news-grid" not in css

    assert (output / "assets/favicon.svg").is_file()
    homepage = (output / "index.html").read_text(encoding="utf-8")
    assert f"{BASE_PATH}assets/favicon.svg" in homepage


def test_javascript_is_safe_progressive_enhancement_without_content_mutation(
    accessibility_report_root: tuple[Path, object],
    tmp_path: Path,
) -> None:
    root, _items = accessibility_report_root
    output = tmp_path / "dist"
    build_site(root, output)
    javascript = (output / "assets/app.js").read_text(encoding="utf-8")

    assert "classList.add" in javascript
    assert "js" in javascript
    assert "innerHTML" not in javascript
    assert "textContent" not in javascript
    assert "eval(" not in javascript
    assert "new Function" not in javascript
    assert f"{BASE_PATH}search.json" not in javascript
