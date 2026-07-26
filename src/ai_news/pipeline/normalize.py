from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl

from ai_news.models import Candidate, RawItem

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "source",
}

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_PATH_SAFE = "/:@!$&'()*+,;=-._~"


def _require_valid_percent_encoding(value: str) -> None:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or not set(value[index + 1 : index + 3]) <= _HEX_DIGITS:
            raise ValueError("URL contains invalid percent encoding")
        index += 3


def _normalize_path(path: str) -> str:
    _require_valid_percent_encoding(path)
    parts: list[str] = []
    index = 0
    while index < len(path):
        character = path[index]
        if character == "%":
            parts.append(path[index : index + 3].upper())
            index += 3
            continue
        try:
            parts.append(quote(character, safe=_PATH_SAFE, encoding="utf-8", errors="strict"))
        except UnicodeError as error:
            raise ValueError("URL path has invalid Unicode encoding") from error
        index += 1

    normalized = "".join(parts) or "/"
    if normalized != "/":
        normalized = normalized.rstrip("/") or "/"
    return normalized


def _normalize_host(host: str) -> tuple[str, bool]:
    if ":" in host:
        try:
            return ipaddress.IPv6Address(host).compressed.lower(), True
        except ValueError as error:
            raise ValueError("URL has an invalid IPv6 host") from error

    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError("URL has an invalid host") from error

    labels = ascii_host.removesuffix(".").split(".")
    if not labels or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("URL has an invalid host")
    return ascii_host, False


def canonical_url(url: str | HttpUrl) -> str:
    value = str(url)
    if not value or value != value.strip():
        raise ValueError("URL must not be blank or surrounded by whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("URL must not contain control characters")

    try:
        parsed = urlsplit(value)
        port = parsed.port
        username = parsed.username
        password = parsed.password
        host = parsed.hostname
    except (UnicodeError, ValueError) as error:
        raise ValueError("URL is malformed") from error

    if parsed.scheme.lower() != "https":
        raise ValueError("URL must use https")
    if not host:
        raise ValueError("URL must include a host")
    if username is not None or password is not None:
        raise ValueError("URL userinfo is not allowed")

    normalized_host, is_ipv6 = _normalize_host(host)
    display_host = f"[{normalized_host}]" if is_ipv6 else normalized_host
    if port is not None and port != 443:
        display_host = f"{display_host}:{port}"

    _require_valid_percent_encoding(parsed.query)
    try:
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeError, ValueError) as error:
        raise ValueError("URL query has invalid encoding") from error
    retained_query = [
        (key, query_value)
        for key, query_value in query_items
        if key.casefold() not in TRACKING_PARAMS
    ]
    retained_query.sort(key=lambda pair: (pair[0], pair[1]))
    query = urlencode(retained_query, doseq=True, encoding="utf-8", errors="strict")

    return urlunsplit(("https", display_host, _normalize_path(parsed.path), query, ""))


def normalize_title(title: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", title).split())
    if not normalized:
        raise ValueError("title must not be empty")
    return normalized


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must include timezone information")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as error:
        raise ValueError("datetime must include valid timezone information") from error
    if offset is None:
        raise ValueError("datetime must include valid timezone information")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as error:
        raise ValueError("datetime must include valid timezone information") from error


def to_candidate(raw: RawItem) -> Candidate:
    normalized_url = canonical_url(raw.url)
    raw_data = raw.model_dump()
    raw_data.update(
        url=normalized_url,
        title=normalize_title(raw.title),
        published_at=ensure_utc(raw.published_at),
    )
    normalized_raw = RawItem.model_validate(raw_data)
    candidate_id = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:16]
    return Candidate(id=candidate_id, canonical_url=normalized_url, raw=normalized_raw)
