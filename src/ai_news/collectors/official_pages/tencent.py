from __future__ import annotations

import re

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

_ADAPTER = "tencent_hunyuan"
_ALLOWED_HOSTS = frozenset({"tencent.com", "www.tencent.com"})
_BRAND_PATTERN = re.compile(r"混元|Hunyuan", re.IGNORECASE)
_DATE_PATTERN = re.compile(r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日")


def _require_source(source: SourceSpec) -> None:
    if source.kind != "official_page" or source.adapter != _ADAPTER or source.url is None:
        raise ValueError("tencent_hunyuan source is required")


def parse(payload: bytes, source: SourceSpec) -> list[OfficialEntry]:
    _require_source(source)
    if looks_like_challenge(payload):
        raise OfficialPageStructureError("tencent page challenge")

    soup = BeautifulSoup(payload, "html.parser")
    cards = soup.select('article[id^="post-"]')
    if not cards:
        raise OfficialPageStructureError("tencent newsroom cards missing")

    entries: list[OfficialEntry] = []
    seen_urls: set[str] = set()
    for card in cards:
        heading_link = card.select_one("h2.blog-title a[href]")
        date_element = card.select_one(".tc-blogpost-date")
        card_text = clean_text(card.get_text(" ", strip=True))
        if heading_link is None or date_element is None or _BRAND_PATTERN.search(card_text) is None:
            continue
        title = clean_text(heading_link.get_text(" ", strip=True))
        date_match = _DATE_PATTERN.search(clean_text(date_element.get_text(" ", strip=True)))
        if not title or date_match is None:
            continue
        date_text = "-".join(
            (
                date_match.group("year"),
                date_match.group("month").zfill(2),
                date_match.group("day").zfill(2),
            )
        )
        paragraphs = [
            clean_text(paragraph.get_text(" ", strip=True)) for paragraph in card.find_all("p")
        ]
        excerpt = max(
            (paragraph for paragraph in paragraphs if paragraph and paragraph != title),
            key=len,
            default="",
        )
        try:
            url = require_allowed_https_url(
                heading_link.get("href"),
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
