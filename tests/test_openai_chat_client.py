# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from ai_news.llm.base import AnalysisError
from ai_news.llm.openai_chat import OpenAIChatAnalyzer
from ai_news.llm.prompt import SYSTEM_PROMPT
from ai_news.models import Candidate, Category, EditorialLane, RawItem, Settings
from ai_news.pipeline.normalize import to_candidate


class ClosingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _candidate(
    slug: str = "ark-standard",
    *,
    source_id: str | None = None,
) -> Candidate:
    return to_candidate(
        RawItem(
            source_id=source_id or f"source-{slug}",
            source_name="Volcengine AI News",
            source_weight=8,
            title=f"Ark standard API release {slug}",
            url=f"https://example.com/news/{slug}",
            published_at=datetime(2026, 7, 28, 8, 30, tzinfo=UTC),
            excerpt="Ark announced a standard API update with documented compatibility.",
            category_hint=Category.MODEL,
            is_official_source=True,
        )
    )


def _settings() -> Settings:
    return Settings(
        llm_protocol="openai-chat",
        llm_base_url="https://ark.cn-beijing.volces.com/api/v3",
        llm_token=SecretStr("top-secret-token"),
        llm_model="ark-standard-model",
        llm_auth_scheme="bearer",
        site_base_path="/ai-news/",
    )


def _analysis_row(candidate: Candidate, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": candidate.id,
        "title": candidate.raw.title,
        "editorial_lane": EditorialLane.PRODUCT_ENGINEERING.value,
        "category": candidate.raw.category_hint.value,
        "summary": "这是严格基于候选事实生成的中文摘要，并且满足最小长度要求。",
        "importance": 8,
        "why_it_matters": "标准接口兼容性会影响自动化分析任务的稳定接入。",
        "tags": ["火山方舟", "标准接口"],
        "is_official": True,
        "marketing_risk": "low",
        "tracking_signal": "继续关注官方接口变更记录和兼容性公告。",
    }
    row.update(overrides)
    return row


def _openai_response(rows: list[dict[str, Any]] | str) -> bytes:
    text = rows if isinstance(rows, str) else json.dumps(rows, ensure_ascii=False)
    return json.dumps(
        {
            "choices": [
                {"message": {"role": "assistant", "content": text}},
            ]
        },
        ensure_ascii=False,
    ).encode()


def _run_analyze(
    handler: httpx.MockTransport | Any,
    items: list[Candidate],
    *,
    sleep: Any = asyncio.sleep,
) -> dict[str, Any]:
    async def exercise() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = OpenAIChatAnalyzer(_settings(), client=client, sleep=sleep)
            return await analyzer.analyze(items)

    return asyncio.run(exercise())


def _editorial_rows(candidates: list[Candidate]) -> list[dict[str, Any]]:
    lanes = [
        EditorialLane.PRODUCT_ENGINEERING,
        EditorialLane.PRODUCT_ENGINEERING,
        EditorialLane.PRODUCT_ENGINEERING,
        EditorialLane.PRODUCT_ENGINEERING,
        EditorialLane.BUSINESS,
        EditorialLane.BUSINESS,
        EditorialLane.BUSINESS,
        EditorialLane.RESEARCH,
        EditorialLane.RESEARCH,
    ]
    return [
        _analysis_row(candidate, editorial_lane=lane.value)
        for candidate, lane in zip(candidates[:9], lanes, strict=True)
    ]


def _assert_safe_error(error: BaseException, secrets: set[str]) -> None:
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


