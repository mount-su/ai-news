from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from bs4 import BeautifulSoup

from ai_news.collectors.official_pages.base import (
    OfficialEntry,
    OfficialPageStructureError,
    clean_text,
    looks_like_challenge,
    require_allowed_https_url,
    zoned_date,
)
from ai_news.models import SourceSpec

_ADAPTER = "baidu_ernie"
_ALLOWED_HOSTS = frozenset({"cloud.baidu.com"})
_BRAND_PATTERN = re.compile(r"文心|ERNIE|千帆", re.IGNORECASE)


def _require_source(source: SourceSpec) -> None:
    if source.kind != "official_page" or source.adapter != _ADAPTER or source.url is None:
        raise ValueError("baidu_ernie source is required")


def _rows(payload: bytes) -> list[Mapping[str, Any]]:
    soup = BeautifulSoup(payload, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        raise OfficialPageStructureError("baidu next data missing")
    try:
        decoded = json.loads(script.string or script.get_text())
        result = decoded["props"]["pageProps"]["pageData"]["listData"]["result"]
    except (KeyError, TypeError, ValueError):
        raise OfficialPageStructureError("baidu news data invalid") from None
    if not isinstance(result, list):
        raise OfficialPageStructureError("baidu news list missing")
    return [row for row in result if isinstance(row, Mapping)]


def _identifier(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return ""
    return str(value).strip()


def parse(payload: bytes, source: SourceSpec) -> list[OfficialEntry]:
    _require_source(source)
    if looks_like_challenge(payload):
        raise OfficialPageStructureError("baidu page challenge")

    entries: list[OfficialEntry] = []
    seen_urls: set[str] = set()
    for row in _rows(payload):
        title = clean_text(row.get("title"))
        excerpt = clean_text(row.get("abstract") or row.get("contentEtl") or row.get("content"))
        brand_text = " ".join(
            (
                title,
                excerpt,
                clean_text(row.get("keywords")),
            )
        )
        news_id = _identifier(row.get("newsId"))
        date_text = clean_text(row.get("time"))
        if not title or not news_id or _BRAND_PATTERN.search(brand_text) is None:
            continue
        try:
            url = require_allowed_https_url(
                f"/news/news_{news_id}",
                base_url=str(source.url),
                allowed_hosts=_ALLOWED_HOSTS,
            )
            published_at = zoned_date(date_text, "Asia/Shanghai")
        except (OverflowError, TypeError, ValueError):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        entries.append(
            OfficialEntry(
                title=title,
                url=url,
                published_at=published_at,
                excerpt=excerpt,
            )
        )
    return entries
