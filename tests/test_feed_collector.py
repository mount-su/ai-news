from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import ai_news.models as models
from ai_news.collectors.feed import parse_feed
from ai_news.models import Category, SourceSpec

FIXTURES = Path(__file__).parent / "fixtures"


def _source() -> SourceSpec:
    return SourceSpec(
        id="openai-news",
        name="OpenAI News",
        kind="feed",
        url="https://example.com/feed.xml",
        category=Category.MODEL,
        weight=9,
        official=True,
        role="primary",
    )


def test_parse_rss_returns_valid_item_with_source_metadata() -> None:
    payload = (FIXTURES / "openai-feed.xml").read_bytes()

    items = parse_feed(payload, _source())

    assert len(items) == 1
    item = items[0]
    assert item.source_id == "openai-news"
    assert item.source_name == "OpenAI News"
    assert item.source_weight == 9
    assert item.title == "Model launch"
    assert str(item.url) == "https://openai.com/index/model-launch/"
    assert item.published_at == datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
    assert item.published_at.tzinfo is not None
    assert item.excerpt == "A model update"
    assert item.category_hint == Category.MODEL
    assert item.is_official_source is True
    assert item.source_role is models.SourceRole.PRIMARY
    assert item.discovery_verified is False


def test_discovery_feed_preserves_role_without_marking_item_verified() -> None:
    payload = (FIXTURES / "openai-feed.xml").read_bytes()
    source = SourceSpec(
        id="producthunt-ai",
        name="Product Hunt AI",
        kind="feed",
        url="https://www.producthunt.com/feed?category=artificial-intelligence",
        category=Category.TOOL,
        weight=5,
        official=False,
        role="discovery",
    )

    items = parse_feed(payload, source)

    assert len(items) == 1
    assert items[0].source_role is models.SourceRole.DISCOVERY
    assert items[0].discovery_verified is False


def test_invalid_entries_are_skipped_without_discarding_valid_entries() -> None:
    payload = b"""\
<rss version="2.0"><channel><title>Mixed</title>
  <item><title>Missing URL</title><pubDate>Sat, 25 Jul 2026 10:00:00 GMT</pubDate></item>
  <item><title>Insecure URL</title><link>http://example.com/insecure</link>
    <pubDate>Sat, 25 Jul 2026 10:00:00 GMT</pubDate></item>
  <item><title>Missing Time</title><link>https://example.com/no-time</link></item>
  <item><title>Invalid Time</title><link>https://example.com/bad-time</link>
    <pubDate>not a date</pubDate></item>
  <item><title> </title><link>https://example.com/no-title</link>
    <pubDate>Sat, 25 Jul 2026 10:00:00 GMT</pubDate></item>
  <item><title>Valid Entry</title><link>https://example.com/valid</link>
    <pubDate>Sat, 25 Jul 2026 12:00:00 GMT</pubDate></item>
</channel></rss>
"""

    items = parse_feed(payload, _source())

    assert [item.title for item in items] == ["Valid Entry"]


def test_malformed_url_entry_is_skipped_without_losing_later_valid_entry() -> None:
    payload = b"""\
<rss version="2.0"><channel><title>Malformed URL</title>
  <item><title>Broken URL</title><link>https://[broken</link>
    <pubDate>Sat, 25 Jul 2026 10:00:00 GMT</pubDate></item>
  <item><title>Valid after broken</title><link>https://example.com/valid</link>
    <pubDate>Sat, 25 Jul 2026 11:00:00 GMT</pubDate></item>
</channel></rss>
"""

    items = parse_feed(payload, _source())

    assert [item.title for item in items] == ["Valid after broken"]


def test_atom_timestamp_with_offset_is_normalized_to_utc() -> None:
    payload = b"""\
<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <title>Atom launch</title>
    <link href="https://example.com/atom-launch" />
    <updated>2026-07-25T18:00:00+08:00</updated>
    <summary type="html">&lt;p&gt;Atom update&lt;/p&gt;</summary>
  </entry>
</feed>
"""

    items = parse_feed(payload, _source())

    assert len(items) == 1
    assert items[0].published_at == datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
    assert items[0].excerpt == "Atom update"


def test_excerpt_strips_html_collapses_whitespace_and_is_limited_to_4000_characters() -> None:
    long_text = "x" * 4100
    payload = f"""\
<rss version="2.0"><channel><title>Excerpt</title>
  <item>
    <title>Long excerpt</title>
    <link>https://example.com/long</link>
    <pubDate>Sat, 25 Jul 2026 10:00:00 GMT</pubDate>
    <description><![CDATA[<p>  First
      <strong>section</strong> </p> <script>hidden()</script> {long_text}]]></description>
  </item>
</channel></rss>
""".encode()

    items = parse_feed(payload, _source())

    assert len(items) == 1
    assert items[0].excerpt.startswith("First section ")
    assert "hidden()" not in items[0].excerpt
    assert "<" not in items[0].excerpt
    assert "  " not in items[0].excerpt
    assert "\n" not in items[0].excerpt
    assert len(items[0].excerpt) == 4000


@pytest.mark.parametrize(
    "payload",
    [
        b"<rss><channel><item><title>No fields</title></item></channel></rss>",
        b"this is not XML or a feed",
        b"",
    ],
)
def test_payload_without_usable_entries_returns_empty_list(payload: bytes) -> None:
    assert parse_feed(payload, _source()) == []
