from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import bleach
import feedparser
from dateutil.parser import parse as parse_datetime
from pydantic import ValidationError

from ai_news.models import RawItem, SourceSpec

_WHITESPACE = re.compile(r"\s+")
_MAX_EXCERPT_LENGTH = 4_000


def _text(value: object) -> str:
    return _WHITESPACE.sub(" ", value if isinstance(value, str) else "").strip()


def _https_url(value: object) -> str | None:
    url = _text(value)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname is None:
        return None
    return url


def _published_at(entry: feedparser.FeedParserDict) -> datetime | None:
    value = entry.get("published") or entry.get("updated") or entry.get("created")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        published_at = parse_datetime(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        return None
    return published_at.astimezone(UTC)


def _excerpt(entry: feedparser.FeedParserDict) -> str:
    description = entry.get("description") or entry.get("summary") or ""
    if not isinstance(description, str):
        return ""
    plain_text = bleach.clean(description, tags=[], strip=True)
    return _text(plain_text)[:_MAX_EXCERPT_LENGTH]


def parse_feed(payload: bytes, source: SourceSpec) -> list[RawItem]:
    try:
        loads = getattr(feedparser, "loads", feedparser.parse)
        parsed_feed = loads(payload)
    except Exception:
        return []

    items: list[RawItem] = []
    for entry in parsed_feed.entries:
        title = _text(entry.get("title"))
        url = _https_url(entry.get("link"))
        published_at = _published_at(entry)
        if not title or url is None or published_at is None:
            continue

        try:
            item = RawItem(
                source_id=source.id,
                source_name=source.name,
                source_weight=source.weight,
                title=title,
                url=url,
                published_at=published_at,
                excerpt=_excerpt(entry),
                category_hint=source.category,
                is_official_source=source.official,
            )
        except ValidationError:
            continue
        items.append(item)

    return items
