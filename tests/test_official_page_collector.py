import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ai_news.collectors.official_pages import (
    MAX_DETAIL_FETCHES,
    MAX_INDEX_ENTRIES,
    PARSERS,
    collect_official_page,
)
from ai_news.collectors.official_pages.base import OfficialEntry
from ai_news.http import HttpStatusFetchError
from ai_news.models import Category, SourceSpec

NOW = datetime(2026, 8, 20, 8, tzinfo=UTC)


def _source() -> SourceSpec:
    return SourceSpec(
        id="anthropic",
        name="Anthropic",
        kind="official_page",
        url="https://www.anthropic.com/news",
        adapter="anthropic_news",
        category=Category.MODEL,
        weight=10,
        official=True,
    )


def _entry(number: int, *, excerpt: str) -> OfficialEntry:
    return OfficialEntry(
        title=f"Official product release {number}",
        url=f"https://www.anthropic.com/news/release-{number}",
        published_at=NOW - timedelta(minutes=number),
        excerpt=excerpt,
    )


def test_official_collector_limits_index_and_detail_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    entries = [
        *[_entry(number, excerpt="x" * 100) for number in range(20)],
        *[_entry(number, excerpt="short") for number in range(20, 40)],
    ]
    detail_requests: list[str] = []

    def fake_parser(_payload: bytes, _source: SourceSpec) -> list[OfficialEntry]:
        return entries

    async def fake_fetcher(_client: httpx.AsyncClient, url: str) -> bytes:
        if url != str(source.url):
            detail_requests.append(url)
        return b'<meta property="og:description" content="' + b"d" * 100 + b'">'

    monkeypatch.setitem(PARSERS, "anthropic_news", fake_parser)
    items = asyncio.run(collect_official_page(httpx.AsyncClient(), source, fetcher=fake_fetcher))

    assert MAX_INDEX_ENTRIES == 30
    assert MAX_DETAIL_FETCHES == 8
    assert len(detail_requests) == 8
    assert len(items) == 28


def test_official_collector_drops_only_failed_detail_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    entries = [
        _entry(1, excerpt="complete " * 12),
        _entry(2, excerpt="short"),
        _entry(3, excerpt="short"),
    ]

    def fake_parser(_payload: bytes, _source: SourceSpec) -> list[OfficialEntry]:
        return entries

    async def fake_fetcher(_client: httpx.AsyncClient, url: str) -> bytes:
        if url == entries[1].url:
            raise HttpStatusFetchError(status_code=500, safe_url=url)
        return b'<meta name="description" content="' + b"d" * 100 + b'">'

    monkeypatch.setitem(PARSERS, "anthropic_news", fake_parser)
    items = asyncio.run(collect_official_page(httpx.AsyncClient(), source, fetcher=fake_fetcher))

    assert {item.title for item in items} == {
        "Official product release 1",
        "Official product release 3",
    }


def test_official_collector_drops_detail_that_still_has_no_useful_excerpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()

    def fake_parser(_payload: bytes, _source: SourceSpec) -> list[OfficialEntry]:
        return [_entry(1, excerpt="short")]

    async def fake_fetcher(_client: httpx.AsyncClient, _url: str) -> bytes:
        return b"<html><body>No metadata</body></html>"

    monkeypatch.setitem(PARSERS, "anthropic_news", fake_parser)

    assert (
        asyncio.run(collect_official_page(httpx.AsyncClient(), source, fetcher=fake_fetcher)) == []
    )


def test_official_collector_rejects_wrong_source_kind() -> None:
    source = SourceSpec(
        id="feed-source",
        name="Feed Source",
        kind="feed",
        url="https://example.com/feed.xml",
        category=Category.MODEL,
    )

    with pytest.raises(ValueError, match="official_page collector"):
        asyncio.run(collect_official_page(httpx.AsyncClient(), source))


def test_official_parser_registry_covers_every_allowed_adapter() -> None:
    assert set(PARSERS) == {
        "anthropic_news",
        "meta_ai_blog",
        "deepseek_news",
        "zhipu_releases",
        "minimax_releases",
        "tencent_hunyuan",
        "volcengine_doubao",
        "baidu_ernie",
    }
