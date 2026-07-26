from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts/validate_site.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("validate_site", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
validate_site_module = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = validate_site_module
SCRIPT_SPEC.loader.exec_module(validate_site_module)
main = validate_site_module.main
validate_site = validate_site_module.validate_site

BASE_PATH = "/ai-news/"


def _write_site(root: Path, files: dict[str, str]) -> Path:
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _page(body: str, *, main: bool = True) -> str:
    content = f'<main id="main-content">{body}</main>' if main else body
    return f"<!doctype html><html><body>{content}</body></html>"


def test_valid_site_handles_queries_fragments_directory_indexes_and_percent_encoding(
    tmp_path: Path,
) -> None:
    dist = _write_site(
        tmp_path / "dist",
        {
            "index.html": _page(
                """
                <link rel="stylesheet" href="/ai-news/assets/styles.css?v=1">
                <script src="/ai-news/assets/app.js#runtime"></script>
                <a href="#main-content">same page</a>
                <a href="/ai-news/about/?view=full#details">about</a>
                <a href="/ai-news/pages/%E4%BD%A0%E5%A5%BD/?q=1#%E9%83%A8%E5%88%86">encoded</a>
                <a href="http://example.com/out">external http</a>
                <a href="https://example.com/out">external</a>
                <a href="//cdn.example.com/library.js">cdn</a>
                <a href="mailto:editor@example.com">mail</a>
                <a href="tel:+861234">telephone</a>
                """
            ),
            "about/index.html": _page('<section id="details">About</section>'),
            "pages/你好/index.html": _page('<section id="部分">Encoded</section>'),
            "assets/styles.css": "body { color: white; }\n",
            "assets/app.js": '"use strict";\n',
        },
    )

    assert validate_site(dist, BASE_PATH) == []


def test_relative_percent_encoded_query_and_fragment_delimiters_remain_path_data(
    tmp_path: Path,
) -> None:
    dist = _write_site(
        tmp_path / "dist",
        {
            "index.html": _page(
                """
                <a href="pages/name%3Fpart/">question mark path</a>
                <a href="pages/name%23part/">hash path</a>
                """
            ),
            "pages/name?part/index.html": _page("<p>Question mark</p>"),
            "pages/name#part/index.html": _page("<p>Hash</p>"),
        },
    )

    assert validate_site(dist, BASE_PATH) == []


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "///ai-news/assets/app.js",
        "javascript:alert(1)",
        "data:text/html,unsafe",
        "vbscript:msgbox(1)",
    ],
)
def test_validator_rejects_authority_ambiguity_and_dangerous_schemes(
    tmp_path: Path,
    unsafe_url: str,
) -> None:
    dist = _write_site(
        tmp_path / "dist",
        {
            "index.html": _page(f'<a href="{unsafe_url}">unsafe</a>'),
            "assets/app.js": '"use strict";\n',
        },
    )

    issues = validate_site(dist, BASE_PATH)

    assert "invalid-url" in {issue.code for issue in issues}


def test_validator_rejects_invalid_percent_encoding_in_query(tmp_path: Path) -> None:
    dist = _write_site(
        tmp_path / "dist",
        {"index.html": _page('<a href="/ai-news/?q=%ZZ">bad query</a>')},
    )

    issues = validate_site(dist, BASE_PATH)

    assert "invalid-url" in {issue.code for issue in issues}


def test_validation_issue_render_escapes_control_characters_in_path(tmp_path: Path) -> None:
    dist = _write_site(
        tmp_path / "dist",
        {"unsafe\x1b[2J.html": _page("<p>missing main</p>", main=False)},
    )

    issues = validate_site(dist, BASE_PATH)

    assert len(issues) == 1
    rendered = issues[0].render()
    assert "\x1b" not in rendered
    assert "\\u001b" in rendered


@pytest.mark.parametrize(
    ("files", "expected_code"),
    [
        (
            {
                "index.html": _page('<a href="/ai-news/missing/">missing page</a>'),
            },
            "missing-target",
        ),
        (
            {
                "index.html": _page('<link rel="stylesheet" href="/ai-news/assets/missing.css">'),
            },
            "missing-target",
        ),
        (
            {
                "index.html": _page('<section id="duplicate"></section><p id="duplicate"></p>'),
            },
            "duplicate-id",
        ),
        (
            {
                "index.html": _page("<p>no main</p>", main=False),
            },
            "main-count",
        ),
        (
            {
                "index.html": _page('<script src="/assets/app.js"></script>'),
                "assets/app.js": '"use strict";\n',
            },
            "base-path-escape",
        ),
        (
            {
                "index.html": _page('<script src="/ai-news/%2e%2e/private/app.js"></script>'),
                "assets/app.js": '"use strict";\n',
            },
            "base-path-escape",
        ),
    ],
)
def test_validator_reports_broken_site_contracts(
    tmp_path: Path,
    files: dict[str, str],
    expected_code: str,
) -> None:
    dist = _write_site(tmp_path / "dist", files)

    issues = validate_site(dist, BASE_PATH)

    assert expected_code in {issue.code for issue in issues}
    assert all(issue.path for issue in issues)


def test_validator_reports_missing_same_page_and_cross_page_fragments(tmp_path: Path) -> None:
    dist = _write_site(
        tmp_path / "dist",
        {
            "index.html": _page(
                """
                <a href="#not-here">same</a>
                <a href="/ai-news/about/#also-missing">other</a>
                """
            ),
            "about/index.html": _page("<p>About</p>"),
        },
    )

    issues = validate_site(dist, BASE_PATH)

    fragment_issues = [issue for issue in issues if issue.code == "missing-fragment"]
    assert len(fragment_issues) == 2


def test_cli_prints_clear_paths_and_exits_one_for_invalid_site(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dist = _write_site(
        tmp_path / "dist",
        {"index.html": _page('<a href="/ai-news/not-found/">broken</a>')},
    )

    assert main([str(dist), "--base-path", BASE_PATH]) == 1

    captured = capsys.readouterr()
    assert "index.html:" in captured.err
    assert "missing-target" in captured.err
    assert "broken" not in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("base_path", ["", "ai-news/", "/ai-news", "//ai-news/", "/../x/"])
def test_invalid_base_path_is_rejected_without_reading_site(
    tmp_path: Path,
    base_path: str,
) -> None:
    with pytest.raises(ValueError, match="base_path"):
        validate_site(tmp_path / "missing", base_path)
