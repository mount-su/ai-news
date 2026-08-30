from __future__ import annotations

import asyncio
import html
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import ai_news.models as models
from ai_news.collectors.github import (
    collect_github_releases,
    parse_github_releases,
    releases_url,
)
from ai_news.models import Category, SourceSpec

FIXTURES = Path(__file__).parent / "fixtures"


def _source() -> SourceSpec:
    return SourceSpec(
        id="github-project",
        name="Example Project",
        kind="github",
        repo="example/project",
        category=Category.OPEN_SOURCE,
        weight=7,
        official=True,
        role="primary",
    )


def test_parse_github_releases_filters_unpublished_items_and_cleans_metadata() -> None:
    payload = (FIXTURES / "github-releases.json").read_bytes()

    items = parse_github_releases(payload, _source())

    assert len(items) == 1
    item = items[0]
    assert item.source_id == "github-project"
    assert item.source_name == "Example Project"
    assert item.source_weight == 7
    assert item.title == "Stable release"
    assert str(item.url) == "https://github.com/example/project/releases/tag/v2.0.0"
    assert item.published_at == datetime(2026, 7, 25, 10, 30, tzinfo=UTC)
    assert item.published_at.tzinfo is not None
    assert item.excerpt == "Production ready release."
    assert item.category_hint == Category.OPEN_SOURCE
    assert item.is_official_source is True
    assert item.source_role is models.SourceRole.PRIMARY
    assert item.discovery_verified is False


def test_parse_github_releases_uses_tag_fallback_and_isolates_invalid_items() -> None:
    releases: list[object] = [
        None,
        {
            "name": "No URL",
            "tag_name": "v0",
            "body": "",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-07-25T10:00:00Z",
        },
        {
            "name": "Insecure URL",
            "tag_name": "v1",
            "body": "",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-07-25T10:00:00Z",
            "html_url": "http://github.com/example/project/releases/tag/v1",
        },
        {
            "name": "Bad time",
            "tag_name": "v2",
            "body": "",
            "draft": False,
            "prerelease": False,
            "published_at": "not-a-time",
            "html_url": "https://github.com/example/project/releases/tag/v2",
        },
        {
            "name": "Malformed URL",
            "tag_name": "v3",
            "body": "",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-07-25T10:00:00Z",
            "html_url": "https://[broken",
        },
        {
            "name": " ",
            "tag_name": "  v4.0.0  ",
            "body": "<p>Fallback\n <em>body</em></p>",
            "draft": False,
            "prerelease": False,
            "published_at": "2026-07-25T12:00:00Z",
            "html_url": "https://github.com/example/project/releases/tag/v4.0.0",
        },
    ]

    items = parse_github_releases(json.dumps(releases).encode(), _source())

    assert [item.title for item in items] == ["v4.0.0"]
    assert items[0].excerpt == "Fallback body"


def test_parse_github_releases_limits_excerpt_to_4000_characters() -> None:
    release = {
        "name": "Long body",
        "tag_name": "v1",
        "body": f"<p>First   section</p> {'x' * 4100}",
        "draft": False,
        "prerelease": False,
        "published_at": "2026-07-25T10:00:00Z",
        "html_url": "https://github.com/example/project/releases/tag/v1",
    }

    items = parse_github_releases(json.dumps([release]).encode(), _source())

    assert len(items) == 1
    assert items[0].excerpt.startswith("First section ")
    assert "<" not in items[0].excerpt
    assert "  " not in items[0].excerpt
    assert len(items[0].excerpt) == 4000


def test_parse_github_releases_decodes_entities_before_stripping_html_without_tag_residue() -> None:
    release = {
        "name": "Entity safety",
        "tag_name": "v1",
        "body": ("&lt;em&gt;single&lt;/em&gt; &amp;lt;em&amp;gt;double&amp;lt;/em&amp;gt; &amp;"),
        "draft": False,
        "prerelease": False,
        "published_at": "2026-07-25T10:00:00Z",
        "html_url": "https://github.com/example/project/releases/tag/v1",
    }

    items = parse_github_releases(json.dumps([release]).encode(), _source())

    assert len(items) == 1
    assert items[0].excerpt == "single double &"
    assert "<em>" not in items[0].excerpt
    assert "<em>" not in html.unescape(items[0].excerpt)


def test_parse_github_releases_skips_incomplete_or_naive_timestamps_and_keeps_valid_item() -> None:
    def release(title: str, published_at: str) -> dict[str, object]:
        return {
            "name": title,
            "tag_name": "v1",
            "body": "",
            "draft": False,
            "prerelease": False,
            "published_at": published_at,
            "html_url": f"https://github.com/example/project/releases/tag/{title}",
        }

    payload = json.dumps(
        [
            release("time-only", "10:00Z"),
            release("date-only", "2026-07-25"),
            release("no-timezone", "2026-07-25T10:00:00"),
            release("complete-timestamp", "2026-07-25T10:00:00Z"),
        ]
    ).encode()

    items = parse_github_releases(payload, _source())

    assert [item.title for item in items] == ["complete-timestamp"]


def test_parse_github_releases_rejects_invalid_json_without_exposing_payload() -> None:
    secret_payload = b'{"private_token":"do-not-expose"'

    with pytest.raises(ValueError, match="valid JSON") as exc_info:
        parse_github_releases(secret_payload, _source())

    assert "do-not-expose" not in str(exc_info.value)
    assert secret_payload.decode() not in repr(exc_info.value)


@pytest.mark.parametrize("payload", [b"{}", b'"not a list"', b"null"])
def test_parse_github_releases_rejects_non_list_json(payload: bytes) -> None:
    with pytest.raises(ValueError, match="JSON list"):
        parse_github_releases(payload, _source())


def test_releases_url_is_exact_github_api_endpoint() -> None:
    assert (
        releases_url(_source())
        == "https://api.github.com/repos/example/project/releases?per_page=10"
    )


def test_github_functions_reject_non_github_source() -> None:
    source = SourceSpec(
        id="regular-feed",
        name="Regular Feed",
        kind="feed",
        url="https://example.com/feed.xml",
        category=Category.OPEN_SOURCE,
    )

    with pytest.raises(ValueError, match="github source"):
        releases_url(source)
    with pytest.raises(ValueError, match="github source"):
        parse_github_releases(b"[]", source)


def test_github_functions_reject_source_without_repo() -> None:
    source = _source().model_copy(update={"repo": None})

    with pytest.raises(ValueError, match="repo"):
        releases_url(source)
    with pytest.raises(ValueError, match="repo"):
        parse_github_releases(b"[]", source)


@pytest.mark.parametrize(
    ("token", "expected_authorization"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("  test-token  ", "Bearer test-token"),
    ],
)
def test_collect_github_releases_sends_required_headers_and_optional_token(
    token: str | None,
    expected_authorization: str | None,
) -> None:
    payload = (FIXTURES / "github-releases.json").read_bytes()
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(200, content=payload, request=request)

    async def exercise() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            items = await collect_github_releases(client, _source(), token=token)
            return [item.title for item in items]

    assert asyncio.run(exercise()) == ["Stable release"]
    assert captured_request is not None
    assert str(captured_request.url) == releases_url(_source())
    assert captured_request.headers["Accept"] == "application/vnd.github+json"
    assert captured_request.headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert captured_request.headers.get("Authorization") == expected_authorization
