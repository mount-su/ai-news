from __future__ import annotations

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

_ADAPTER = "anthropic_news"
_ALLOWED_HOSTS = frozenset({"anthropic.com", "www.anthropic.com"})


def _require_source(source: SourceSpec) -> None:
    if source.kind != "official_page" or source.adapter != _ADAPTER or source.url is None:
        raise ValueError("anthropic_news source is required")


def parse(payload: bytes, source: SourceSpec) -> list[OfficialEntry]:
    _require_source(source)
    if looks_like_challenge(payload):
        raise OfficialPageStructureError("anthropic page challenge")

    soup = BeautifulSoup(payload, "html.parser")
    cards = [
        anchor for anchor in soup.find_all("a", href=True) if "/news/" in str(anchor.get("href"))
    ]
    if not cards:
        raise OfficialPageStructureError("anthropic news cards missing")

    entries: list[OfficialEntry] = []
    seen_urls: set[str] = set()
    for card in cards:
        heading = card.find("h2")
        time = card.find("time")
        if heading is None or time is None:
            continue
        title = clean_text(heading.get_text(" ", strip=True))
        date_text = clean_text(time.get_text(" ", strip=True))
        paragraphs = [
            clean_text(paragraph.get_text(" ", strip=True)) for paragraph in card.find_all("p")
        ]
        excerpt = max((text for text in paragraphs if text), key=len, default="")
        try:
            url = require_allowed_https_url(
                card.get("href"),
                base_url=str(source.url),
                allowed_hosts=_ALLOWED_HOSTS,
            )
            published_at = zoned_date(date_text, "America/Los_Angeles")
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
