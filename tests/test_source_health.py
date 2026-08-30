from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx

from ai_news.models import Category, RawItem, SourceConfig, SourceSpec
from ai_news.source_health import (
    NEW_OFFICIAL_SOURCE_IDS,
    SourceHealthResult,
    check_source_health,
    format_health_result,
    source_health_succeeded,
)


def _anthropic() -> SourceSpec:
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


def _reddit(*, enabled: bool = True) -> SourceSpec:
    return SourceSpec(
        id="reddit-ai",
        name="Reddit AI 线索",
        kind="reddit_rss",
        communities=["LocalLLaMA", "OpenAI"],
        category=Category.TOOL,
        weight=5,
        official=False,
        enabled=enabled,
    )


def _raw(source: SourceSpec) -> RawItem:
    return RawItem(
        source_id=source.id,
        source_name=source.name,
        source_weight=source.weight,
        title="A useful AI product release",
        url="https://www.anthropic.com/news/product",
        published_at=datetime(2026, 8, 20, 1, tzinfo=UTC),
        excerpt="A concrete product update for users.",
        category_hint=source.category,
        is_official_source=source.official,
    )


def test_health_check_requires_nonempty_result_for_every_enabled_source() -> None:
    config = SourceConfig(sources=[_anthropic(), _reddit()])

    async def collector(
        _client: httpx.AsyncClient,
        source: SourceSpec,
        official_domains: frozenset[str],
    ) -> list[RawItem]:
        assert official_domains == frozenset({"www.anthropic.com"})
        return [_raw(source)] if source.id == "anthropic" else []

    results = asyncio.run(
        check_source_health(
            config,
            collector=collector,
            required_official_ids=frozenset({"anthropic"}),
        )
    )

    assert [(result.source_id, result.healthy) for result in results] == [
        ("anthropic", True),
        ("reddit-ai", False),
    ]
    assert results[1].error_type == "EmptySourceResult"


def test_optional_reddit_health_failure_does_not_fail_source_health_gate() -> None:
    results = [
        SourceHealthResult(
            source_id="anthropic",
            healthy=True,
            item_count=1,
            latest_date=datetime(2026, 8, 20, tzinfo=UTC).date(),
            error_type=None,
        ),
        SourceHealthResult(
            source_id="reddit-ai",
            healthy=False,
            item_count=0,
            latest_date=None,
            error_type="HttpStatusFetchError",
        ),
    ]

    assert source_health_succeeded(results) is True


def test_default_source_health_includes_channel_supply_sources() -> None:
    assert {
        "cursor-changelog",
        "sourcegraph-blog",
        "ollama-blog",
        "github-blog-ai",
    } <= NEW_OFFICIAL_SOURCE_IDS


def test_required_source_health_failure_fails_source_health_gate() -> None:
    results = [
        SourceHealthResult(
            source_id="anthropic",
            healthy=False,
            item_count=0,
            latest_date=None,
            error_type="EmptySourceResult",
        ),
    ]

    assert source_health_succeeded(results) is False


def test_health_check_skips_disabled_reddit() -> None:
    config = SourceConfig(sources=[_anthropic(), _reddit(enabled=False)])

    async def collector(
        _client: httpx.AsyncClient,
        source: SourceSpec,
        _official_domains: frozenset[str],
    ) -> list[RawItem]:
        return [_raw(source)]

    results = asyncio.run(
        check_source_health(
            config,
            collector=collector,
            required_official_ids=frozenset({"anthropic"}),
        )
    )

    assert [result.source_id for result in results] == ["anthropic"]
    assert all(result.healthy for result in results)


def test_health_check_marks_missing_or_disabled_required_official_source() -> None:
    config = SourceConfig(sources=[_reddit(enabled=False)])

    results = asyncio.run(
        check_source_health(
            config,
            collector=None,
            required_official_ids=frozenset({"anthropic"}),
        )
    )

    assert results == [
        SourceHealthResult(
            source_id="anthropic",
            healthy=False,
            item_count=0,
            latest_date=None,
            error_type="MissingRequiredSource",
        )
    ]


def test_health_check_retains_only_safe_exception_type() -> None:
    config = SourceConfig(sources=[_anthropic()])

    async def collector(
        _client: httpx.AsyncClient,
        _source: SourceSpec,
        _official_domains: frozenset[str],
    ) -> list[RawItem]:
        raise RuntimeError("private response text and URL")

    results = asyncio.run(
        check_source_health(
            config,
            collector=collector,
            required_official_ids=frozenset({"anthropic"}),
        )
    )

    assert results[0].error_type == "RuntimeError"
    assert "private" not in repr(results)


def test_health_output_contains_only_safe_fields() -> None:
    line = format_health_result(
        SourceHealthResult(
            source_id="anthropic",
            healthy=False,
            item_count=0,
            latest_date=None,
            error_type="HttpStatusFetchError",
        )
    )

    assert line == "anthropic failed 0 - HttpStatusFetchError"
    assert "https://" not in line


def test_health_output_includes_latest_date_for_healthy_source() -> None:
    line = format_health_result(
        SourceHealthResult(
            source_id="anthropic",
            healthy=True,
            item_count=2,
            latest_date=datetime(2026, 8, 20, tzinfo=UTC).date(),
            error_type=None,
        )
    )

    assert line == "anthropic ok 2 2026-08-20 -"