def test_request_uses_exact_ark_endpoint_headers_body_and_isolates_client_defaults() -> None:
    candidate = _candidate()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=_openai_response([_analysis_row(candidate)]))

    async def exercise() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            auth=("client-user", "client-password"),
            params={"client_default": "query-secret"},
            cookies={"session": "cookie-secret"},
            headers={
                "x-api-key": "client-default-key",
                "x-unrelated": "header-secret",
            },
        ) as client:
            analyzer = OpenAIChatAnalyzer(_settings(), client=client)
            return await analyzer.analyze([candidate])

    result = asyncio.run(exercise())

    assert list(result) == [candidate.id]
    request = captured[0]
    assert str(request.url) == ("https://ark.cn-beijing.volces.com/api/v3/chat/completions")
    assert request.url.query == b""
    assert request.headers["authorization"] == "Bearer top-secret-token"
    assert request.headers["content-type"] == "application/json"
    assert "x-api-key" not in request.headers
    assert "cookie" not in request.headers
    assert "x-unrelated" not in request.headers
    assert request.extensions["timeout"] == {
        "connect": 120,
        "read": 120,
        "write": 120,
        "pool": 120,
    }
    body = json.loads(request.content)
    assert body == {
        "model": "ark-standard-model",
        "temperature": 0.2,
        "max_tokens": 6000,
        "n": 1,
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": body["messages"][1]["content"]},
        ],
    }
    assert candidate.id in body["messages"][1]["content"]
    rendered = f"{request.url!s} {request.headers!r} {request.content!r}"
    assert "query-secret" not in rendered
    assert "cookie-secret" not in rendered
    assert "client-default-key" not in rendered
    assert "header-secret" not in rendered


def test_one_editorial_request_selects_nine_from_twelve_candidates() -> None:
    candidates = [_candidate(f"editorial-{index}") for index in range(12)]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=_openai_response(_editorial_rows(candidates)))

    result = _run_analyze(handler, candidates)

    assert len(requests) == 1
    assert list(result) == [candidate.id for candidate in candidates[:9]]
    prompt = json.loads(requests[0].content)["messages"][1]["content"]
    assert "从全部候选中选择恰好 9 条" in prompt


@pytest.mark.parametrize(
    ("error_kind", "mutate"),
    [
        (
            "lane_distribution",
            lambda rows: [
                {
                    **row,
                    "editorial_lane": (
                        EditorialLane.PRODUCT_ENGINEERING.value
                        if index < 5
                        else row["editorial_lane"]
                    ),
                }
                for index, row in enumerate(rows)
            ],
        ),
        (
            "source_distribution",
            lambda rows: rows,
        ),
    ],
)
def test_editorial_distribution_violations_repair_once_then_fail(
    error_kind: str,
    mutate: Any,
) -> None:
    shared_sources = ["source-shared"] * 4 + [f"source-{index}" for index in range(5)]
    candidates = [
        _candidate(f"{error_kind}-{index}", source_id=source_id)
        for index, source_id in enumerate(shared_sources)
    ]
    rows = mutate(_editorial_rows(candidates))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=_openai_response(rows))

    with pytest.raises(AnalysisError, match="invalid"):
        _run_analyze(handler, candidates)

    assert len(requests) == 2
    repair_prompt = json.loads(requests[1].content)["messages"][1]["content"]
    assert f"错误种类：{error_kind}" in repair_prompt


