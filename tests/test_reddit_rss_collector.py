from __future__ import annotations

import asyncio
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ai_news.collectors.official_pages.base import OfficialEntry
from ai_news.collectors.reddit_rss import (
    MAX_EXTERNAL_FETCHES,
    REQUEST_SPACING_SECONDS,
    RedditEntry,
    RedditFeedStructureError,
    build_reddit_item,
    collect_reddit_rss,
    parse_reddit_atom,
    reddit_feed_urls,
)
from ai_news.http import HttpStatusFetchError
from ai_news.models import Category, SourceSpec

FIXTURES = Path(__file__).parent / "fixtures" / "reddit"


def _source() -> SourceSpec:
    return SourceSpec(
        id="reddit-ai",
        name="Reddit AI 线索",
        kind="reddit_rss",
        communities=[
            "LocalLLaMA",
            "OpenAI",
            "ClaudeAI",
            "GeminiAI",
            "artificial",
            "ChatGPT",
            "aiagents",
            "perplexity_ai",
            "StableDiffusion",
        ],
        category=Category.TOOL,
        weight=5,
        official=False,
    )


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_reddit_feed_urls_are_two_combined_fixed_feeds() -> None:
    source = _source()
    joined = "+".join(source.communities)

    assert reddit_feed_urls(source.communities) == (
        f"https://www.reddit.com/r/{joined}/new/.rss?limit=100",
        f"https://www.reddit.com/r/{joined}/top/.rss?sort=top&t=day&limit=100",
    )


def test_parse_reddit_atom_keeps_only_eligible_external_link_and_community() -> None:
    entries = parse_reddit_atom(fixture("new.atom"), _source())

    assert [(entry.community, entry.external_url) for entry in entries] == [
        ("LocalLLaMA", "https://www.anthropic.com/news/claude-product")
    ]


def test_reddit_entry_never_stores_user_body_comments_or_metrics() -> None:
    assert {field.name for field in fields(RedditEntry)} == {
        "community",
        "post_id",
        "title",
        "external_url",
        "posted_at",
    }


def test_build_reddit_item_uses_verified_original_metadata() -> None:
    entry = parse_reddit_atom(fixture("new.atom"), _source())[0]
    metadata = OfficialEntry(
        title="Anthropic launches a useful Claude AI product",
        url=entry.external_url,
        published_at=datetime(2026, 8, 20, 1, tzinfo=UTC),
        excerpt="The Claude AI product changes how customers complete practical work.",
    )

    item = build_reddit_item(entry, metadata, _source(), {"anthropic.com"})

    assert item is not None
    assert item.source_id == "reddit-ai"
    assert item.source_name == "Reddit · r/LocalLLaMA"
    assert item.source_weight == 5
    assert str(item.url) == "https://www.anthropic.com/news/claude-product"
    assert item.published_at == metadata.published_at
    assert item.is_official_source is False


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        OfficialEntry(
            title="Ordinary database release",
            url="https://www.anthropic.com/news/database",
            published_at=datetime(2026, 8, 20, 1, tzinfo=UTC),
            excerpt="This item changes an ordinary database maintenance process.",
        ),
        OfficialEntry(
            title="Anthropic launches a useful Claude AI product",
            url="https://unknown.example/news/ai-product",
            published_at=datetime(2026, 8, 20, 1, tzinfo=UTC),
            excerpt="A useful Claude AI product update.",
        ),
    ],
)
def test_build_reddit_item_rejects_missing_wrong_topic_or_unknown_domain(
    metadata: OfficialEntry | None,
) -> None:
    entry = parse_reddit_atom(fixture("new.atom"), _source())[0]

    assert build_reddit_item(entry, metadata, _source(), {"anthropic.com"}) is None


def test_collect_reddit_requests_feeds_sequentially_and_deduplicates_links() -> None:
    source = _source()
    feed_urls = reddit_feed_urls(source.communities)
    events: list[str] = []

    async def fake_fetcher(_client: httpx.AsyncClient, url: str) -> bytes:
        events.append(url)
        if url == feed_urls[0]:
            return fixture("new.atom")
        if url == feed_urls[1]:
            return fixture("top.atom")
        return fixture("article.html")

    async def fake_sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds}")

    async def run() -> list[object]:
        async with httpx.AsyncClient() as client:
            return await collect_reddit_rss(
                client,
                source,
                official_domains={"anthropic.com"},
                fetcher=fake_fetcher,
                sleep=fake_sleep,
            )

    items = asyncio.run(run())

    assert events[:3] == [feed_urls[0], "sleep:20", feed_urls[1]]
    assert REQUEST_SPACING_SECONDS == 20
    assert [str(item.url) for item in items] == [
        "https://www.anthropic.com/news/claude-product",
        "https://techcrunch.com/2026/08/20/generative-ai-product/",
    ]


def test_collect_reddit_limits_external_article_fetches_to_eight() -> None:
    source = _source()
    feed_urls = reddit_feed_urls(source.communities)
    entries = "".join(
        f"""<entry><category term="LocalLLaMA"/><id>t3_{index}</id>
        <published>2026-08-20T01:00:00Z</published>
        <title>Claude AI product release {index}</title>
        <link href="https://www.reddit.com/r/LocalLLaMA/comments/{index}/story/"/>
        <content type="html">&lt;a href="https://www.anthropic.com/news/product-{index}"&gt;[link]&lt;/a&gt;</content>
        </entry>"""
        for index in range(10)
    )
    new_feed = f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'.encode()
    empty_feed = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    external_requests: list[str] = []

    async def fake_fetcher(_client: httpx.AsyncClient, url: str) -> bytes:
        if url == feed_urls[0]:
            return new_feed
        if url == feed_urls[1]:
            return empty_feed
        external_requests.append(url)
        return fixture("article.html")

    async def fake_sleep(_seconds: float) -> None:
        return None

    async def run() -> list[object]:
        async with httpx.AsyncClient() as client:
            return await collect_reddit_rss(
                client,
                source,
                official_domains={"anthropic.com"},
                fetcher=fake_fetcher,
                sleep=fake_sleep,
            )

    items = asyncio.run(run())

    assert MAX_EXTERNAL_FETCHES == 8
    assert len(external_requests) == 8
    assert len(items) == 8


def test_collect_reddit_propagates_feed_failure_without_html_fallback() -> None:
    source = _source()

    async def fake_fetcher(_client: httpx.AsyncClient, url: str) -> bytes:
        raise HttpStatusFetchError(status_code=429, safe_url=url)

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            await collect_reddit_rss(
                client,
                source,
                official_domains={"anthropic.com"},
                fetcher=fake_fetcher,
            )

    with pytest.raises(HttpStatusFetchError):
        asyncio.run(run())


def test_parse_reddit_atom_rejects_invalid_structure() -> None:
    with pytest.raises(RedditFeedStructureError):
        parse_reddit_atom(b"not an atom feed", _source())
