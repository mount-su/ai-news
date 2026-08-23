from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil.parser import parse as parse_datetime
from pydantic import ValidationError

from ai_news.collectors.official_pages.base import OfficialEntry, article_metadata, clean_text
from ai_news.http import HttpFetchError, get_bytes
from ai_news.models import RawItem, SourceSpec
from ai_news.pipeline.normalize import canonical_url

TRUSTED_MEDIA_DOMAINS = frozenset(
    {
        "reuters.com",
        "apnews.com",
        "theverge.com",
        "arstechnica.com",
        "techcrunch.com",
        "wired.com",
    }
)
MAX_FEED_ENTRIES = 100
MAX_EXTERNAL_FETCHES = 8
REQUEST_SPACING_SECONDS = 20

_COMMUNITY_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,21}$")
_COMMUNITY_LINK_PATTERN = re.compile(r"/r/(?P<community>[A-Za-z0-9_]{2,21})(?:/|$)")
_TOPIC_PATTERN = re.compile(
    r"\b(?:AI|artificial intelligence|LLMs?|large language models?|GPT|Claude|Gemini|"
    r"Llama|Mistral|DeepSeek|ChatGPT|Copilot|Perplexity|Stable Diffusion|agents?|"
    r"generative)\b|大模型|人工智能|生成式|智能体|豆包|文心|千问|混元|智谱|MiniMax",
    re.IGNORECASE,
)
_REJECTED_TITLE_PATTERN = re.compile(
    r"\b(?:help|question|rumou?r|unconfirmed|meme|showcase)\b|"
    r"self[- ]?promo|\bmy app\b|\bi built\b",
    re.IGNORECASE,
)
_BLOCKED_DOMAINS = frozenset(
    {
        "external-preview.redd.it",
        "i.redd.it",
        "preview.redd.it",
        "redd.it",
        "reddit.com",
        "v.redd.it",
    }
)
_SHORTENER_DOMAINS = frozenset(
    {"bit.ly", "buff.ly", "goo.gl", "ow.ly", "shorturl.at", "t.co", "tinyurl.com"}
)
_MEDIA_EXTENSION_PATTERN = re.compile(
    r"\.(?:avif|gif|jpe?g|m4v|mov|mp4|png|webm|webp)$",
    re.IGNORECASE,
)

Fetcher = Callable[[httpx.AsyncClient, str], Awaitable[bytes]]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RedditEntry:
    community: str
    post_id: str
    title: str
    external_url: str
    posted_at: datetime


class RedditFeedStructureError(ValueError):
    """The expected Reddit Atom structure is unavailable."""


def _require_source(source: SourceSpec) -> None:
    if source.kind != "reddit_rss" or source.official or source.weight != 5:
        raise ValueError("reddit_rss source is required")


def reddit_feed_urls(communities: Iterable[str]) -> tuple[str, str]:
    selected = list(communities)
    if (
        not selected
        or len(selected) != len(set(selected))
        or any(_COMMUNITY_PATTERN.fullmatch(community) is None for community in selected)
    ):
        raise ValueError("valid unique Reddit communities are required")
    joined = "+".join(selected)
    root = f"https://www.reddit.com/r/{joined}"
    return (
        f"{root}/new/.rss?limit=100",
        f"{root}/top/.rss?sort=top&t=day&limit=100",
    )


def _domain_matches(hostname: str, domain: str) -> bool:
    candidate = hostname.casefold().rstrip(".").removeprefix("www.")
    expected = domain.casefold().rstrip(".").removeprefix("www.")
    return candidate == expected or candidate.endswith(f".{expected}")


def _is_domain_allowed(hostname: str, domains: Iterable[str]) -> bool:
    return any(_domain_matches(hostname, domain) for domain in domains)


def _external_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except (TypeError, ValueError):
        return None
    hostname = parsed.hostname.casefold().rstrip(".") if parsed.hostname is not None else None
    if (
        parsed.scheme != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or _is_domain_allowed(hostname, _BLOCKED_DOMAINS)
        or _is_domain_allowed(hostname, _SHORTENER_DOMAINS)
        or _MEDIA_EXTENSION_PATTERN.search(parsed.path) is not None
    ):
        return None
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def _posted_at(entry: feedparser.FeedParserDict) -> datetime | None:
    value = entry.get("published") or entry.get("updated")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = parse_datetime(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _community(entry: feedparser.FeedParserDict, allowed: dict[str, str]) -> str | None:
    tags = entry.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            term = tag.get("term")
            if isinstance(term, str) and term.casefold() in allowed:
                return allowed[term.casefold()]
    link = entry.get("link")
    if isinstance(link, str):
        match = _COMMUNITY_LINK_PATTERN.search(link)
        if match is not None:
            return allowed.get(match.group("community").casefold())
    return None


def _content_html(entry: feedparser.FeedParserDict) -> str:
    content = entry.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("value"), str):
                return block["value"]
    summary = entry.get("summary")
    return summary if isinstance(summary, str) else ""


