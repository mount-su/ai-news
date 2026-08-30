from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit

import httpx

from ai_news.collectors.feed import parse_feed
from ai_news.collectors.official_pages import collect_official_page
from ai_news.collectors.reddit_rss import collect_reddit_rss
from ai_news.http import get_bytes
from ai_news.models import RawItem, SourceConfig, SourceRole, SourceSpec

_FRESHNESS_DAYS = {
    SourceRole.PRIMARY: 90,
    SourceRole.TRUSTED_MEDIA: 7,
    SourceRole.DISCOVERY: 7,
}
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_HEALTH_CONCURRENCY = 6

HealthCollector = Callable[
    [httpx.AsyncClient, SourceSpec, frozenset[str]],
    Awaitable[list[RawItem]],
]


class SourceHealthStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    EMPTY = "empty"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SourceHealthResult:
    source_id: str
    source_name: str
    source_role: SourceRole
    status: SourceHealthStatus
    item_count: int
    latest_date: date | None
    error_type: str | None

    @property
    def healthy(self) -> bool:
        return self.status in {SourceHealthStatus.FRESH, SourceHealthStatus.STALE}


async def _collect_source(
    client: httpx.AsyncClient,
    source: SourceSpec,
    official_domains: frozenset[str],
) -> list[RawItem]:
    if source.kind == "feed":
        if source.url is None:
            raise ValueError("feed source requires a URL")
        payload = await get_bytes(client, str(source.url))
        return parse_feed(payload, source)
    if source.kind == "official_page":
        return await collect_official_page(client, source)
    if source.kind == "reddit_rss":
        return await collect_reddit_rss(
            client,
            source,
            official_domains=official_domains,
        )
    raise ValueError("unsupported source health kind")


def _official_domains(config: SourceConfig) -> frozenset[str]:
    return frozenset(
        hostname
        for source in config.sources
        if source.enabled
        and source.role in {SourceRole.PRIMARY, SourceRole.TRUSTED_MEDIA}
        and source.url is not None
        if (hostname := urlsplit(str(source.url)).hostname) is not None
    )


def _safe_error_name(error: BaseException) -> str:
    error_name = type(error).__name__
    return error_name if _SAFE_ERROR_TYPE.fullmatch(error_name) else "Error"


def _result(
    source: SourceSpec,
    *,
    status: SourceHealthStatus,
    item_count: int,
    latest_date: date | None,
    error_type: str | None,
) -> SourceHealthResult:
    return SourceHealthResult(
        source_id=source.id,
        source_name=source.name,
        source_role=source.role,
        status=status,
        item_count=item_count,
        latest_date=latest_date,
        error_type=error_type,
    )


async def check_source_health(
    config: SourceConfig,
    *,
    collector: HealthCollector | None = None,
    as_of_date: date | None = None,
) -> list[SourceHealthResult]:
    validated = SourceConfig.model_validate(config)
    reference_date = as_of_date or datetime.now(UTC).date()
    targets = [source for source in validated.sources if source.enabled]
    selected_collector = collector or _collect_source
    official_domains = _official_domains(validated)
    semaphore = asyncio.Semaphore(_HEALTH_CONCURRENCY)

    async with httpx.AsyncClient(timeout=20) as client:

        async def check_one(source: SourceSpec) -> SourceHealthResult:
            async with semaphore:
                try:
                    items = await selected_collector(client, source, official_domains)
                    if not isinstance(items, list):
                        raise TypeError("collector result must be a list")
                    if not items:
                        return _result(
                            source,
                            status=SourceHealthStatus.EMPTY,
                            item_count=0,
                            latest_date=None,
                            error_type="EmptySourceResult",
                        )

                    latest_date = max(item.published_at for item in items).date()
                    threshold = reference_date - timedelta(days=_FRESHNESS_DAYS[source.role])
                    status = (
                        SourceHealthStatus.FRESH
                        if latest_date >= threshold
                        else SourceHealthStatus.STALE
                    )
                    return _result(
                        source,
                        status=status,
                        item_count=len(items),
                        latest_date=latest_date,
                        error_type=None,
                    )
                except Exception as error:
                    return _result(
                        source,
                        status=SourceHealthStatus.FAILED,
                        item_count=0,
                        latest_date=None,
                        error_type=_safe_error_name(error),
                    )

        return list(await asyncio.gather(*(check_one(source) for source in targets)))


def format_health_result(result: SourceHealthResult) -> str:
    source_name = " ".join(result.source_name.split())
    latest_date = result.latest_date.isoformat() if result.latest_date is not None else "-"
    error_type = result.error_type or "-"
    return (
        f"{result.source_id} {source_name} {result.source_role.value} "
        f"{result.status.value} {result.item_count} {latest_date} {error_type}"
    )


def source_health_succeeded(results: list[SourceHealthResult]) -> bool:
    """Require every enabled primary/media source to be reachable and non-empty."""

    required = [result for result in results if result.source_role is not SourceRole.DISCOVERY]
    return bool(required) and all(result.healthy for result in required)


__all__ = [
    "SourceHealthResult",
    "SourceHealthStatus",
    "check_source_health",
    "format_health_result",
    "source_health_succeeded",
]
