from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import bleach
from bs4 import BeautifulSoup
from dateutil.parser import parse as parse_datetime

from ai_news.models import RawItem, SourceSpec

_MAX_EXCERPT_LENGTH = 4_000
_WHITESPACE = re.compile(r"\s+")
_CHALLENGE_MARKERS = (
    "access denied",
    "captcha",
    "cf-chl-",
    "verify you are human",
)
_ARTICLE_TYPES = frozenset({"article", "newsarticle"})


@dataclass(frozen=True, slots=True)
class OfficialEntry:
    title: str
    url: str
    published_at: datetime
    excerpt: str = ""


class OfficialPageStructureError(ValueError):
    """The reviewed official page structure is no longer recognizable."""


def clean_text(value: object, *, max_length: int = _MAX_EXCERPT_LENGTH) -> str:
    if not isinstance(value, str) or max_length <= 0:
        return ""
    plain_text = bleach.clean(unescape(value), tags=[], strip=True)
    return _WHITESPACE.sub(" ", unescape(plain_text)).strip()[:max_length]


def require_allowed_https_url(
    value: object,
    *,
    base_url: str,
    allowed_hosts: set[str] | frozenset[str],
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("URL must use an allowed official host")
    try:
        joined = urljoin(base_url, value.strip())
        parsed = urlsplit(joined)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("URL must use an allowed official host") from None

    normalized_hosts = {host.casefold().rstrip(".") for host in allowed_hosts}
    hostname = parsed.hostname.casefold().rstrip(".") if parsed.hostname is not None else None
    if (
        parsed.scheme != "https"
        or hostname is None
        or hostname not in normalized_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise ValueError("URL must use an allowed official host")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def zoned_date(value: str, timezone_name: str) -> datetime:
    parsed_date = date.fromisoformat(value.strip())
    local_midnight = datetime.combine(parsed_date, datetime.min.time(), ZoneInfo(timezone_name))
    return local_midnight.astimezone(UTC)


def _json_ld_nodes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        nodes: list[dict[str, Any]] = []
        for item in value:
            nodes.extend(_json_ld_nodes(item))
        return nodes
    if not isinstance(value, dict):
        return []
    nodes = [value]
    graph = value.get("@graph")
    if isinstance(graph, list | dict):
        nodes.extend(_json_ld_nodes(graph))
    return nodes


def _article_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw_json = script.string or script.get_text()
        if not raw_json.strip():
            continue
        try:
            decoded = json.loads(raw_json)
        except (TypeError, ValueError):
            continue
        for node in _json_ld_nodes(decoded):
            raw_types = node.get("@type")
            types = [raw_types] if isinstance(raw_types, str) else raw_types
            if not isinstance(types, list):
                continue
            if any(
                isinstance(value, str) and value.casefold() in _ARTICLE_TYPES for value in types
            ):
                articles.append(node)
    return articles


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parse_datetime(value.strip())
    except (OverflowError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _meta_content(soup: BeautifulSoup, key: str) -> str:
    for attribute in ("property", "name"):
        tag = soup.find("meta", attrs={attribute: key})
        if tag is None:
            continue
        content = tag.get("content")
        if isinstance(content, str):
            return clean_text(content)
    return ""


def article_metadata(payload: bytes, url: str) -> OfficialEntry | None:
    soup = BeautifulSoup(payload, "html.parser")
    for article in _article_json_ld(soup):
        title = clean_text(article.get("headline") or article.get("name"))
        published_at = _aware_datetime(article.get("datePublished"))
        if title and published_at is not None:
            return OfficialEntry(
                title=title,
                url=url,
                published_at=published_at,
                excerpt=clean_text(article.get("description")),
            )

    title = _meta_content(soup, "og:title") or _meta_content(soup, "twitter:title")
    published_at = _aware_datetime(_meta_content(soup, "article:published_time"))
    if not title or published_at is None:
        return None
    return OfficialEntry(
        title=title,
        url=url,
        published_at=published_at,
        excerpt=article_excerpt(payload),
    )


def article_excerpt(payload: bytes) -> str:
    soup = BeautifulSoup(payload, "html.parser")
    for key in ("og:description", "description", "twitter:description"):
        excerpt = _meta_content(soup, key)
        if excerpt:
            return excerpt
    for article in _article_json_ld(soup):
        excerpt = clean_text(article.get("description"))
        if excerpt:
            return excerpt
    return ""


def looks_like_challenge(payload: bytes) -> bool:
    text = payload.decode("utf-8", errors="ignore").casefold()
    return any(marker in text for marker in _CHALLENGE_MARKERS)


def to_raw_item(
    entry: OfficialEntry,
    source: SourceSpec,
    *,
    source_name: str | None = None,
) -> RawItem:
    return RawItem(
        source_id=source.id,
        source_name=source_name or source.name,
        source_weight=source.weight,
        title=clean_text(entry.title),
        url=entry.url,
        published_at=entry.published_at,
        excerpt=clean_text(entry.excerpt),
        category_hint=source.category,
        is_official_source=source.official,
    )
