from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

_DEFAULT_USER_AGENT = "mount-su-ai-news/0.1"
_MAX_REDIRECTS = 20
_RESPONSE_TOO_LARGE = "response exceeds byte limit"


def _require_https(url: httpx.URL) -> None:
    if url.scheme != "https" or not url.host:
        raise ValueError("URL must use https")


def _request_headers(headers: dict[str, str] | None) -> httpx.Headers:
    merged = httpx.Headers(headers)
    merged["User-Agent"] = _DEFAULT_USER_AGENT
    return merged


async def _read_bounded(response: httpx.Response, max_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise ValueError(_RESPONSE_TOO_LARGE)

    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise ValueError(_RESPONSE_TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


async def _request_once(
    client: httpx.AsyncClient,
    url: httpx.URL,
    headers: httpx.Headers,
    max_bytes: int,
) -> bytes:
    request = client.build_request("GET", url, headers=headers, timeout=20)

    for _ in range(_MAX_REDIRECTS + 1):
        response = await client.send(request, stream=True, follow_redirects=False)
        next_request: httpx.Request | None = None
        try:
            if response.has_redirect_location:
                next_request = response.next_request
                if next_request is None:
                    response.raise_for_status()
                    return await _read_bounded(response, max_bytes)
                _require_https(next_request.url)
            else:
                response.raise_for_status()
                return await _read_bounded(response, max_bytes)
        finally:
            await response.aclose()

        request = next_request

    raise httpx.TooManyRedirects("Exceeded maximum allowed redirects", request=request)


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

    for retry_number in range(attempts + 1):
        try:
            return await _request_once(client, parsed_url, request_headers, max_bytes)
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if status != 429 and not 500 <= status <= 599:
                raise
            if retry_number == attempts:
                raise
        except httpx.TransportError:
            if retry_number == attempts:
                raise

        await sleep(2**retry_number)

    raise RuntimeError("unreachable")
