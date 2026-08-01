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
        self.text_parts: list[str] = []
        self._in_body = False

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

    def handle_endtag(self, tag: str) -> None:
        if tag == "body":
            self._in_body = False

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
        assert parser.main_ids == ["main-content"]
        assert parser.body_start_tags[0][0] == "a"
        assert parser.body_start_tags[0][1].get("href") == "#main-content"
        assert {"header", "nav", "section", "footer"} <= parser.landmarks
        assert parser.headings[0] == 1
        assert all(
            current <= previous + 1
            for previous, current in zip(parser.headings, parser.headings[1:], strict=False)
        )


def test_concise_brief_cards_are_accessible_without_javascript(
    accessibility_report_root: tuple[Path, object],
    tmp_path: Path,
) -> None:
    root, _items = accessibility_report_root
    output = tmp_path / "dist"
    build_site(root, output)

    for page in [
        output / "index.html",
        output / "days/2026-07-26/index.html",
        output / "categories/coding-agent/index.html",
    ]:
        parser = _parse(page)
        visible_text = " ".join(parser.text_parts)
        assert not parser.inputs
        assert not parser.buttons
        assert parser.details_count == 0
        assert parser.summary_count == 0
        assert "变化" in visible_text
        assert "影响" in visible_text
        assert "行动" not in visible_text
        assert "查看原始来源" in visible_text


def test_styles_define_visual_system_responsive_layout_and_accessibility_contract(
    accessibility_report_root: tuple[Path, object],
    tmp_path: Path,
) -> None:
    root, _items = accessibility_report_root
    output = tmp_path / "dist"
    build_site(root, output)
    css = (output / "assets/styles.css").read_text(encoding="utf-8")

    required_variables = {
        "--ink": "#07111f",
        "--panel": "#0c1b2d",
        "--panel-strong": "#10253d",
        "--line": "#23415d",
        "--text": "#edf8ff",
        "--muted": "#9ab3c7",
        "--signal": "#52e3ff",
        "--amber": "#ffbd59",
        "--danger": "#ff7d8a",
        "--max-width": "1240px",
    }
    for name, value in required_variables.items():
        assert f"{name}: {value}" in css
    assert ":focus-visible" in css
    assert "min-height: 44px" in css
    assert "@media (min-width: 900px)" in css
    assert "@media (max-width: 719px)" in css
    assert "@media (max-width: 600px)" in css
    assert "grid-template-columns: repeat(2" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "animation: none" in css
    assert "transition: none" in css
    assert "scroll-behavior: auto" in css


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
