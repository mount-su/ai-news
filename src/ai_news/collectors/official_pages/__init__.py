from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

import httpx
from pydantic import ValidationError

from ai_news.collectors.official_pages import (
    anthropic,
    baidu,
    deepseek,
    meta,
    minimax,
    tencent,
    volcengine,
    zhipu,
)
from ai_news.collectors.official_pages.base import (
    OfficialEntry,
    OfficialPageStructureError,
    article_excerpt,
    looks_like_challenge,
    to_raw_item,
)
from ai_news.http import HttpFetchError, get_bytes
from ai_news.models import OfficialPageAdapter, RawItem, SourceSpec

Parser = Callable[[bytes, SourceSpec], list[OfficialEntry]]
Fetcher = Callable[[httpx.AsyncClient, str], Awaitable[bytes]]

PARSERS: dict[OfficialPageAdapter, Parser] = {
    "anthropic_news": anthropic.parse,
    "meta_ai_blog": meta.parse,
    "deepseek_news": deepseek.parse,
    "zhipu_releases": zhipu.parse,
    "minimax_releases": minimax.parse,
    "tencent_hunyuan": tencent.parse,
    "volcengine_doubao": volcengine.parse,
    "baidu_ernie": baidu.parse,
}
MAX_INDEX_ENTRIES = 30
MAX_DETAIL_FETCHES = 8
MIN_INDEX_EXCERPT_LENGTH = 80


async def collect_official_page(
    client: httpx.AsyncClient,
    source: SourceSpec,
    *,
    fetcher: Fetcher = get_bytes,
) -> list[RawItem]:
    if source.kind != "official_page" or source.url is None or source.adapter is None:
        raise ValueError("official_page collector requires a valid source")

    payload = await fetcher(client, str(source.url))
    entries = PARSERS[source.adapter](payload, source)[:MAX_INDEX_ENTRIES]
    items: list[RawItem] = []
    detail_fetches = 0
    for entry in entries:
        selected_entry = entry
        if len(entry.excerpt) < MIN_INDEX_EXCERPT_LENGTH:
            if detail_fetches == MAX_DETAIL_FETCHES:
                continue
            detail_fetches += 1
            try:
                detail_payload = await fetcher(client, entry.url)
                if looks_like_challenge(detail_payload):
                    continue
                excerpt = article_excerpt(detail_payload)
            except (HttpFetchError, OverflowError, TypeError, ValueError):
                continue
            if len(excerpt) < MIN_INDEX_EXCERPT_LENGTH:
                continue
            selected_entry = replace(entry, excerpt=excerpt)
        try:
            items.append(to_raw_item(selected_entry, source))
        except (OverflowError, TypeError, ValueError, ValidationError):
            continue
    return items


__all__ = [
    "MAX_DETAIL_FETCHES",
    "MAX_INDEX_ENTRIES",
    "MIN_INDEX_EXCERPT_LENGTH",
    "PARSERS",
    "OfficialEntry",
    "OfficialPageStructureError",
    "collect_official_page",
]
