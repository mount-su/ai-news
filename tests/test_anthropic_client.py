from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from ai_news.llm.anthropic import AnalysisError, AnthropicAnalyzer
from ai_news.llm.prompt import SYSTEM_PROMPT
from ai_news.models import Candidate, Category, RawItem, Settings
from ai_news.pipeline.normalize import to_candidate

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "llm-success.json"


class ClosingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.iterations = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.iterations += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _candidate(
    slug: str,
    *,
    title: str | None = None,
    excerpt: str = "Factual excerpt.",
    category: Category = Category.MODEL,
) -> Candidate:
    return to_candidate(
        RawItem(
            source_id=f"source-{slug}",
            source_name=f"Source {slug}",
            source_weight=7,
            title=title or f"Title {slug}",
            url=f"https://example.com/{slug}",
            published_at=datetime(2026, 7, 26, 8, 30, tzinfo=UTC),
            excerpt=excerpt,
            category_hint=category,
            is_official_source=True,
        )
    )


def _settings(
    *,
    base_url: str = "https://proxy.example/api/coding/",
    token: str = "top-secret-token",
    auth_scheme: str = "bearer",
) -> Settings:
    return Settings(
        llm_base_url=base_url,
        llm_token=SecretStr(token),
        llm_model="claude-test",
        llm_auth_scheme=auth_scheme,
        site_base_path="/ai-news/",
    )


def _analysis_row(candidate: Candidate, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": candidate.id,
        "title": candidate.raw.title,
        "category": candidate.raw.category_hint.value,
        "summary": "这是一段满足最小长度要求且完全基于候选事实的中文摘要内容。",
        "importance": 7,
        "why_it_matters": "这项进展可能影响后续产品能力与行业采用。",
        "tags": ["模型", "发布"],
        "is_official": candidate.raw.is_official_source,
        "marketing_risk": "low",
        "tracking_signal": "关注官方后续评测和更新。",
    }
    row.update(overrides)
    return row


def _anthropic_response(rows: list[dict[str, Any]] | str) -> bytes:
    text = rows if isinstance(rows, str) else json.dumps(rows, ensure_ascii=False)
    return json.dumps(
        {"content": [{"type": "tool_use"}, {"type": "text", "text": text}]},
        ensure_ascii=False,
    ).encode()


def _run_analyze(
    handler: httpx.MockTransport | Any,
    items: list[Candidate],
    *,
    settings: Settings | None = None,
    sleep: Any = asyncio.sleep,
):
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = AnthropicAnalyzer(settings or _settings(), client=client, sleep=sleep)
            return await analyzer.analyze(items)

    return asyncio.run(exercise())


def _assert_exception_has_no_sensitive_state(error: BaseException, secrets: set[str]) -> None:
    pending = [error]
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


def test_request_endpoint_bearer_headers_and_body_preserve_base_path() -> None:
    candidate = _candidate("request")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=_anthropic_response([_analysis_row(candidate)]))

    result = _run_analyze(handler, [candidate])

    assert list(result) == [candidate.id]
    request = captured[0]
    assert str(request.url) == "https://proxy.example/api/coding/v1/messages"
    assert request.headers["authorization"] == "Bearer top-secret-token"
    assert "x-api-key" not in request.headers
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body == {
        "model": "claude-test",
        "max_tokens": 6000,
        "temperature": 0.2,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": body["messages"][0]["content"]}],
    }
    assert candidate.id in body["messages"][0]["content"]