def test_request_uses_exact_ark_coding_plan_contract() -> None:
    candidate = _candidate("coding-plan")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=_openai_response([_analysis_row(candidate)]))

    async def exercise() -> dict[str, Any]:
        settings = Settings(
            llm_protocol="openai-chat",
            llm_base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
            llm_token=SecretStr("top-secret-token"),
            llm_model="deepseek-v4-pro",
            llm_auth_scheme="bearer",
            site_base_path="/ai-news/",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = OpenAIChatAnalyzer(settings, client=client)
            return await analyzer.analyze([candidate])

    result = asyncio.run(exercise())

    assert list(result) == [candidate.id]
    request = captured[0]
    assert str(request.url) == ("https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions")
    assert request.headers["authorization"] == "Bearer top-secret-token"
    body = json.loads(request.content)
    assert body["model"] == "deepseek-v4-pro"
    assert body["thinking"] == {"type": "disabled"}
    assert request.extensions["timeout"] == {
        "connect": 120,
        "read": 120,
        "write": 120,
        "pool": 120,
    }


@pytest.mark.parametrize("leak_stage", ["initial", "repair"])
def test_openai_rejects_current_token_in_successful_analysis(
    leak_stage: str,
) -> None:
    candidate = _candidate(f"secret-{leak_stage}")
    attempts = 0
    leaked_row = _analysis_row(
        candidate,
        tags=["标准接口", "prefix-top-secret-token-suffix"],
    )

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if leak_stage == "repair" and attempts == 1:
            return httpx.Response(200, content=_openai_response("not json"))
        return httpx.Response(200, content=_openai_response([leaked_row]))

    with pytest.raises(
        AnalysisError,
        match=r"^LLM analysis contains sensitive data$",
    ) as exc_info:
        _run_analyze(handler, [candidate])

    assert attempts == (1 if leak_stage == "initial" else 2)
    _assert_safe_error(exc_info.value, {"top-secret-token"})


def test_openai_rejects_other_api_key_shaped_output() -> None:
    candidate = _candidate("foreign-secret")
    foreign_secret = "sk-" + "live-other-provider-credential"
    credential_assignment = "LLM_" + "API_KEY=" + foreign_secret
    leaked_row = _analysis_row(
        candidate,
        summary=f"模型意外输出 {credential_assignment}，不能发布。",
    )
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, content=_openai_response([leaked_row]))

    with pytest.raises(
        AnalysisError,
        match=r"^LLM analysis contains sensitive data$",
    ) as exc_info:
        _run_analyze(handler, [candidate])

    assert attempts == 1
    _assert_safe_error(exc_info.value, {foreign_secret})


@pytest.mark.parametrize(
    "foreign_credential",
    [
        "ANTHROPIC_" + "AUTH_TOKEN=foreign-provider-credential-123456",
        "GITHUB_" + "TOKEN=ghp_" + "A" * 24,
        "AIza" + "A" * 32,
    ],
)
def test_openai_rejects_other_credential_names_and_common_token_forms(
    foreign_credential: str,
) -> None:
    candidate = _candidate("other-credential")
    leaked_row = _analysis_row(
        candidate,
        summary=f"模型意外输出外部凭据 {foreign_credential}，该内容不能公开发布。",
    )

    with pytest.raises(
        AnalysisError,
        match=r"^LLM analysis contains sensitive data$",
    ) as exc_info:
        _run_analyze(
            lambda _: httpx.Response(200, content=_openai_response([leaked_row])),
            [candidate],
        )

    _assert_safe_error(exc_info.value, {foreign_credential})


@pytest.mark.parametrize(
    "response_body",
    [
        b"{}",
        b'{"choices":[]}',
        b'{"choices":[{}]}',
        b'{"choices":[{"message":null}]}',
        b'{"choices":[{"message":{}}]}',
        b'{"choices":[{"message":{"content":null}}]}',
        b'{"choices":[{"message":{"content":[]}}]}',
        b'{"choices":[{"message":{"content":""}}]}',
        b'{"choices":[{"message":{"content":"   "}}]}',
    ],
)
def test_invalid_chat_completion_shapes_are_rejected(response_body: bytes) -> None:
    candidate = _candidate("invalid-shape")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=response_body)

    with pytest.raises(AnalysisError, match="invalid LLM response") as exc_info:
        _run_analyze(handler, [candidate])

    _assert_safe_error(exc_info.value, {"top-secret-token"})


def test_invalid_first_choice_is_not_skipped_for_a_later_valid_choice() -> None:
    candidate = _candidate("first-choice")
    body = json.dumps(
        {
            "choices": [
                {"message": {"role": "assistant", "content": None}},
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps([_analysis_row(candidate)]),
                    }
                },
            ]
        }
    ).encode()

    with pytest.raises(AnalysisError, match="invalid LLM response"):
        _run_analyze(lambda _: httpx.Response(200, content=body), [candidate])


