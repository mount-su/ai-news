from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ai_news.models import Category, RawItem
from ai_news.pipeline.editorial import is_editorially_eligible
from ai_news.pipeline.normalize import to_candidate


def _candidate(title: str, excerpt: str = "", source_id: str = "source-test"):
    return to_candidate(
        RawItem(
            source_id=source_id,
            source_name="Test source",
            source_weight=7,
            title=title,
            url="https://example.com/editorial",
            published_at=datetime(2026, 8, 1, 8, tzinfo=UTC),
            excerpt=excerpt,
            category_hint=Category.MODEL,
            is_official_source=True,
        )
    )


@pytest.mark.parametrize(
    "title",
    [
        "arXiv: A new benchmark for multimodal models",
        "SDK v2.4.0 adds a streaming API",
        "Open-source framework introduces a new agent runtime",
        "Research paper studies alignment behavior",
        "Version 1.2.3 fixes connector timeout",
        "Company raises $100M in latest funding round",
    ],
)
def test_editorial_filter_rejects_technical_academic_and_unimpactful_corporate_news(
    title: str,
) -> None:
    assert is_editorially_eligible(_candidate(title)) is False


@pytest.mark.parametrize(
    "title,excerpt",
    [
        ("ChatGPT launches a new consumer plan", "The plan is available to all users today."),
        ("AI assistant cuts subscription price", "The monthly price changes for customers."),
        ("Major retailer deploys AI shopping assistant", "Millions of shoppers can use it."),
        (
            "Government publishes new AI platform rules",
            "The rules change what providers must disclose.",
        ),
        (
            "Company raises funding to expand free access",
            "The investment will expand the product's free tier.",
        ),
    ],
)
def test_editorial_filter_accepts_concrete_user_or_market_changes(
    title: str,
    excerpt: str,
) -> None:
    assert is_editorially_eligible(_candidate(title, excerpt)) is True


@pytest.mark.parametrize(
    "title",
    [
        "LangChain core 1.5.5 release fixes tool validation",
        "LangChain v0.2.8 changelog adds provider metadata",
        "LangChain patch fixes streaming reasoning content",
    ],
)
def test_editorial_filter_rejects_github_release_details(title: str) -> None:
    assert is_editorially_eligible(_candidate(title, source_id="github-langchain")) is False