def test_x_api_key_authentication_uses_only_x_api_key_header() -> None:
    candidate = _candidate("x-api-key")
    headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers)
        return httpx.Response(200, content=_anthropic_response([_analysis_row(candidate)]))

    _run_analyze(handler, [candidate], settings=_settings(auth_scheme="x-api-key"))

    assert headers[0]["x-api-key"] == "top-secret-token"
    assert "authorization" not in headers[0]
    assert headers[0]["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize(
    ("auth_scheme", "client_headers", "required_header", "forbidden_header"),
    [
        (
            "bearer",
            {"x-api-key": "client-default-key"},
            ("authorization", "Bearer top-secret-token"),
            "x-api-key",
        ),
        (
            "x-api-key",
            {"Authorization": "Bearer client-default-token"},
            ("x-api-key", "top-secret-token"),
            "authorization",
        ),
    ],
)
def test_request_auth_is_isolated_from_opposite_client_default_header(
    auth_scheme: str,
    client_headers: dict[str, str],
    required_header: tuple[str, str],
    forbidden_header: str,
) -> None:
    candidate = _candidate(f"client-header-{auth_scheme}")
    captured: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers)
        return httpx.Response(200, content=_anthropic_response([_analysis_row(candidate)]))

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers=client_headers,
        ) as client:
            analyzer = AnthropicAnalyzer(
                _settings(auth_scheme=auth_scheme),
                client=client,
            )
            await analyzer.analyze([candidate])

    asyncio.run(exercise())

    assert captured[0][required_header[0]] == required_header[1]
    assert forbidden_header not in captured[0]


@pytest.mark.parametrize("auth_scheme", ["bearer", "x-api-key"])
def test_request_auth_is_isolated_from_client_default_auth(auth_scheme: str) -> None:
    candidate = _candidate(f"client-auth-{auth_scheme}")
    captured: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers)
        return httpx.Response(200, content=_anthropic_response([_analysis_row(candidate)]))

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            auth=("client-user", "client-password"),
        ) as client:
            analyzer = AnthropicAnalyzer(
                _settings(auth_scheme=auth_scheme),
                client=client,
            )
            await analyzer.analyze([candidate])

    asyncio.run(exercise())

    if auth_scheme == "bearer":
        assert captured[0]["authorization"] == "Bearer top-secret-token"
        assert "x-api-key" not in captured[0]
    else:
        assert captured[0]["x-api-key"] == "top-secret-token"
        assert "authorization" not in captured[0]


def test_request_does_not_inherit_client_default_params_cookies_or_unrelated_headers() -> None:
    candidate = _candidate("client-defaults")
    captured: list[httpx.Request] = []
    secrets = {"query-secret", "cookie-secret", "header-secret"}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(401, request=request)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            params={"client_default": "query-secret"},
            cookies={"session": "cookie-secret"},
            headers={"x-unrelated": "header-secret"},
        ) as client:
            analyzer = AnthropicAnalyzer(_settings(), client=client)
            with pytest.raises(AnalysisError) as exc_info:
                await analyzer.analyze([candidate])
        _assert_exception_has_no_sensitive_state(exc_info.value, secrets)

    asyncio.run(exercise())

    request = captured[0]
    assert str(request.url) == "https://proxy.example/api/coding/v1/messages"
    assert request.url.query == b""
    assert "cookie" not in request.headers
    assert "x-unrelated" not in request.headers
    assert request.extensions["timeout"] == {
        "connect": 30,
        "read": 30,
        "write": 30,
        "pool": 30,
    }
    rendered_request = f"{request.url!s} {request.headers!r}"
    assert all(secret not in rendered_request for secret in secrets)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:password@proxy.example/api",
        "https://@proxy.example/api",
        "https://proxy.example/api?private=query",
        "https://proxy.example/api?",
        "https://proxy.example/api#private-fragment",
        "https://proxy.example/api#",
    ],
)
def test_unsafe_base_url_components_are_rejected_without_request(base_url: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    candidate = _candidate("unsafe-base")
    with pytest.raises(AnalysisError) as exc_info:
        _run_analyze(handler, [candidate], settings=_settings(base_url=base_url))

    assert requests == []
    _assert_exception_has_no_sensitive_state(
        exc_info.value,
        {"top-secret-token", "password", "private=query", "private-fragment"},
    )


@pytest.mark.parametrize(
    ("base_url", "expected_path"),
    [
        ("https://[2001:db8::1]/api/coding", "/api/coding/v1/messages"),
        ("https://proxy.example/api/@scope", "/api/@scope/v1/messages"),
    ],
)
def test_base_url_validation_preserves_valid_ipv6_and_path_at_sign(
    base_url: str,
    expected_path: str,
) -> None:
    candidate = _candidate("valid-base-delimiters")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=_anthropic_response([_analysis_row(candidate)]))

    result = _run_analyze(handler, [candidate], settings=_settings(base_url=base_url))

    assert list(result) == [candidate.id]
    assert captured[0].url.path == expected_path


