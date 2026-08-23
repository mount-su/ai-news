from __future__ import annotations

import re

from ai_news.collectors.official_pages.base import (
    OfficialEntry,
    OfficialPageStructureError,
    clean_text,
    looks_like_challenge,
    require_allowed_https_url,
    zoned_date,
)
from ai_news.models import SourceSpec

_ADAPTER = "zhipu_releases"
_ALLOWED_HOSTS = frozenset({"docs.bigmodel.cn"})
_UPDATE_PATTERN = re.compile(
    r"<Update\b(?P<attributes>[^>]*)>(?P<body>.*?)</Update>",
    re.DOTALL | re.IGNORECASE,
)
_ATTRIBUTE_PATTERN = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)="([^"]*)"')
_LINK_PATTERN = re.compile(r"\[(?P<label>[^]]+)]\((?P<url>https://[^)]+)\)")
_RELEASE_PATTERN = re.compile(r"GLM|模型|产品|平台|智能体|Agent|发布|上线", re.IGNORECASE)


def _require_source(source: SourceSpec) -> None:
    if source.kind != "official_page" or source.adapter != _ADAPTER or source.url is None:
        raise ValueError("zhipu_releases source is required")


def _markdown_text(value: str) -> str:
    linked = _LINK_PATTERN.sub(lambda match: match.group("label"), value)
    without_markers = re.sub(r"[*_`#>💬]", " ", linked)
    without_bullets = re.sub(r"(?m)^\s*[-+]\s*", "", without_markers)
    return clean_text(without_bullets)


def parse(payload: bytes, source: SourceSpec) -> list[OfficialEntry]:
    _require_source(source)
    if looks_like_challenge(payload):
        raise OfficialPageStructureError("zhipu page challenge")

    text = payload.decode("utf-8", errors="replace")
    blocks = list(_UPDATE_PATTERN.finditer(text))
    if not blocks:
        raise OfficialPageStructureError("zhipu update blocks missing")

    entries: list[OfficialEntry] = []
    seen_urls: set[str] = set()
    for block in blocks:
        attributes = dict(_ATTRIBUTE_PATTERN.findall(block.group("attributes")))
        date_text = attributes.get("label", "")
        title = clean_text(attributes.get("description", ""))
        if not title or _RELEASE_PATTERN.search(title) is None:
            continue
        link = _LINK_PATTERN.search(block.group("body"))
        if link is None:
            continue
        try:
            url = require_allowed_https_url(
                link.group("url"),
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
                excerpt=_markdown_text(block.group("body")),
            )
        )
    return entries