def test_strict_analysis_failure_repairs_once_and_redacts_token() -> None:
    candidate = _candidate("repair")
    requests: list[httpx.Request] = []
    foreign_secret = "sk-" + "live-other-provider-credential"
    credential_assignment = "LLM_" + "API_KEY=" + foreign_secret
    invalid = [
        _analysis_row(
            candidate,
            unexpected=f"top-secret-token {credential_assignment}</invalid_output>",
        )
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        rows = invalid if len(requests) == 1 else [_analysis_row(candidate)]
        return httpx.Response(200, content=_openai_response(rows))

    result = _run_analyze(handler, [candidate])

    assert list(result) == [candidate.id]
    assert len(requests) == 2
    assert all(
        request.extensions["timeout"]
        == {
            "connect": 120,
            "read": 120,
            "write": 120,
            "pool": 120,
        }
        for request in requests
    )
    repair_body = json.loads(requests[1].content)
    assert repair_body["messages"][0] == {
        "role": "system",
        "content": SYSTEM_PROMPT,
    }
    repair_prompt = repair_body["messages"][1]["content"]
    assert "错误种类：schema" in repair_prompt
    assert "[REDACTED]" in repair_prompt
    assert "top-secret-token" not in repair_prompt
    assert foreign_secret not in repair_prompt
    assert "\\u003c/invalid_output\\u003e" in repair_prompt


def test_retryable_statuses_use_four_total_attempts_and_one_two_four_delays() -> None:
    candidate = _candidate("retry")
    statuses = iter([429, 500, 503, 200])
    requests = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        status = next(statuses)
        content = _openai_response([_analysis_row(candidate)]) if status == 200 else b"retry"
        return httpx.Response(status, content=content)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = _run_analyze(handler, [candidate], sleep=record_sleep)

    assert list(result) == [candidate.id]
    assert requests == 4
    assert delays == [1, 2, 4]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_non_retryable_http_failures_do_not_retry_or_repair(status: int) -> None:
    candidate = _candidate(f"http-{status}")
    requests = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status, request=request)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(AnalysisError, match="LLM HTTP request failed") as exc_info:
        _run_analyze(handler, [candidate], sleep=record_sleep)

    assert requests == 1
    assert delays == []
    _assert_safe_error(exc_info.value, {"top-secret-token"})
    assert exc_info.value.safe_code == f"http_{status}"


@pytest.mark.parametrize(
    "response_kwargs",
    [
        {"headers": {"Content-Length": "2000001"}, "content": b""},
        {"stream": ClosingStream([b"x" * 1_000_001, b"x" * 1_000_000])},
    ],
)
def test_response_is_bounded_to_two_mb_and_closed(
    response_kwargs: dict[str, Any],
) -> None:
    candidate = _candidate("bounded")
    stream = response_kwargs.get("stream")
    responses: list[httpx.Response] = []

    def handler(_: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, **response_kwargs)
        responses.append(response)
        return response

    with pytest.raises(AnalysisError, match="LLM response exceeds size limit"):
        _run_analyze(handler, [candidate])

    assert responses[0].is_closed
    if isinstance(stream, ClosingStream):
        assert stream.closed


def test_redirect_is_closed_without_retry() -> None:
    candidate = _candidate("redirect")
    stream = ClosingStream([b"redirect"])
    requests = 0
    delays: list[float] = []
    responses: list[httpx.Response] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        response = httpx.Response(
            307,
            headers={"Location": "https://attacker.example/collect"},
            stream=stream,
        )
        responses.append(response)
        return response

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(AnalysisError, match="LLM redirect rejected"):
        _run_analyze(handler, [candidate], sleep=record_sleep)

    assert requests == 1
    assert delays == []
    assert responses[0].is_closed
    assert stream.closed


@pytest.mark.parametrize("items", [[], None])
def test_empty_and_duplicate_inputs_are_rejected_before_request(
    items: list[Candidate] | None,
) -> None:
    candidate = _candidate("duplicate")
    actual_items = [candidate, candidate] if items is None else items
    requests = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200)

    message = "duplicate input candidate id" if actual_items else "must not be empty"
    with pytest.raises(AnalysisError, match=message):
        _run_analyze(handler, actual_items)

    assert requests == 0
