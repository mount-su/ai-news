from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ai_news.collectors.official_pages import anthropic, deepseek, meta
from ai_news.collectors.official_pages.base import (
    OfficialEntry,
    OfficialPageStructureError,
)
from ai_news.models import Category, SourceSpec

FIXTURES = Path(__file__).parent / "fixtures" / "official_pages"
Parser = Callable[[bytes, SourceSpec], list[OfficialEntry]]


def _source(
    source_id: str,
    name: str,
    url: str,
    adapter: str,
    weight: int,
) -> SourceSpec:
    return SourceSpec.model_validate(
        {
            "id": source_id,
            "name": name,
            "kind": "official_page",
            "url": url,
            "adapter": adapter,
            "category": Category.MODEL,
            "weight": weight,
            "official": True,
        }
    )


ANTHROPIC = _source(
    "anthropic",
    "Anthropic",
    "https://www.anthropic.com/news",
    "anthropic_news",
    10,
)
META = _source(
    "meta-ai",
    "Meta AI",
    "https://ai.meta.com/blog/",
    "meta_ai_blog",
    9,
)
DEEPSEEK = _source(
    "deepseek",
    "DeepSeek",
    "https://www.deepseek.com/en/news/",
    "deepseek_news",
    10,
)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.parametrize(
    (
        "adapter",
        "fixture_name",
        "source",
        "expected_title",
        "expected_host",
        "expected_date",
    ),
    [
        (
            anthropic.parse,
            "anthropic.html",
            ANTHROPIC,
            "Introducing Claude Opus 5",
            "www.anthropic.com",
            datetime(2026, 7, 24, 7, tzinfo=UTC),
        ),
        (
            meta.parse,
            "meta.html",
            META,
            "Meta AI launches a new product",
            "ai.meta.com",
            datetime(2026, 7, 9, 7, tzinfo=UTC),
        ),
        (
            deepseek.parse,
            "deepseek.html",
            DEEPSEEK,
            "DeepSeek-V4 Preview is live",
            "www.deepseek.com",
            datetime(2026, 4, 23, 16, tzinfo=UTC),
        ),
    ],
)
def test_international_adapter_extracts_verified_entry(
    adapter: Parser,
    fixture_name: str,
    source: SourceSpec,
    expected_title: str,
    expected_host: str,
    expected_date: datetime,
) -> None:
    entries = adapter(fixture(fixture_name), source)

    assert len(entries) == 1
    assert entries[0].title == expected_title
    assert urlsplit(entries[0].url).hostname == expected_host
    assert entries[0].published_at == expected_date
    assert entries[0].excerpt


def test_meta_groups_repeated_links_into_one_article() -> None:
    entries = meta.parse(fixture("meta.html"), META)

    assert len(entries) == 1
    assert entries[0].url == "https://ai.meta.com/blog/meta-ai-product-launch/"


@pytest.mark.parametrize(
    ("adapter", "source"),
    [
        (anthropic.parse, ANTHROPIC),
        (meta.parse, META),
        (deepseek.parse, DEEPSEEK),
    ],
)
def test_international_adapter_raises_on_structure_change(
    adapter: Parser,
    source: SourceSpec,
) -> None:
    with pytest.raises(OfficialPageStructureError):
        adapter(b"<html><body>unrecognized</body></html>", source)


@pytest.mark.parametrize(
    ("adapter", "source"),
    [
        (anthropic.parse, ANTHROPIC),
        (meta.parse, META),
        (deepseek.parse, DEEPSEEK),
    ],
)
def test_international_adapter_raises_on_challenge_page(
    adapter: Parser,
    source: SourceSpec,
) -> None:
    with pytest.raises(OfficialPageStructureError):
        adapter(b"<html><title>Access denied</title><p>CAPTCHA</p></html>", source)


def test_adapter_rejects_source_for_another_parser() -> None:
    with pytest.raises(ValueError, match="anthropic_news source"):
        anthropic.parse(fixture("anthropic.html"), META)