def test_429_and_5xx_are_retried_three_times_with_exponential_delays() -> None:
    candidate = _candidate("retry")
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            return httpx.Response(429 if attempts == 1 else 503, request=request)
        return httpx.Response(200, content=_anthropic_response([_analysis_row(candidate)]))

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = _run_analyze(handler, [candidate], sleep=record_sleep)

    assert result[candidate.id].importance == 7
    assert attempts == 4
    assert delays == [1, 2, 4]


def test_401_is_not_retried_or_repaired() -> None:
    candidate = _candidate("unauthorized")
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, request=request)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(AnalysisError):
        _run_analyze(handler, [candidate], sleep=record_sleep)

    assert attempts == 1
    assert delays == []


@pytest.mark.parametrize("error_type", [httpx.TimeoutException, httpx.ConnectError])
def test_timeout_and_transport_failures_are_retried(error_type: type[httpx.RequestError]) -> None:
    candidate = _candidate("transport")
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise error_type("private transport detail", request=request)
        return httpx.Response(200, content=_anthropic_response([_analysis_row(candidate)]))

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = _run_analyze(handler, [candidate], sleep=record_sleep)

    assert list(result) == [candidate.id]
    assert attempts == 4
    assert delays == [1, 2, 4]


@pytest.mark.parametrize(
    ("headers", "chunks", "expected_iterations"),
    [
        ({"Content-Length": "2000001"}, [b"not-read"], 0),
        ({}, [b"x" * 1_000_000, b"y" * 1_000_001], 2),
    ],
)
def test_response_is_bounded_to_two_mb_and_always_closed(
    headers: dict[str, str],
    chunks: list[bytes],
    expected_iterations: int,
) -> None:
    candidate = _candidate("large")
    stream = ClosingStream(chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=stream, request=request)

    with pytest.raises(AnalysisError, match="response"):
        _run_analyze(handler, [candidate])

    assert stream.iterations == expected_iterations
    assert stream.closed


def test_redirect_is_a_safe_non_retryable_failure_and_response_is_closed() -> None:
    candidate = _candidate("redirect")
    attempts = 0
    stream = ClosingStream([b"redirect"])

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            302,
            headers={"Location": "https://evil.example/steal"},
            stream=stream,
            request=request,
        )

    with pytest.raises(AnalysisError):
        _run_analyze(handler, [candidate])

    assert attempts == 1
    assert stream.closed


def test_parses_fixture_and_one_complete_outer_markdown_fence() -> None:
    first = _candidate("fixture-one", title="Fixture One")
    second = _candidate("fixture-two", title="Fixture Two", category=Category.RESEARCH)
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture_text = fixture["content"][0]["text"]
    fixture["content"][0]["text"] = f"```json\n{fixture_text}\n```"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture, request=request)

    result = _run_analyze(handler, [first, second])

    assert list(result) == [first.id, second.id]
    assert result[first.id].category is Category.MODEL
    assert result[second.id].marketing_risk == "medium"


