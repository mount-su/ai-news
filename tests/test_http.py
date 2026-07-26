from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from ai_news.http import (
    HttpFetchError,
    HttpStatusFetchError,
    HttpTransportFetchError,
    get_bytes,
)


class RecordingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.iterations = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.iterations += 1
            yield chunk


def _assert_exception_has_no_sensitive_state(error: Exception, secrets: set[str]) -> None:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        rendered = f"{current!s} {current!r}"
        assert all(secret not in rendered for secret in secrets)
        for value in vars(current).values():
            assert not isinstance(value, httpx.Request | httpx.Response | httpx.Headers)
            value_rendered = f"{value!s} {value!r}"
            assert all(secret not in value_rendered for secret in secrets)

        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


def test_non_https_url_is_rejected_without_a_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"unexpected")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="https"):
                await get_bytes(client, "http://example.com/feed.xml")

    asyncio.run(exercise())
    assert requests == []


@pytest.mark.parametrize("retry_status", [429, 500, 503, 599])
def test_retryable_statuses_use_exponential_delays_and_can_succeed(
    retry_status: int,
) -> None:
    request_count = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count < 4:
            return httpx.Response(retry_status, request=request)
        return httpx.Response(200, content=b"feed", request=request)

    async def no_op_sleep(delay: float) -> None:
        delays.append(delay)

    async def exercise() -> bytes:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await get_bytes(client, "https://example.com/feed.xml", sleep=no_op_sleep)

    assert asyncio.run(exercise()) == b"feed"
    assert request_count == 4
    assert delays == [1, 2, 4]


def test_retryable_status_stops_after_configured_attempts() -> None:
    request_count = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(500, request=request)

    async def no_op_sleep(delay: float) -> None:
        delays.append(delay)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HttpFetchError):
                await get_bytes(
                    client,
                    "https://example.com/feed.xml",
                    attempts=2,
                    sleep=no_op_sleep,
                )

    asyncio.run(exercise())
    assert request_count == 3
    assert delays == [1, 2]


@pytest.mark.parametrize("status", [400, 401, 404, 499])
def test_other_client_errors_are_not_retried(status: int) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(status, request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HttpFetchError) as exc_info:
                await get_bytes(client, "https://example.com/feed.xml?private=query")
        assert exc_info.value.status_code == status
        assert exc_info.value.safe_url == "https://example.com/feed.xml"

    asyncio.run(exercise())
    assert request_count == 1


@pytest.mark.parametrize("error_type", [httpx.TimeoutException, httpx.TransportError])
def test_transport_failures_are_retried(error_type: type[httpx.RequestError]) -> None:
    request_count = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count < 4:
            raise error_type("temporary failure", request=request)
        return httpx.Response(200, content=b"recovered", request=request)

    async def no_op_sleep(delay: float) -> None:
        delays.append(delay)

    async def exercise() -> bytes:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await get_bytes(client, "https://example.com/feed.xml", sleep=no_op_sleep)

    assert asyncio.run(exercise()) == b"recovered"
    assert request_count == 4
    assert delays == [1, 2, 4]


def test_content_length_over_limit_is_rejected_before_stream_read() -> None:
    stream = RecordingStream([b"should not be read"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "101"},
            stream=stream,
            request=request,
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HttpFetchError, match="^response exceeds byte limit$"):
                await get_bytes(client, "https://example.com/feed.xml", max_bytes=100)

    asyncio.run(exercise())
    assert stream.iterations == 0


def test_unknown_content_length_is_strictly_bounded_while_streaming() -> None:
    stream = RecordingStream([b"123", b"456", b"789"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HttpFetchError, match="^response exceeds byte limit$"):
                await get_bytes(client, "https://example.com/feed.xml", max_bytes=7)

    asyncio.run(exercise())
    assert stream.iterations == 3


def test_headers_merge_without_removing_default_user_agent_and_timeout_is_20_seconds() -> None:
    captured_headers: httpx.Headers | None = None
    captured_timeout: dict[str, float] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers, captured_timeout
        captured_headers = request.headers
        captured_timeout = request.extensions["timeout"]
        return httpx.Response(200, content=b"ok", request=request)

    async def exercise() -> bytes:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await get_bytes(
                client,
                "https://example.com/feed.xml",
                headers={"X-Trace": "enabled", "User-Agent": ""},
            )

    assert asyncio.run(exercise()) == b"ok"
    assert captured_headers is not None
    assert captured_headers["User-Agent"] == "mount-su-ai-news/0.1"
    assert captured_headers["X-Trace"] == "enabled"
    assert captured_timeout == {"connect": 20.0, "read": 20.0, "write": 20.0, "pool": 20.0}


def test_https_redirect_is_followed() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"Location": "https://example.com/final"},
                request=request,
            )
        return httpx.Response(200, content=b"redirected", request=request)

    async def exercise() -> bytes:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await get_bytes(client, "https://example.com/start")

    assert asyncio.run(exercise()) == b"redirected"
    assert requested_urls == ["https://example.com/start", "https://example.com/final"]


