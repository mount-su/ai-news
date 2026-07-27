from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote, urlsplit

import httpx

_ALLOWED_SUFFIXES = frozenset({"chat/completions", "v1/messages"})
_ARK_HOST = "ark.cn-beijing.volces.com"
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_MAX_DECODE_PASSES = 16


class InvalidLLMEndpoint(ValueError):
    """Raised when an LLM base URL cannot be used safely."""


def _invalid(message: str = "Invalid LLM endpoint configuration.") -> InvalidLLMEndpoint:
    return InvalidLLMEndpoint(message)


def _has_unsafe_character(value: str) -> bool:
    return any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    )


def _decode_path(path: str) -> tuple[str, bool]:
    current = path
    encoded_separator = False
    for _ in range(_MAX_DECODE_PASSES):
        if _INVALID_PERCENT_ESCAPE.search(current):
            raise _invalid()
        encoded_separator = encoded_separator or bool(_ENCODED_SEPARATOR.search(current))
        try:
            decoded = unquote(current, errors="strict")
        except (UnicodeDecodeError, ValueError):
            raise _invalid() from None
        if decoded == current:
            return decoded, encoded_separator
        current = decoded
    raise _invalid()


def _normalized_path(path: str) -> tuple[str, bool, bool]:
    separator_normalized = path.replace("\\", "/")
    has_dot_segment = any(
        segment in {".", ".."} for segment in separator_normalized.split("/")
    )
    normalized = posixpath.normpath(f"/{separator_normalized.lstrip('/')}")
    boundary_stripped = separator_normalized.rstrip("/") or "/"
    return normalized, has_dot_segment, normalized != boundary_stripped


def validate_llm_base_url(value: str) -> str:
    """Return a canonical HTTPS LLM base URL or raise a credential-safe error."""

    if not isinstance(value, str) or not value or _has_unsafe_character(value):
        raise _invalid()
    if "?" in value or "#" in value or "\\" in value:
        raise _invalid()

    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise _invalid() from None

    if (
        parsed.scheme.lower() != "https"
        or not host
        or "@" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
    ):
        raise _invalid()

    decoded_path, has_encoded_separator = _decode_path(parsed.path or "/")
    normalized_path, has_dot_segment, path_was_rewritten = _normalized_path(decoded_path)
    canonical_host = host.lower()

    if canonical_host == _ARK_HOST and (
        normalized_path == "/api/coding"
        or normalized_path.startswith("/api/coding/")
    ):
        raise _invalid("Ark Coding Plan endpoints are not allowed.")

    if (
        has_encoded_separator
        or "\\" in decoded_path
        or _has_unsafe_character(decoded_path)
        or has_dot_segment
        or path_was_rewritten
        or "?" in decoded_path
        or "#" in decoded_path
    ):
        raise _invalid()

    authority = f"[{canonical_host}]" if ":" in canonical_host else canonical_host
    if port is not None:
        authority = f"{authority}:{port}"

    try:
        canonical = httpx.URL(f"https://{authority}{normalized_path}")
    except (TypeError, ValueError):
        raise _invalid() from None
    if not canonical.host or canonical.query or canonical.fragment or canonical.userinfo:
        raise _invalid()
    return str(canonical)


def build_llm_endpoint(base_url: str, suffix: str) -> httpx.URL:
    """Build one of the supported LLM request endpoints from a safe base URL."""

    if not isinstance(suffix, str):
        raise _invalid()
    normalized_suffix = suffix.strip("/")
    if normalized_suffix not in _ALLOWED_SUFFIXES:
        raise _invalid()

    canonical_base = validate_llm_base_url(base_url)
    return httpx.URL(f"{canonical_base.rstrip('/')}/{normalized_suffix}")
