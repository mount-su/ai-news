from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import bleach
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


def _require_github_repo(source: SourceSpec) -> tuple[str, str]:
    if source.kind != "github":
        raise ValueError("expected a github source")
    if source.repo is None:
        raise ValueError("github source requires a repo")
    parts = source.repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("github source requires an owner/repo")
    return parts[0], parts[1]


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


def releases_url(source: SourceSpec) -> str:
    owner, repo = _require_github_repo(source)
    safe_owner = quote(owner, safe="")
    safe_repo = quote(repo, safe="")
    return f"https://api.github.com/repos/{safe_owner}/{safe_repo}/releases?per_page=10"


def parse_github_releases(payload: bytes, source: SourceSpec) -> list[RawItem]:
    _require_github_repo(source)
    try:
        releases = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("GitHub releases payload is not valid JSON") from None
    if not isinstance(releases, list):
        raise ValueError("GitHub releases payload must be a JSON list")

    items: list[RawItem] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") is not False or release.get("prerelease") is not False:
            continue

        try:
            title = _clean_html(release.get("name")) or _clean_html(release.get("tag_name"))
            url = _https_url(release.get("html_url"))
            published_at = _published_at(release.get("published_at"))
            if not title or url is None or published_at is None:
                continue

            item = RawItem(
                source_id=source.id,
                source_name=source.name,
                source_weight=source.weight,
                title=title,
                url=url,
                published_at=published_at,
                excerpt=_clean_html(release.get("body"))[:_MAX_EXCERPT_LENGTH],
                category_hint=source.category,
                is_official_source=source.official,
            )
        except (OverflowError, TypeError, ValueError, ValidationError):
            continue
        items.append(item)

    return items


async def collect_github_releases(
    client: httpx.AsyncClient,
    source: SourceSpec,
    token: str | None = None,
) -> list[RawItem]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if token is not None and (stripped_token := token.strip()):
        headers["Authorization"] = f"Bearer {stripped_token}"

    payload = await get_bytes(client, releases_url(source), headers=headers)
    return parse_github_releases(payload, source)
