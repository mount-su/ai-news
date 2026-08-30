from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import httpx

from ai_news.models import Category, RawItem, SourceConfig, SourceRole, SourceSpec
from ai_news.source_health import (
    SourceHealthResult,
    SourceHealthStatus,
    check_source_health,
    format_health_result,
    source_health_succeeded,
)

AS_OF = date(2026, 8, 31)


def _source(
    source_id: str,
    *,
    role: SourceRole,
    enabled: bool = True,
    hostname: str | None = None,
) -> SourceSpec:
    return SourceSpec(
        id=source_id,
        name=source_id.replace("-", " ").title(),
        kind="feed",
        url=f"https://{hostname or f'{source_id}.example'}/feed.xml",
        category=Category.MODEL,
        weight=8,
        official=role is SourceRole.PRIMARY,
        role=role,
        enabled=enabled,
    )


def _reddit() -> SourceSpec:
    return SourceSpec(
        id="reddit-ai",
        name="Reddit AI 线索",
        kind="reddit_rss",
        communities=["LocalLLaMA", "OpenAI"],
        category=Category.TOOL,
        weight=5,
        official=False,
        role=SourceRole.DISCOVERY,
    )


def _raw(source: SourceSpec, published_date: date) -> RawItem:
    return RawItem(
        source_id=source.id,
        source_name=source.name,
        source_weight=source.weight,
        title=f"A useful AI product release from {source.id}",
        url=f"https://{source.id}.example/product",
        published_at=datetime.combine(published_date, datetime.min.time(), tzinfo=UTC),
        excerpt="A concrete product update for users.",
        category_hint=source.category,
        is_official_source=source.official,
        source_role=source.role,
        discovery_verified=source.role is SourceRole.DISCOVERY,
    )


def _result(
    source_id: str,
    *,
    role: SourceRole,
    status: SourceHealthStatus,
) -> SourceHealthResult:
    healthy_statuses = {SourceHealthStatus.FRESH, SourceHealthStatus.STALE}
    return SourceHealthResult(
        source_id=source_id,
        source_name=source_id.title(),
        source_role=role,
        status=status,
        item_count=1 if status in healthy_statuses else 0,
        latest_date=AS_OF if status in healthy_statuses else None,
        error_type=(
            "EmptySourceResult"
            if status is SourceHealthStatus.EMPTY
            else "RuntimeError"
            if status is SourceHealthStatus.FAILED
            else None
        ),
    )


def test_health_check_distinguishes_fresh_stale_empty_and_failed() -> None:
    fresh = _source("fresh-primary", role=SourceRole.PRIMARY)
    stale = _source("stale-primary", role=SourceRole.PRIMARY)
    empty = _source("empty-media", role=SourceRole.TRUSTED_MEDIA)
    failed = _source("failed-media", role=SourceRole.TRUSTED_MEDIA)
    config = SourceConfig(sources=[fresh, stale, empty, failed])

    async def collector(
        _client: httpx.AsyncClient,
        source: SourceSpec,
        _official_domains: frozenset[str],
    ) -> list[RawItem]:
        if source is fresh:
            return [_raw(source, AS_OF - timedelta(days=90))]
        if source is stale:
            return [_raw(source, AS_OF - timedelta(days=91))]
        if source is empty:
            return []
        raise RuntimeError("private response text and URL")

    results = asyncio.run(check_source_health(config, collector=collector, as_of_date=AS_OF))

    assert [result.status for result in results] == [
        SourceHealthStatus.FRESH,
        SourceHealthStatus.STALE,
        SourceHealthStatus.EMPTY,
        SourceHealthStatus.FAILED,
    ]
    assert results[2].item_count == 0
    assert results[2].error_type == "EmptySourceResult"
    assert results[3].error_type == "RuntimeError"
    assert "private" not in repr(results)


def test_media_freshness_threshold_is_seven_days_and_primary_is_ninety() -> None:
    primary_fresh = _source("primary-fresh", role=SourceRole.PRIMARY)
    primary_stale = _source("primary-stale", role=SourceRole.PRIMARY)
    media_fresh = _source("media-fresh", role=SourceRole.TRUSTED_MEDIA)
    media_stale = _source("media-stale", role=SourceRole.TRUSTED_MEDIA)
    dates = {
        primary_fresh.id: AS_OF - timedelta(days=90),
        primary_stale.id: AS_OF - timedelta(days=91),
        media_fresh.id: AS_OF - timedelta(days=7),
        media_stale.id: AS_OF - timedelta(days=8),
    }

    async def collector(
        _client: httpx.AsyncClient,
        source: SourceSpec,
        _official_domains: frozenset[str],
    ) -> list[RawItem]:
        return [_raw(source, dates[source.id])]

    results = asyncio.run(
        check_source_health(
            SourceConfig(sources=[primary_fresh, primary_stale, media_fresh, media_stale]),
            collector=collector,
            as_of_date=AS_OF,
        )
    )

    assert {result.source_id: result.status for result in results} == {
        "primary-fresh": SourceHealthStatus.FRESH,
        "primary-stale": SourceHealthStatus.STALE,
        "media-fresh": SourceHealthStatus.FRESH,
        "media-stale": SourceHealthStatus.STALE,
    }