def test_cross_origin_redirect_is_rejected_before_credentials_leave_origin() -> None:
    secret = "private-api-key"
    received_requests: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_requests.append((str(request.url), request.headers.get("X-API-Key")))
        if request.url.host == "source.example":
            return httpx.Response(
                302,
                headers={"Location": "https://127.0.0.1/internal"},
                request=request,
            )
        return httpx.Response(200, content=b"must not be reached", request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HttpFetchError, match="origin"):
                await get_bytes(
                    client,
                    "https://source.example/start",
                    headers={"X-API-Key": secret},
                )

    asyncio.run(exercise())
    assert received_requests == [("https://source.example/start", secret)]


def test_redirect_cannot_change_from_explicit_zero_port_to_default_https_port() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/final"},
            request=request,
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HttpFetchError, match="origin"):
                await get_bytes(client, "https://example.com:0/start")

    asyncio.run(exercise())
    assert requested_urls == ["https://example.com:0/start"]


def test_redirect_to_non_https_is_rejected_before_insecure_request() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "http://example.com/insecure"},
            request=request,
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HttpFetchError, match="https"):
                await get_bytes(client, "https://example.com/start")

    asyncio.run(exercise())
    assert requested_urls == ["https://example.com/start"]


@pytest.mark.parametrize(
    "location",
    [
        "https://user:password@example.com/final",
        "https:///missing-host",
    ],
)
def test_redirect_target_requires_host_and_forbids_userinfo(location: str) -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(302, headers={"Location": location}, request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HttpFetchError, match="redirect"):
                await get_bytes(client, "https://example.com/start")

    asyncio.run(exercise())
    assert requested_urls == ["https://example.com/start"]


def test_redirect_loop_is_rejected_before_revisiting_a_url() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        location = "/second" if request.url.path == "/start" else "/start"
        return httpx.Response(302, headers={"Location": location}, request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HttpFetchError, match="redirect"):
                await get_bytes(client, "https://example.com/start")

    asyncio.run(exercise())
    assert requested_paths == ["/start", "/second"]


def test_more_than_five_redirects_is_rejected_before_sixth_hop() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        redirect_number = int(request.url.path.removeprefix("/"))
        return httpx.Response(
            302,
            headers={"Location": f"/{redirect_number + 1}"},
            request=request,
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HttpFetchError, match="redirect"):
                await get_bytes(client, "https://example.com/0")

    asyncio.run(exercise())
    assert requested_paths == ["/0", "/1", "/2", "/3", "/4", "/5"]


@pytest.mark.parametrize(
    ("attempts", "max_bytes"),
    [
        (-1, 100),
        (0, 0),
        (0, -1),
    ],
)
def test_invalid_limits_are_rejected(attempts: int, max_bytes: int) -> None:
    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)) as client:
            with pytest.raises(ValueError):
                await get_bytes(
                    client,
                    "https://example.com/feed.xml",
                    attempts=attempts,
                    max_bytes=max_bytes,
                )

    asyncio.run(exercise())


def test_authorization_header_value_is_not_exposed_in_errors() -> None:
    secret = "very-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request)

    async def exercise() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(HttpFetchError) as exc_info:
                await get_bytes(
                    client,
                    "https://example.com/feed.xml",
                    headers={"Authorization": f"Bearer {secret}"},
                )
        return str(exc_info.value)

    assert secret not in asyncio.run(exercise())


@pytest.mark.parametrize("failure_kind", ["status", "transport", "redirect", "oversize"])
def test_final_http_failures_do_not_retain_sensitive_request_state(failure_kind: str) -> None:
    authorization = "authorization-secret"
    api_key = "api-key-secret"
    query_secret = "query-secret"
    userinfo_secret = "userinfo-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        if failure_kind == "status":
            return httpx.Response(401, request=request)
        if failure_kind == "transport":
            raise httpx.ConnectError("connection failed", request=request)
        if failure_kind == "redirect":
            return httpx.Response(
                302,
                headers={"Location": "https://127.0.0.1/internal"},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"Content-Length": "2"},
            content=b"xx",
            request=request,
        )

    async def exercise() -> Exception:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            try:
                await get_bytes(
                    client,
                    f"https://url-user:{userinfo_secret}@example.com/feed.xml?token={query_secret}",
                    headers={
                        "Authorization": f"Bearer {authorization}",
                        "X-API-Key": api_key,
                    },
                    attempts=0,
                    max_bytes=1,
                )
            except Exception as error:
                return error
        raise AssertionError("expected get_bytes to fail")

    error = asyncio.run(exercise())
    assert isinstance(error, HttpFetchError)
    assert not isinstance(error, httpx.HTTPStatusError | httpx.TransportError | ValueError)
    expected_type = {
        "status": HttpStatusFetchError,
        "transport": HttpTransportFetchError,
        "redirect": HttpFetchError,
        "oversize": HttpFetchError,
    }[failure_kind]
    assert type(error) is expected_type
    expected_state: dict[str, int | str] = {"safe_url": "https://example.com/feed.xml"}
    if failure_kind == "status":
        expected_state["status_code"] = 401
    assert vars(error) == expected_state
    assert "?" not in error.safe_url
    assert "@" not in error.safe_url
    _assert_exception_has_no_sensitive_state(
        error,
        {
            authorization,
            api_key,
            query_secret,
            userinfo_secret,
            "Authorization",
            "X-API-Key",
        },
    )
