from datetime import UTC, datetime

import pytest

from ai_news.collectors.official_pages.base import (
    OfficialEntry,
    article_excerpt,
    article_metadata,
    clean_text,
    looks_like_challenge,
    require_allowed_https_url,
    to_raw_item,
    zoned_date,
)
from ai_news.models import Category, SourceSpec


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


def test_clean_text_strips_markup_decodes_entities_and_bounds_excerpt() -> None:
    value = clean_text("<b>A&amp;B</b>\n" + "x" * 5_000)

    assert value.startswith("A&B x")
    assert len(value) == 4_000


def test_require_allowed_https_url_resolves_relative_official_path() -> None:
    assert (
        require_allowed_https_url(
            "/news/claude-opus-5",
            base_url="https://www.anthropic.com/news",
            allowed_hosts={"www.anthropic.com"},
        )
        == "https://www.anthropic.com/news/claude-opus-5"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.anthropic.com/news/item",
        "https://example.com/story",
        "https://www.anthropic.com.evil.example/story",
        "https://user:password@www.anthropic.com/news/item",
    ],
)
def test_require_allowed_https_url_rejects_unsafe_or_unapproved_target(url: str) -> None:
    with pytest.raises(ValueError, match="allowed official host"):
        require_allowed_https_url(
            url,
            base_url="https://www.anthropic.com/news",
            allowed_hosts={"www.anthropic.com"},
        )


def test_zoned_date_converts_local_midnight_to_utc() -> None:
    assert zoned_date("2026-08-20", "Asia/Shanghai") == datetime(
        2026,
        8,
        19,
        16,
        tzinfo=UTC,
    )


def test_article_metadata_requires_explicit_publication_date() -> None:
    html = b"""<html><head><meta property="og:title" content="A useful AI product">
    <meta property="og:description" content="A concrete release with user impact.">
    </head></html>"""

    assert article_metadata(html, "https://www.anthropic.com/news/item") is None


def test_article_metadata_rejects_naive_publication_datetime() -> None:
    html = b"""<html><head><meta property="og:title" content="A useful AI product">
    <meta property="article:published_time" content="2026-08-20T09:00:00">
    </head></html>"""

    assert article_metadata(html, "https://www.anthropic.com/news/item") is None


def test_article_metadata_reads_json_ld_publication_date() -> None:
    html = b"""<script type="application/ld+json">{
      "@context":"https://schema.org","@type":"NewsArticle",
      "headline":"A useful AI product",
      "description":"A concrete release with user impact.",
      "datePublished":"2026-08-20T09:00:00+08:00"
    }</script>"""

    metadata = article_metadata(html, "https://www.anthropic.com/news/item")

    assert metadata == OfficialEntry(
        title="A useful AI product",
        url="https://www.anthropic.com/news/item",
        published_at=datetime(2026, 8, 20, 1, tzinfo=UTC),
        excerpt="A concrete release with user impact.",
    )


def test_article_metadata_reads_article_from_json_ld_graph() -> None:
    html = b"""<script type="application/ld+json">{
      "@context":"https://schema.org","@graph":[
        {"@type":"Organization","name":"Publisher"},
        {"@type":["Thing","Article"],"headline":"Graph article",
         "description":"A relevant product article from a trusted publisher.",
         "datePublished":"2026-08-20T01:00:00Z"}
      ]
    }</script>"""

    metadata = article_metadata(html, "https://www.anthropic.com/news/graph")

    assert metadata is not None
    assert metadata.title == "Graph article"
    assert metadata.published_at == datetime(2026, 8, 20, 1, tzinfo=UTC)


def test_article_metadata_uses_open_graph_when_json_ld_is_invalid() -> None:
    html = b"""<script type="application/ld+json">not-json</script>
    <meta property="og:title" content="Open graph article">
    <meta property="og:description" content="A useful model product update for customers.">
    <meta property="article:published_time" content="2026-08-20T09:00:00+08:00">"""

    metadata = article_metadata(html, "https://www.anthropic.com/news/open-graph")

    assert metadata is not None
    assert metadata.title == "Open graph article"
    assert metadata.excerpt == "A useful model product update for customers."


def test_article_excerpt_does_not_invent_a_publication_date() -> None:
    html = b"""<meta property="og:description" content="A concrete release with user impact.">"""

    assert article_excerpt(html) == "A concrete release with user impact."


def test_article_excerpt_prefers_a_useful_article_paragraph_over_truncated_metadata() -> None:
    html = b"""<html><head>
    <meta property="og:description" content="A truncated release summary [...]">
    </head><body><article><div class="entry-content">
      <p>This official release gives customers a practical AI product with a clear workflow,
      broad availability, and concrete guidance for getting started today.</p>
    </div></article></body></html>"""

    assert article_excerpt(html).startswith(
        "This official release gives customers a practical AI product"
    )


def test_article_excerpt_reads_the_first_useful_paragraph_after_an_unscoped_heading() -> None:
    html = b"""<html><body><div><h1>Official model launch</h1>
    <div><p>The new multimodal model is available today through a public product API,
    improves everyday agent workflows, and includes clear guidance for customers.</p></div>
    </div></body></html>"""

    assert article_excerpt(html).startswith(
        "The new multimodal model is available today through a public product API"
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"<html><title>Access denied</title></html>",
        b"<div id='cf-chl-widget'>Verify you are human</div>",
        b"<form>CAPTCHA</form>",
    ],
)
def test_looks_like_challenge_recognizes_block_pages(payload: bytes) -> None:
    assert looks_like_challenge(payload) is True


def test_looks_like_challenge_ignores_captcha_code_in_a_normal_page() -> None:
    payload = b"""<html><head><title>AI at Meta Blog</title>
    <script>const captchaTelemetry = {enabled: false};</script></head>
    <body><main><h1>AI at Meta Blog</h1></main></body></html>"""

    assert looks_like_challenge(payload) is False


def test_to_raw_item_preserves_source_metadata_and_bounds_excerpt() -> None:
    source = _source()
    entry = OfficialEntry(
        title="Claude product release",
        url="https://www.anthropic.com/news/claude-product",
        published_at=datetime(2026, 8, 20, 1, tzinfo=UTC),
        excerpt="x" * 5_000,
    )

    item = to_raw_item(entry, source)

    assert item.source_id == "anthropic"
    assert item.source_name == "Anthropic"
    assert item.source_weight == 10
    assert item.category_hint is Category.MODEL
    assert item.is_official_source is True
    assert len(item.excerpt) == 4_000
