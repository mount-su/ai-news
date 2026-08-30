from __future__ import annotations

import asyncio
import html
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import ai_news.models as models
from ai_news.collectors.arxiv import collect_arxiv, parse_arxiv
from ai_news.models import Category, SourceSpec

FIXTURES = Path(__file__).parent / "fixtures"


def _source() -> SourceSpec:
    return SourceSpec(
        id="arxiv-ai",
        name="arXiv AI",
        kind="arxiv",
        url="https://export.arxiv.org/api/query?search_query=cat:cs.AI",
        category=Category.RESEARCH,
        weight=8,
        official=False,
        role="trusted_media",
    )


def test_parse_arxiv_returns_clean_valid_item_with_source_metadata() -> None:
    payload = (FIXTURES / "arxiv-feed.xml").read_bytes()

    items = parse_arxiv(payload, _source())

    assert len(items) == 1
    item = items[0]
    assert item.source_id == "arxiv-ai"
    assert item.source_name == "arXiv AI"
    assert item.source_weight == 8
    assert item.title == "Reliable Agents for Research"
    assert str(item.url) == "https://arxiv.org/abs/2607.12345"
    assert item.published_at == datetime(2026, 7, 25, 10, 30, tzinfo=UTC)
    assert item.published_at.tzinfo is not None
    assert item.excerpt == "A practical agent study."
    assert item.category_hint == Category.RESEARCH
    assert item.is_official_source is False
    assert item.source_role is models.SourceRole.TRUSTED_MEDIA
    assert item.discovery_verified is False


def test_parse_arxiv_skips_invalid_entries_and_keeps_later_valid_entry() -> None:
    payload = b"""\
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Mixed arXiv feed</title>
  <entry>
    <title>Missing URL</title><published>2026-07-25T10:00:00Z</published>
  </entry>
  <entry>
    <title>Insecure URL</title><link href="http://arxiv.org/abs/1" />
    <published>2026-07-25T10:00:00Z</published>
  </entry>
  <entry>
    <title> </title><link href="https://arxiv.org/abs/2" />
    <published>2026-07-25T10:00:00Z</published>
  </entry>
  <entry>
    <title>Invalid time</title><link href="https://arxiv.org/abs/3" />
    <published>not-a-time</published>
  </entry>
  <entry>
    <title>Malformed URL</title><link href="https://[broken" />
    <published>2026-07-25T10:00:00Z</published>
  </entry>
  <entry>
    <title>Valid after invalid</title><link href="https://arxiv.org/abs/4" />
    <published>2026-07-25T11:00:00Z</published>
    <summary>Usable paper</summary>
  </entry>
</feed>
"""

    items = parse_arxiv(payload, _source())

    assert [item.title for item in items] == ["Valid after invalid"]


def test_parse_arxiv_limits_clean_excerpt_to_4000_characters() -> None:
    long_text = "x" * 4100
    payload = f"""\
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Long summary</title>
    <link href="https://arxiv.org/abs/2607.00001" />
    <published>2026-07-25T10:00:00Z</published>
    <summary type="html">&lt;p&gt;First   section&lt;/p&gt; {long_text}</summary>
  </entry>
</feed>
""".encode()

    items = parse_arxiv(payload, _source())

    assert len(items) == 1
    assert items[0].excerpt.startswith("First section ")
    assert "<" not in items[0].excerpt
    assert "  " not in items[0].excerpt
    assert len(items[0].excerpt) == 4000


def test_parse_arxiv_decodes_entities_before_stripping_html_without_tag_residue() -> None:
    payload = b"""\
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Entity safety</title>
    <link href="https://arxiv.org/abs/2607.00002" />
    <published>2026-07-25T10:00:00Z</published>
    <summary type="html">&lt;em&gt;single&lt;/em&gt;
      &amp;lt;em&amp;gt;double&amp;lt;/em&amp;gt; &amp;</summary>
  </entry>
</feed>
"""

    items = parse_arxiv(payload, _source())

    assert len(items) == 1
    assert items[0].excerpt == "single double &"
    assert "<em>" not in items[0].excerpt
    assert "<em>" not in html.unescape(items[0].excerpt)


def test_parse_arxiv_skips_incomplete_or_naive_timestamps_and_keeps_valid_item() -> None:
    payload = b"""\
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Time only</title><link href="https://arxiv.org/abs/1" />
    <published>10:00Z</published></entry>
  <entry><title>Date only</title><link href="https://arxiv.org/abs/2" />
    <published>2026-07-25</published></entry>
  <entry><title>No timezone</title><link href="https://arxiv.org/abs/3" />
    <published>2026-07-25T10:00:00</published></entry>
  <entry><title>Complete timestamp</title><link href="https://arxiv.org/abs/4" />
    <published>2026-07-25T10:00:00Z</published></entry>
</feed>
"""

    items = parse_arxiv(payload, _source())

    assert [item.title for item in items] == ["Complete timestamp"]


@pytest.mark.parametrize("payload", [b"", b"not XML", b"<feed><entry>"])
def test_parse_arxiv_returns_empty_for_completely_invalid_payload(payload: bytes) -> None:
    assert parse_arxiv(payload, _source()) == []


def test_parse_arxiv_rejects_non_arxiv_source() -> None:
    source = SourceSpec(
        id="regular-feed",
        name="Regular Feed",
        kind="feed",
        url="https://example.com/feed.xml",
        category=Category.RESEARCH,
    )

    with pytest.raises(ValueError, match="arxiv source"):
        parse_arxiv(b"", source)


def test_parse_arxiv_rejects_source_without_url() -> None:
    source = _source().model_copy(update={"url": None})

    with pytest.raises(ValueError, match="url"):
        parse_arxiv(b"", source)


def test_collect_arxiv_fetches_configured_url_and_parses_response() -> None:
    payload = (FIXTURES / "arxiv-feed.xml").read_bytes()
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, content=payload, request=request)

    async def exercise() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [item.title for item in await collect_arxiv(client, _source())]

    assert asyncio.run(exercise()) == ["Reliable Agents for Research"]
    assert requested_urls == [
        "https://export.arxiv.org/api/query?search_query=cat:cs.AI",
    ]
