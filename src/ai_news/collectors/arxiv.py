from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

import bleach
import feedparser
import httpx
from dateutil.parser import isoparse
from pydantic import ValidationError

from ai_news.http import get_bytes
from ai_news.models import RawItem, SourceSpec

_RAW_HTML_BLOCK = re.compile(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", re.I | re.S)
_ISO_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}"
    r"(?::\d{2}(?:[.,]\d+)?)?(?:[Zz]|[+-]\d{2}(?::?\d{2})?)$"
)
_WHITESPACE = re.compile(r"\s+")
_MAX_EXCERPT_LENGTH = 4_000
_MAX_SANITIZE_INPUT_LENGTH = _MAX_EXCERPT_LENGTH * 16


def _require_arxiv_source(source: SourceSpec) -> str:
    if source.kind != "arxiv":
        raise ValueError("expected an arxiv source")
    if source.url is None:
        raise ValueError("arxiv source requires a url")
    return str(source.url)


def _clean_html(value: object) -> str:
    if not isinstance(value, str):
        return ""
    decoded = value[:_MAX_SANITIZE_INPUT_LENGTH]
    for _ in range(3):
        next_decoded = html.unescape(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    without_raw_blocks = _RAW_HTML_BLOCK.sub("", decoded)
    plain_text = bleach.clean(without_raw_blocks, tags=[], strip=True)
    plain_text = plain_text.replace("&amp;", "&")
    return _WHITESPACE.sub(" ", plain_text).strip()


def _https_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    url = value.strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname is None:
        return None
    return url


def _published_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    timestamp = value.strip()
    if not _ISO_DATETIME.fullmatch(timestamp):
        return None
    try:
        published_at = isoparse(timestamp)
    except (OverflowError, TypeError, ValueError):
        return None
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        return None
    return published_at.astimezone(UTC)


def parse_arxiv(payload: bytes, source: SourceSpec) -> list[RawItem]:
    _require_arxiv_source(source)
    try:
        loads = getattr(feedparser, "loads", feedparser.parse)
        parsed_feed = loads(payload)
    except Exception:
        return []

    items: list[RawItem] = []
    for entry in parsed_feed.entries:
        try:
            title = _clean_html(entry.get("title"))
            url = _https_url(entry.get("link"))
            published_at = _published_at(entry.get("published"))
            if not title or url is None or published_at is None:
                continue

            item = RawItem(
                source_id=source.id,
                source_name=source.name,
                source_weight=source.weight,
                title=title,
                url=url,
                published_at=published_at,
                excerpt=_clean_html(entry.get("summary"))[:_MAX_EXCERPT_LENGTH],
                category_hint=source.category,
                is_official_source=source.official,
                source_role=source.role,
            )
        except (OverflowError, TypeError, ValueError, ValidationError):
            continue
        items.append(item)

    return items


async def collect_arxiv(
    client: httpx.AsyncClient,
    source: SourceSpec,
) -> list[RawItem]:
    url = _require_arxiv_source(source)
    payload = await get_bytes(client, url)
    return parse_arxiv(payload, source)
