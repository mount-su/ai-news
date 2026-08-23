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

_ADAPTER = "minimax_releases"
_ALLOWED_HOSTS = frozenset({"minimaxi.com", "platform.minimaxi.com", "www.minimaxi.com"})
_HEADING_PATTERN = re.compile(r"^##\s+(?P<heading>.+?)\s*$", re.MULTILINE)
_FULL_DATE_PATTERN = re.compile(
    r"^(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日$"
)
_CARD_PATTERN = re.compile(
    r"<Card\b(?P<attributes>[^>]*)>(?P<body>.*?)</Card>",
    re.DOTALL | re.IGNORECASE,
)
_ATTRIBUTE_PATTERN = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)="([^"]*)"')
_MODEL_PATTERN = re.compile(
    r"MiniMax|model|模型|H\d|M\d|abab|海螺|Hailuo|speech|audio|video",
    re.IGNORECASE,
)


def _require_source(source: SourceSpec) -> None:
    if source.kind != "official_page" or source.adapter != _ADAPTER or source.url is None:
        raise ValueError("minimax_releases source is required")


def _markdown_text(value: str) -> str:
    without_links = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", value)
    without_markers = re.sub(r"[*_`#>]", " ", without_links)
    return clean_text(without_markers)


def parse(payload: bytes, source: SourceSpec) -> list[OfficialEntry]:
    _require_source(source)
    if looks_like_challenge(payload):
        raise OfficialPageStructureError("minimax page challenge")

    text = payload.decode("utf-8", errors="replace")
    headings = list(_HEADING_PATTERN.finditer(text))
    if not headings or _CARD_PATTERN.search(text) is None:
        raise OfficialPageStructureError("minimax release sections missing")

    entries: list[OfficialEntry] = []
    seen_urls: set[str] = set()
    for index, heading in enumerate(headings):
        date_match = _FULL_DATE_PATTERN.fullmatch(heading.group("heading").strip())
        if date_match is None:
            continue
        section_end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[heading.end() : section_end]
        date_text = "-".join(
            (
                date_match.group("year"),
                date_match.group("month").zfill(2),
                date_match.group("day").zfill(2),
            )
        )
        for card in _CARD_PATTERN.finditer(section):
            attributes = dict(_ATTRIBUTE_PATTERN.findall(card.group("attributes")))
            title = clean_text(attributes.get("title", ""))
            if not title or _MODEL_PATTERN.search(title) is None:
                continue
            try:
                url = require_allowed_https_url(
                    attributes.get("href"),
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
                    excerpt=_markdown_text(card.group("body")),
                )
            )
    return entries
