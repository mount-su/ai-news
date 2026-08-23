from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from ai_news.collectors.official_pages.base import (
    OfficialEntry,
    OfficialPageStructureError,
    clean_text,
    looks_like_challenge,
    require_allowed_https_url,
    zoned_date,
)
from ai_news.models import SourceSpec

_ADAPTER = "volcengine_doubao"
_ALLOWED_HOSTS = frozenset({"volcengine.com", "www.volcengine.com"})
_ROUTER_MARKER = re.compile(r"window\._ROUTER_DATA\s*=\s*")
_BRAND_PATTERN = re.compile(r"豆包|方舟|大模型|Doubao|ARK", re.IGNORECASE)


def _require_source(source: SourceSpec) -> None:
    if source.kind != "official_page" or source.adapter != _ADAPTER or source.url is None:
        raise ValueError("volcengine_doubao source is required")


def _router_data(payload: bytes) -> Mapping[str, Any]:
    text = payload.decode("utf-8", errors="replace")
    marker = _ROUTER_MARKER.search(text)
    if marker is None:
        raise OfficialPageStructureError("volcengine router data missing")
    try:
        decoded, _ = json.JSONDecoder().raw_decode(text[marker.end() :].lstrip())
    except (TypeError, ValueError):
        raise OfficialPageStructureError("volcengine router data invalid") from None
    if not isinstance(decoded, Mapping):
        raise OfficialPageStructureError("volcengine router data invalid")
    return decoded


def _news_page(data: Mapping[str, Any]) -> Mapping[str, Any]:
    loader_data = data.get("loaderData")
    if not isinstance(loader_data, Mapping):
        raise OfficialPageStructureError("volcengine news data missing")
    page = loader_data.get("__ssr_without_user/news/page")
    if not isinstance(page, Mapping):
        raise OfficialPageStructureError("volcengine news data missing")
    article_container = page.get("listOnlineArticle")
    if not isinstance(article_container, Mapping) or not isinstance(
        article_container.get("List"), list
    ):
        raise OfficialPageStructureError("volcengine article list missing")
    return page


def _identifier(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return ""
    return str(value).strip()


def _row_entry(row: Mapping[str, Any], source: SourceSpec) -> OfficialEntry | None:
    title = clean_text(row.get("Title") or row.get("title"))
    excerpt = clean_text(
        row.get("Summary") or row.get("summary") or row.get("Description") or row.get("description")
    )
    brand_text = " ".join(
        clean_text(row.get(key))
        for key in (
            "Title",
            "title",
            "Summary",
            "summary",
            "Description",
            "description",
            "TagName",
            "tag",
            "CategoryCodeName",
            "category",
        )
    )
    if not title or _BRAND_PATTERN.search(brand_text) is None:
        return None

    document_id = _identifier(row.get("DocumentID"))
    raw_url = row.get("link") or row.get("Link")
    if document_id:
        raw_url = f"/news/detail/{document_id}"
    date_text = clean_text(
        row.get("CreatedTime") or row.get("date") or row.get("Date") or row.get("UpdatedTime")
    )
    try:
        url = require_allowed_https_url(
            raw_url,
            base_url=str(source.url),
            allowed_hosts=_ALLOWED_HOSTS,
        )
        published_at = zoned_date(date_text, "Asia/Shanghai")
    except (OverflowError, TypeError, ValueError):
        return None
    return OfficialEntry(
        title=title,
        url=url,
        published_at=published_at,
        excerpt=excerpt,
    )


def parse(payload: bytes, source: SourceSpec) -> list[OfficialEntry]:
    _require_source(source)
    if looks_like_challenge(payload):
        raise OfficialPageStructureError("volcengine page challenge")

    page = _news_page(_router_data(payload))
    article_container = page["listOnlineArticle"]
    rows: list[Mapping[str, Any]] = []
    banner = page.get("banner")
    if isinstance(banner, Mapping) and banner:
        rows.append(banner)
    rows.extend(row for row in article_container["List"] if isinstance(row, Mapping))

    entries: list[OfficialEntry] = []
    seen_urls: set[str] = set()
    for row in rows:
        entry = _row_entry(row, source)
        if entry is None or entry.url in seen_urls:
            continue
        seen_urls.add(entry.url)
        entries.append(entry)
    return entries