def _linked_original(entry: feedparser.FeedParserDict) -> str | None:
    soup = BeautifulSoup(_content_html(entry), "html.parser")
    for anchor in soup.find_all("a", href=True):
        if clean_text(anchor.get_text(" ", strip=True)).casefold() != "[link]":
            continue
        return _external_url(anchor.get("href"))
    return None


def _is_topic(value: str) -> bool:
    return _TOPIC_PATTERN.search(value) is not None


def parse_reddit_atom(payload: bytes, source: SourceSpec) -> list[RedditEntry]:
    _require_source(source)
    try:
        loads = getattr(feedparser, "loads", feedparser.parse)
        parsed = loads(payload)
    except Exception:
        raise RedditFeedStructureError("reddit atom parsing failed") from None
    if not str(parsed.get("version", "")).startswith("atom"):
        raise RedditFeedStructureError("reddit atom feed missing")

    allowed = {community.casefold(): community for community in source.communities}
    entries: list[RedditEntry] = []
    for raw_entry in parsed.entries[:MAX_FEED_ENTRIES]:
        title = clean_text(raw_entry.get("title"))
        community = _community(raw_entry, allowed)
        external_url = _linked_original(raw_entry)
        posted_at = _posted_at(raw_entry)
        post_id = clean_text(raw_entry.get("id"), max_length=300)
        if (
            not title
            or not post_id
            or community is None
            or external_url is None
            or posted_at is None
            or not _is_topic(title)
            or _REJECTED_TITLE_PATTERN.search(title) is not None
        ):
            continue
        entries.append(
            RedditEntry(
                community=community,
                post_id=post_id,
                title=title,
                external_url=external_url,
                posted_at=posted_at,
            )
        )
    return entries


def build_reddit_item(
    entry: RedditEntry,
    metadata: OfficialEntry | None,
    source: SourceSpec,
    allowed_domains: set[str] | frozenset[str],
) -> RawItem | None:
    _require_source(source)
    if metadata is None or not metadata.excerpt:
        return None
    parsed = urlsplit(metadata.url)
    if parsed.hostname is None or not _is_domain_allowed(parsed.hostname, allowed_domains):
        return None
    try:
        if canonical_url(metadata.url) != canonical_url(entry.external_url):
            return None
    except (TypeError, ValueError):
        return None
    if not _is_topic(f"{metadata.title} {metadata.excerpt}"):
        return None
    if metadata.published_at.tzinfo is None or metadata.published_at.utcoffset() is None:
        return None
    try:
        return RawItem(
            source_id=source.id,
            source_name=f"Reddit · r/{entry.community}",
            source_weight=5,
            title=metadata.title,
            url=metadata.url,
            published_at=metadata.published_at,
            excerpt=metadata.excerpt,
            category_hint=source.category,
            is_official_source=False,
        )
    except (TypeError, ValueError, ValidationError):
        return None


async def collect_reddit_rss(
    client: httpx.AsyncClient,
    source: SourceSpec,
    *,
    official_domains: set[str] | frozenset[str],
    fetcher: Fetcher = get_bytes,
    sleep: Sleeper = asyncio.sleep,
) -> list[RawItem]:
    _require_source(source)
    new_url, top_url = reddit_feed_urls(source.communities)
    new_payload = await fetcher(client, new_url)
    await sleep(REQUEST_SPACING_SECONDS)
    top_payload = await fetcher(client, top_url)

    discovered = [
        *parse_reddit_atom(new_payload, source),
        *parse_reddit_atom(top_payload, source),
    ]
    allowed_domains = frozenset({*official_domains, *TRUSTED_MEDIA_DOMAINS})
    unique: list[RedditEntry] = []
    seen_urls: set[str] = set()
    for entry in discovered:
        parsed = urlsplit(entry.external_url)
        if parsed.hostname is None or not _is_domain_allowed(parsed.hostname, allowed_domains):
            continue
        try:
            normalized = canonical_url(entry.external_url)
        except (TypeError, ValueError):
            continue
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)
        unique.append(entry)

    items: list[RawItem] = []
    for entry in unique[:MAX_EXTERNAL_FETCHES]:
        try:
            payload = await fetcher(client, entry.external_url)
            metadata = article_metadata(payload, entry.external_url)
        except (HttpFetchError, OverflowError, TypeError, ValueError):
            continue
        item = build_reddit_item(entry, metadata, source, allowed_domains)
        if item is not None:
            items.append(item)
    return items


__all__ = [
    "MAX_EXTERNAL_FETCHES",
    "REQUEST_SPACING_SECONDS",
    "RedditEntry",
    "RedditFeedStructureError",
    "build_reddit_item",
    "collect_reddit_rss",
    "parse_reddit_atom",
    "reddit_feed_urls",
]
