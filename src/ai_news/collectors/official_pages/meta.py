from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from ai_news.collectors.official_pages.base import (
    OfficialEntry,
    OfficialPageStructureError,
    clean_text,
    looks_like_challenge,
    require_allowed_https_url,
    zoned_date,
)
from ai_news.models import SourceSpec

_ADAPTER = "meta_ai_blog"
_ALLOWED_HOSTS = frozenset({"ai.meta.com"})
_GENERIC_LINK_TEXT = frozenset({"blog", "featured", "learn more", "open source", "research"})
_DATE_PATTERN = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)


def _require_source(source: SourceSpec) -> None:
    if source.kind != "official_page" or source.adapter != _ADAPTER or source.url is None:
        raise ValueError("meta_ai_blog source is required")


def _dated_context(anchor: Tag) -> tuple[Tag, str] | None:
    for ancestor in anchor.parents:
        if not isinstance(ancestor, Tag):
            continue
        text = clean_text(ancestor.get_text(" ", strip=True))
        match = _DATE_PATTERN.search(text)
        if match is not None:
            return ancestor, match.group(0)
        if ancestor.name in {"body", "html"}:
            break
    return None


def _title(anchors: list[Tag]) -> str:
    candidates: set[str] = set()
    for anchor in anchors:
        for heading in anchor.find_all(["h1", "h2", "h3"]):
            candidates.add(clean_text(heading.get_text(" ", strip=True)))
        candidates.add(clean_text(anchor.get_text(" ", strip=True)))
    eligible = [
        candidate
        for candidate in candidates
        if candidate and candidate.casefold() not in _GENERIC_LINK_TEXT
    ]
    return max(eligible, key=len, default="")


def _excerpt(context: Tag, title: str, date_text: str) -> str:
    paragraphs = [
        clean_text(paragraph.get_text(" ", strip=True)) for paragraph in context.find_all("p")
    ]
    eligible = [
        paragraph
        for paragraph in paragraphs
        if paragraph
        and paragraph != title
        and date_text.casefold() not in paragraph.casefold()
        and paragraph.casefold() not in _GENERIC_LINK_TEXT
    ]
    return max(eligible, key=len, default="")


def parse(payload: bytes, source: SourceSpec) -> list[OfficialEntry]:
    _require_source(source)
    if looks_like_challenge(payload):
        raise OfficialPageStructureError("meta page challenge")

    soup = BeautifulSoup(payload, "html.parser")
    grouped: dict[str, list[Tag]] = defaultdict(list)
    for anchor in soup.find_all("a", href=True):
        try:
            url = require_allowed_https_url(
                anchor.get("href"),
                base_url=str(source.url),
                allowed_hosts=_ALLOWED_HOSTS,
            )
        except ValueError:
            continue
        parsed = urlsplit(url)
        if "/blog/" not in parsed.path or parsed.path.rstrip("/") == "/blog":
            continue
        grouped[url].append(anchor)
    if not grouped:
        raise OfficialPageStructureError("meta blog links missing")

    entries: list[OfficialEntry] = []
    for url, anchors in grouped.items():
        context_and_date = next(
            (value for anchor in anchors if (value := _dated_context(anchor)) is not None),
            None,
        )
        if context_and_date is None:
            continue
        context, date_text = context_and_date
        title = _title(anchors)
        if not title:
            continue
        try:
            published_at = zoned_date(date_text, "America/Los_Angeles")
        except (OverflowError, TypeError, ValueError):
            continue
        entries.append(
            OfficialEntry(
                title=title,
                url=url,
                published_at=published_at,
                excerpt=_excerpt(context, title, date_text),
            )
        )
    return entries