def test_invalid_first_text_block_is_not_skipped_for_a_later_text_block() -> None:
    candidate = _candidate("invalid-first-text")
    attempts = 0
    payload = {
        "content": [
            {"type": "text", "text": 123},
            {
                "type": "text",
                "text": json.dumps([_analysis_row(candidate)], ensure_ascii=False),
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, json=payload, request=request)

    with pytest.raises(AnalysisError):
        _run_analyze(handler, [candidate])

    assert attempts == 1


@pytest.mark.parametrize("invalid_kind", ["unknown", "duplicate", "missing"])
def test_unknown_duplicate_and_missing_response_ids_trigger_one_repair_then_fail(
    invalid_kind: str,
) -> None:
    first = _candidate(f"{invalid_kind}-first")
    second = _candidate(f"{invalid_kind}-second")
    rows = [_analysis_row(first), _analysis_row(second)]
    if invalid_kind == "unknown":
        rows[0]["id"] = "0123456789abcdef"
    elif invalid_kind == "duplicate":
        rows[1]["id"] = first.id
    else:
        rows.pop()
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(200, content=_anthropic_response(rows), request=request)

    with pytest.raises(AnalysisError):
        _run_analyze(handler, [first, second])

    assert len(prompts) == 2
    assert "修复" in prompts[1]
    assert invalid_kind in prompts[1]


@pytest.mark.parametrize(
    "bad_field",
    [
        {"summary": "太短"},
        {"importance": 11},
        {"marketing_risk": "extreme"},
        {"unexpected": "forbidden"},
    ],
)
def test_analysis_field_boundaries_and_extra_fields_are_strictly_validated(
    bad_field: dict[str, Any],
) -> None:
    candidate = _candidate("bad-field")
    row = _analysis_row(candidate, **bad_field)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, content=_anthropic_response([row]), request=request)

    with pytest.raises(AnalysisError):
        _run_analyze(handler, [candidate])

    assert attempts == 2


def test_invalid_first_response_is_repaired_exactly_once_with_bounded_safe_prompt() -> None:
    candidate = _candidate("repair")
    token = "top-secret-token"
    invalid = token + " {" + "x" * 30_000
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompts.append(json.loads(request.content)["messages"][0]["content"])
        if len(prompts) == 1:
            return httpx.Response(200, content=_anthropic_response(invalid), request=request)
        return httpx.Response(
            200,
            content=_anthropic_response([_analysis_row(candidate)]),
            request=request,
        )

    result = _run_analyze(handler, [candidate])

    assert list(result) == [candidate.id]
    assert len(prompts) == 2
    assert "json" in prompts[1]
    assert token not in prompts[1]
    assert len(prompts[1]) < 22_000


def test_second_invalid_repair_response_raises_safe_analysis_error() -> None:
    candidate = _candidate("repair-fails")
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, content=_anthropic_response("not json"), request=request)

    with pytest.raises(AnalysisError, match="invalid") as exc_info:
        _run_analyze(handler, [candidate])

    assert attempts == 2
    _assert_exception_has_no_sensitive_state(
        exc_info.value,
        {"top-secret-token", "Authorization", "private transport detail"},
    )


def test_http_failure_exception_chain_does_not_retain_request_headers_or_secrets() -> None:
    candidate = _candidate("secret-chain")
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError(
            "failed at https://proxy.example/api?private=query",
            request=request,
        )

    async def no_sleep(delay: float) -> None:
        return None

    with pytest.raises(AnalysisError) as exc_info:
        _run_analyze(handler, [candidate], sleep=no_sleep)

    assert attempts == 4
    _assert_exception_has_no_sensitive_state(
        exc_info.value,
        {
            "top-secret-token",
            "Authorization",
            "Bearer",
            "private=query",
            "https://proxy.example/api/coding",
        },
    )


def test_unexpected_client_error_is_unified_without_retaining_original_exception() -> None:
    candidate = _candidate("unexpected-client-error")

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("top-secret-token Authorization private=query")

    with pytest.raises(AnalysisError) as exc_info:
        _run_analyze(handler, [candidate])

    _assert_exception_has_no_sensitive_state(
        exc_info.value,
        {"top-secret-token", "Authorization", "private=query"},
    )


def test_duplicate_input_ids_are_rejected_before_any_request() -> None:
    candidate = _candidate("duplicate-input")
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200)

    with pytest.raises(AnalysisError, match="duplicate"):
        _run_analyze(handler, [candidate, candidate])

    assert attempts == 0


def test_batches_ten_plus_one_and_preserves_input_order_in_aggregate() -> None:
    candidates = [_candidate(f"batch-{index}") for index in range(11)]
    batch_ids: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"]
        encoded = prompt.split("<candidate_data>\n", 1)[1].split(
            "\n</candidate_data>",
            1,
        )[0]
        ids = [row["id"] for row in json.loads(encoded)]
        batch_ids.append(ids)
        by_id = {candidate.id: candidate for candidate in candidates}
        rows = [_analysis_row(by_id[candidate_id]) for candidate_id in ids]
        return httpx.Response(200, content=_anthropic_response(rows), request=request)

    result = _run_analyze(handler, candidates)

    assert [len(ids) for ids in batch_ids] == [10, 1]
    assert list(result) == [candidate.id for candidate in candidates]
