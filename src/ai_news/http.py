from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

_DEFAULT_USER_AGENT = "mount-su-ai-news/0.1 (+https://github.com/mount-su/ai-news)"
_MAX_REDIRECTS = 5
_RESPONSE_TOO_LARGE = "response exceeds byte limit"


class HttpFetchError(Exception):
    def __init__(self, message: str, *, safe_url: str) -> None:
        super().__init__(message)
        self.safe_url = safe_url


class HttpStatusFetchError(HttpFetchError):
    def __init__(self, *, status_code: int, safe_url: str) -> None:
        super().__init__(f"HTTP request failed with status {status_code}", safe_url=safe_url)
        self.status_code = status_code


class HttpTransportFetchError(HttpFetchError):
    def __init__(self, *, safe_url: str) -> None:
        super().__init__("HTTP transport failed", safe_url=safe_url)


def _require_https(url: httpx.URL) -> None:
    if url.scheme != "https" or not url.host:
        raise ValueError("URL must use https")


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    effective_port = url.port if url.port is not None else 443
    return (url.scheme, url.host, effective_port)


def _safe_url(url: httpx.URL) -> str:
    return str(url.copy_with(username=None, password=None, query=None, fragment=None))


def _request_headers(headers: dict[str, str] | None) -> httpx.Headers:
    merged = httpx.Headers(headers)
    merged["User-Agent"] = _DEFAULT_USER_AGENT
    return merged


async def _read_bounded(response: httpx.Response, max_bytes: int, url: httpx.URL) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise HttpFetchError(_RESPONSE_TOO_LARGE, safe_url=_safe_url(url))

    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise HttpFetchError(_RESPONSE_TOO_LARGE, safe_url=_safe_url(url))
        chunks.append(chunk)
    return b"".join(chunks)


def _redirect_url(
    current_url: httpx.URL,
    location: str,
    initial_origin: tuple[str, str, int],
    visited_urls: set[httpx.URL],
    redirect_count: int,
) -> httpx.URL:
    invalid_target = False
    try:
        raw_target = httpx.URL(location)
        target = current_url.join(location)
    except (TypeError, ValueError):
        invalid_target = True

    if invalid_target:
        raise HttpFetchError(
            "invalid redirect target",
            safe_url=_safe_url(current_url),
        ) from None

    if raw_target.userinfo or (raw_target.scheme and not raw_target.host):
        raise HttpFetchError(
            "invalid redirect target",
            safe_url=_safe_url(current_url),
        )
    if target.scheme != "https" or not target.host or target.userinfo:
        raise HttpFetchError(
            "redirect target must use https",
            safe_url=_safe_url(current_url),
        )
    if _origin(target) != initial_origin:
        raise HttpFetchError(
            "redirect must preserve origin",
            safe_url=_safe_url(current_url),
        )
    if target in visited_urls:
        raise HttpFetchError(
            "redirect loop detected",
            safe_url=_safe_url(current_url),
        )
    if redirect_count == _MAX_REDIRECTS:
        raise HttpFetchError(
            "redirect limit exceeded",
            safe_url=_safe_url(current_url),
        )
    return target


async def _request_once(
    client: httpx.AsyncClient,
    url: httpx.URL,
    headers: httpx.Headers,
    max_bytes: int,
) -> bytes:
    initial_origin = _origin(url)
    current_url = url
    visited_urls = {url}

    for redirect_count in range(_MAX_REDIRECTS + 1):
        async with client.stream(
            "GET",
            current_url,
            headers=headers,
            timeout=20,
            follow_redirects=False,
        ) as response:
            if response.has_redirect_location:
                next_url = _redirect_url(
                    current_url,
                    response.headers["Location"],
                    initial_origin,
                    visited_urls,
                    redirect_count,
                )
            elif response.is_error:
                raise HttpStatusFetchError(
                    status_code=response.status_code,
                    safe_url=_safe_url(current_url),
                )
            else:
                return await _read_bounded(response, max_bytes, current_url)

        visited_urls.add(next_url)
        current_url = next_url

    raise RuntimeError("unreachable")


async def get_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    attempts: int = 3,
    max_bytes: int = 2_000_000,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bytes:
    if attempts < 0:
        raise ValueError("attempts must be non-negative")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    parsed_url = httpx.URL(url)
    _require_https(parsed_url)
    request_headers = _request_headers(headers)

    transport_failed = False
    for retry_number in range(attempts + 1):
        try:
            return await _request_once(client, parsed_url, request_headers, max_bytes)
        except HttpStatusFetchError as error:
            status = error.status_code
            if status != 429 and not 500 <= status <= 599:
                raise
            if retry_number == attempts:
                raise
        except httpx.TransportError:
            if retry_number == attempts:
                transport_failed = True
                break

        await sleep(2**retry_number)

    if transport_failed:
        raise HttpTransportFetchError(safe_url=_safe_url(parsed_url)) from None
    raise RuntimeError("unreachable")
