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

_ADAPTER = "deepseek_news"
_ALLOWED_HOSTS = frozenset({"deepseek.com", "www.deepseek.com"})
_DATE_PATTERN = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)


def _require_source(source: SourceSpec) -> None:
    if source.kind != "official_page" or source.adapter != _ADAPTER or source.url is None:
        raise ValueError("deepseek_news source is required")


def parse(payload: bytes, source: SourceSpec) -> list[OfficialEntry]:
    _require_source(source)
    if looks_like_challenge(payload):
        raise OfficialPageStructureError("deepseek page challenge")

    soup = BeautifulSoup(payload, "html.parser")
    cards = soup.select("a.ds-news-hero-card[href]")
    if not cards:
        raise OfficialPageStructureError("deepseek news cards missing")

    entries: list[OfficialEntry] = []
    seen_urls: set[str] = set()
    for card in cards:
        heading = card.find("h2")
        card_text = clean_text(card.get_text(" ", strip=True))
        date_match = _DATE_PATTERN.search(card_text)
        if heading is None or date_match is None:
            continue
        title = clean_text(heading.get_text(" ", strip=True))
        paragraphs = [
            clean_text(paragraph.get_text(" ", strip=True)) for paragraph in card.find_all("p")
        ]
        excerpt = max(
            (
                text
                for text in paragraphs
                if text and text != title and date_match.group(0).casefold() not in text.casefold()
            ),
            key=len,
            default="",
        )
        try:
            url = require_allowed_https_url(
                card.get("href"),
                base_url=str(source.url),
                allowed_hosts=_ALLOWED_HOSTS,
            )
            published_at = zoned_date(date_match.group(0), "Asia/Shanghai")
        except (OverflowError, TypeError, ValueError):
            continue
        if not title or url in seen_urls:
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