def test_health_checks_every_enabled_source_and_skips_only_disabled() -> None:
    primary = _source("primary", role=SourceRole.PRIMARY)
    media = _source("media", role=SourceRole.TRUSTED_MEDIA)
    discovery = _source("discovery", role=SourceRole.DISCOVERY)
    disabled = _source("disabled", role=SourceRole.PRIMARY, enabled=False)
    seen: list[str] = []

    async def collector(
        _client: httpx.AsyncClient,
        source: SourceSpec,
        _official_domains: frozenset[str],
    ) -> list[RawItem]:
        seen.append(source.id)
        return [_raw(source, AS_OF)]

    results = asyncio.run(
        check_source_health(
            SourceConfig(sources=[primary, media, discovery, disabled]),
            collector=collector,
            as_of_date=AS_OF,
        )
    )

    assert seen == ["primary", "media", "discovery"]
    assert [result.source_id for result in results] == seen


def test_health_collection_uses_bounded_concurrency_and_keeps_config_order() -> None:
    sources = [_source(f"source-{index}", role=SourceRole.PRIMARY) for index in range(12)]
    active = 0
    peak = 0

    async def collector(
        _client: httpx.AsyncClient,
        source: SourceSpec,
        _official_domains: frozenset[str],
    ) -> list[RawItem]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [_raw(source, AS_OF)]

    results = asyncio.run(
        check_source_health(
            SourceConfig(sources=sources),
            collector=collector,
            as_of_date=AS_OF,
        )
    )

    assert peak == 6
    assert [result.source_id for result in results] == [source.id for source in sources]


def test_discovery_is_advisory_but_required_empty_or_failed_sources_fail_gate() -> None:
    required_stale = _result(
        "required-stale",
        role=SourceRole.PRIMARY,
        status=SourceHealthStatus.STALE,
    )
    discovery_failed = _result(
        "discovery",
        role=SourceRole.DISCOVERY,
        status=SourceHealthStatus.FAILED,
    )
    required_empty = _result(
        "required-empty",
        role=SourceRole.TRUSTED_MEDIA,
        status=SourceHealthStatus.EMPTY,
    )
    required_failed = _result(
        "required-failed",
        role=SourceRole.PRIMARY,
        status=SourceHealthStatus.FAILED,
    )

    assert source_health_succeeded([required_stale, discovery_failed]) is True
    assert source_health_succeeded([required_empty, discovery_failed]) is False
    assert source_health_succeeded([required_failed, discovery_failed]) is False
    assert source_health_succeeded([discovery_failed]) is False


def test_reddit_allowlist_uses_primary_and_trusted_media_hosts_only() -> None:
    primary = _source(
        "primary",
        role=SourceRole.PRIMARY,
        hostname="official.example",
    )
    media = _source(
        "media",
        role=SourceRole.TRUSTED_MEDIA,
        hostname="media.example",
    )
    discovery = _source(
        "discovery",
        role=SourceRole.DISCOVERY,
        hostname="community.example",
    )
    reddit = _reddit()
    observed_allowlists: list[frozenset[str]] = []

    async def collector(
        _client: httpx.AsyncClient,
        source: SourceSpec,
        official_domains: frozenset[str],
    ) -> list[RawItem]:
        observed_allowlists.append(official_domains)
        return [_raw(source, AS_OF)]

    asyncio.run(
        check_source_health(
            SourceConfig(sources=[primary, media, discovery, reddit]),
            collector=collector,
            as_of_date=AS_OF,
        )
    )

    assert observed_allowlists
    assert set(observed_allowlists) == {frozenset({"official.example", "media.example"})}


def test_health_output_contains_only_safe_explicit_fields() -> None:
    line = format_health_result(
        SourceHealthResult(
            source_id="anthropic",
            source_name="Anthropic",
            source_role=SourceRole.PRIMARY,
            status=SourceHealthStatus.STALE,
            item_count=2,
            latest_date=date(2026, 5, 1),
            error_type=None,
        )
    )

    assert line == "anthropic Anthropic primary stale 2 2026-05-01 -"
    assert "https://" not in line
