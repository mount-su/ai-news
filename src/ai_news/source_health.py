from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit

import httpx

from ai_news.collectors.feed import parse_feed
from ai_news.collectors.official_pages import collect_official_page
from ai_news.collectors.reddit_rss import collect_reddit_rss
from ai_news.http import get_bytes
from ai_news.models import RawItem, SourceConfig, SourceSpec

NEW_OFFICIAL_SOURCE_IDS = frozenset(
    {
        "anthropic",
        "baidu-ernie",
        "deepseek",
        "meta-ai",
        "minimax",
        "mistral",
        "qwen",
        "tencent-hunyuan",
        "volcengine-doubao",
        "zhipu-glm",
    }
)

HealthCollector = Callable[
    [httpx.AsyncClient, SourceSpec, frozenset[str]],
    Awaitable[list[RawItem]],
]


@dataclass(frozen=True, slots=True)
class SourceHealthResult:
    source_id: str
    healthy: bool
    item_count: int
    latest_date: date | None
    error_type: str | None


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
        if source.enabled and source.official and source.url is not None
        if (hostname := urlsplit(str(source.url)).hostname) is not None
    )


def _failed(source_id: str, error_type: str) -> SourceHealthResult:
    return SourceHealthResult(
        source_id=source_id,
        healthy=False,
        item_count=0,
        latest_date=None,
        error_type=error_type,
    )


async def check_source_health(
    config: SourceConfig,
    *,
    collector: HealthCollector | None = None,
    required_official_ids: frozenset[str] | None = None,
) -> list[SourceHealthResult]:
    validated = SourceConfig.model_validate(config)
    required_ids = required_official_ids or NEW_OFFICIAL_SOURCE_IDS
    source_by_id = {source.id: source for source in validated.sources}
    results = [
        _failed(source_id, "MissingRequiredSource")
        for source_id in sorted(required_ids - source_by_id.keys())
    ]

    targets: list[SourceSpec] = []
    for source in validated.sources:
        if source.id in required_ids:
            if source.enabled:
                targets.append(source)
            else:
                results.append(_failed(source.id, "DisabledRequiredSource"))
        elif source.id == "reddit-ai" and source.enabled:
            targets.append(source)

    selected_collector = collector or _collect_source
    official_domains = _official_domains(validated)
    async with httpx.AsyncClient(timeout=20) as client:
        for source in targets:
            try:
                items = await selected_collector(client, source, official_domains)
                if not isinstance(items, list):
                    raise TypeError("collector result must be a list")
                if not items:
                    results.append(_failed(source.id, "EmptySourceResult"))
                    continue
                latest_date = max(item.published_at for item in items).date()
                results.append(
                    SourceHealthResult(
                        source_id=source.id,
                        healthy=True,
                        item_count=len(items),
                        latest_date=latest_date,
                        error_type=None,
                    )
                )
            except Exception as error:
                results.append(_failed(source.id, type(error).__name__[:128]))
    return results


def format_health_result(result: SourceHealthResult) -> str:
    status = "ok" if result.healthy else "failed"
    latest_date = result.latest_date.isoformat() if result.latest_date is not None else "-"
    error_type = result.error_type or "-"
    return f"{result.source_id} {status} {result.item_count} {latest_date} {error_type}"


__all__ = [
    "NEW_OFFICIAL_SOURCE_IDS",
    "SourceHealthResult",
    "check_source_health",
    "format_health_result",
]
