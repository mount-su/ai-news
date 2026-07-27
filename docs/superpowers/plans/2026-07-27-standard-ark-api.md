# Standard Ark API Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe OpenAI-compatible Ark `/api/v3` support while preserving legal Anthropic-compatible providers, rejecting Ark Coding Plan endpoints, and carrying the result through GitHub Actions to a verified production Pages deployment.

**Architecture:** Introduce one canonical LLM endpoint validator, migrate configuration to `LLM_*`, extract the existing strict analysis/retry loop into a protocol-neutral base class, and add thin Anthropic and OpenAI Chat codecs selected by a factory. Keep collection, ranking, persistence, report Schema, and site generation unchanged. Migrate workflow Secrets/Variables atomically and gate first production generation on a standard API key and a model ID confirmed in the standard Ark account.

**Tech Stack:** Python 3.12, Pydantic 2, httpx, pytest, GitHub Actions, GitHub Pages, GitHub CLI.

---

## File map

| Path | Responsibility |
| --- | --- |
| `src/ai_news/llm/endpoint.py` | Canonical HTTPS base URL validation, Coding Plan rejection, safe endpoint construction |
| `src/ai_news/models.py` | `Settings` protocol and auth compatibility contract |
| `src/ai_news/config.py` | Exact `LLM_*` environment mapping |
| `src/ai_news/llm/base.py` | Shared strict batch parsing, repair, bounded response, retry, and safe error handling |
| `src/ai_news/llm/anthropic.py` | Anthropic request/response codec |
| `src/ai_news/llm/openai_chat.py` | OpenAI-compatible Chat Completions codec |
| `src/ai_news/llm/factory.py` | Protocol-to-analyzer selection |
| `src/ai_news/pipeline/run.py` | Use the analyzer factory without changing the `Analyzer` interface |
| `src/ai_news/cli.py` | Add non-echoing `check-config` preflight |
| `.github/workflows/daily-news.yml` | Inject only generic model configuration into the generate step |
| `scripts/check_no_secrets.py` | Detect both new and legacy model credential assignments |
| `.env.example`, `README.md` | Document standard API configuration and first deployment |
| `tests/` | Unit, integration, workflow, scanner, CLI, and documentation contracts |

## Task 1: Canonical LLM endpoint safety

**Files:**

- Create: `src/ai_news/llm/endpoint.py`
- Create: `tests/test_llm_endpoint.py`

- [ ] **Step 1: Write failing tests for canonicalization and Coding Plan rejection**

Create `tests/test_llm_endpoint.py` with explicit safe and unsafe cases:

```python
import httpx
import pytest

from ai_news.llm.endpoint import InvalidLLMEndpoint, build_llm_endpoint, validate_llm_base_url


@pytest.mark.parametrize(
    ("base_url", "suffix", "expected"),
    [
        (
            "https://ark.cn-beijing.volces.com/api/v3/",
            "chat/completions",
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        ),
        (
            "https://api.anthropic.com",
            "v1/messages",
            "https://api.anthropic.com/v1/messages",
        ),
        (
            "https://proxy.example/tenant",
            "/v1/messages",
            "https://proxy.example/tenant/v1/messages",
        ),
    ],
)
def test_build_llm_endpoint_preserves_a_safe_base_path(
    base_url: str,
    suffix: str,
    expected: str,
) -> None:
    assert str(build_llm_endpoint(base_url, suffix)) == expected


@pytest.mark.parametrize(
    "base_url",
    [
        "https://ark.cn-beijing.volces.com/api/coding",
        "https://ARK.CN-BEIJING.VOLCES.COM/api/coding/",
        "https://ark.cn-beijing.volces.com/api/coding/v3",
        "https://ark.cn-beijing.volces.com/api/v3/../coding",
        "https://ark.cn-beijing.volces.com/api%2fcoding",
        "https://ark.cn-beijing.volces.com/api%252fcoding",
        "https://ark.cn-beijing.volces.com/api%5ccoding",
    ],
)
def test_rejects_ark_coding_plan_without_building_a_request(base_url: str) -> None:
    with pytest.raises(InvalidLLMEndpoint, match="not allowed"):
        validate_llm_base_url(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.com/v1",
        "https://user:password@api.example.com/v1",
        "https://api.example.com/v1?token=value",
        "https://api.example.com/v1#fragment",
        "https://api.example.com/a\\b",
        "https://api.example.com/%00",
        "https:///missing-host",
    ],
)
def test_rejects_unsafe_base_url_components(base_url: str) -> None:
    with pytest.raises(InvalidLLMEndpoint):
        validate_llm_base_url(base_url)


def test_endpoint_result_is_an_httpx_url() -> None:
    endpoint = build_llm_endpoint("https://api.example.com/root/", "/v1/messages")
    assert isinstance(endpoint, httpx.URL)
    assert endpoint.query == b""
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_llm_endpoint.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'ai_news.llm.endpoint'`.

- [ ] **Step 3: Implement the minimal endpoint validator**

Create `src/ai_news/llm/endpoint.py`. Use `urlsplit`, iterative percent decoding, and
`posixpath.normpath`; do not use a raw substring-only check:

```python
from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

_ARK_HOST = "ark.cn-beijing.volces.com"
_ENCODED_PATH_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_INVALID_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")


class InvalidLLMEndpoint(ValueError):
    """Raised before any request when an LLM base URL is unsafe or unsupported."""


def _decoded_path(raw_path: str) -> str:
    current = raw_path or "/"
    for _ in range(4):
        if _INVALID_PERCENT.search(current) or _ENCODED_PATH_SEPARATOR.search(current):
            raise InvalidLLMEndpoint("invalid encoded LLM path")
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    if "\\" in current or any(ord(character) < 32 for character in current):
        raise InvalidLLMEndpoint("invalid LLM path")
    return current


def validate_llm_base_url(value: str) -> str:
    if "?" in value or "#" in value or any(ord(character) < 32 for character in value):
        raise InvalidLLMEndpoint("invalid LLM base URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise InvalidLLMEndpoint("invalid LLM base URL") from error
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise InvalidLLMEndpoint("invalid LLM base URL")

    decoded_path = _decoded_path(parsed.path)
    normalized_path = posixpath.normpath(decoded_path)
    if not normalized_path.startswith("/") or normalized_path.startswith("//"):
        raise InvalidLLMEndpoint("invalid LLM path")
    coding_path = normalized_path.casefold()
    if parsed.hostname.casefold() == _ARK_HOST and (
        coding_path == "/api/coding" or coding_path.startswith("/api/coding/")
    ):
        raise InvalidLLMEndpoint("Ark Coding Plan endpoints are not allowed")

    host = parsed.hostname.casefold()
    if ":" in host:
        host = f"[{host}]"
    authority = f"{host}:{port}" if port is not None else host
    canonical_path = "" if normalized_path == "/" else normalized_path.rstrip("/")
    return urlunsplit(("https", authority, canonical_path, "", ""))


def build_llm_endpoint(base_url: str, suffix: str) -> httpx.URL:
    canonical = validate_llm_base_url(base_url)
    endpoint = f"{canonical.rstrip('/')}/{suffix.lstrip('/')}"
    return httpx.URL(endpoint)
```

If a test exposes a `urlsplit` edge case, tighten the validator rather than weakening the
test matrix.

- [ ] **Step 4: Verify GREEN and run URL-related regression tests**

Run:

```bash
.venv/bin/pytest tests/test_llm_endpoint.py tests/test_anthropic_client.py -q
```

Expected: the endpoint tests pass; existing Anthropic tests remain green because the analyzer has
not yet adopted the new helper.

- [ ] **Step 5: Commit the endpoint safety boundary**

```bash
git add src/ai_news/llm/endpoint.py tests/test_llm_endpoint.py
git commit -m "feat: validate safe LLM endpoints"
```

## Task 2: Migrate settings to generic `LLM_*`

**Files:**

- Modify: `src/ai_news/models.py`
- Modify: `src/ai_news/config.py`
- Modify: `tests/test_config.py`
- Modify: all tests that directly construct `Settings`

- [ ] **Step 1: Change configuration tests first**

Replace the valid environment helper in `tests/test_config.py`:

```python
def _set_valid_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROTOCOL", "openai-chat")
    monkeypatch.setenv("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "ark-standard-model")
    monkeypatch.setenv("LLM_AUTH_SCHEME", "bearer")
    monkeypatch.setenv("SITE_BASE_PATH", "/ai-news/")
```

Update the primary assertion:

```python
settings = load_settings()
assert settings.llm_protocol == "openai-chat"
assert settings.llm_base_url == "https://ark.cn-beijing.volces.com/api/v3"
assert settings.llm_token.get_secret_value() == "test-secret"
assert settings.llm_model == "ark-standard-model"
assert settings.llm_auth_scheme == "bearer"
```

Add these tests:

```python
def test_openai_chat_requires_bearer_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_settings_environment(monkeypatch)
    monkeypatch.setenv("LLM_AUTH_SCHEME", "x-api-key")
    with pytest.raises(ValueError):
        load_settings()


def test_old_anthropic_environment_is_not_an_implicit_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("LLM_PROTOCOL", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "legacy-secret")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "legacy-model")
    with pytest.raises(ValueError) as exc_info:
        load_settings()
    assert "legacy-secret" not in str(exc_info.value)


def test_settings_reject_ark_coding_plan_without_echoing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_settings_environment(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding")
    with pytest.raises(ValueError) as exc_info:
        load_settings()
    assert "test-secret" not in str(exc_info.value)
```

Mechanically replace old environment names in the remaining tests:

```text
ANTHROPIC_BASE_URL            -> LLM_BASE_URL
ANTHROPIC_AUTH_TOKEN          -> LLM_API_KEY
ANTHROPIC_DEFAULT_OPUS_MODEL  -> LLM_MODEL
ANTHROPIC_AUTH_SCHEME         -> LLM_AUTH_SCHEME
```

Add `llm_protocol="anthropic"` to existing direct `Settings(...)` fixtures that exercise
Anthropic behavior; use `openai-chat` only in the new standard Ark tests.

- [ ] **Step 2: Run configuration tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_config.py -q
```

Expected: failures show that `LLM_*` is not read and `Settings` has no `llm_protocol`.

- [ ] **Step 3: Implement generic settings and protocol/auth validation**

In `src/ai_news/models.py`, import `model_validator` if it is not already imported, import
`validate_llm_base_url`, and update `Settings`:

```python
class Settings(BaseModel):
    llm_protocol: Literal["openai-chat", "anthropic"]
    llm_base_url: str
    llm_token: SecretStr
    llm_model: str
    llm_auth_scheme: Literal["bearer", "x-api-key"] = "bearer"
    site_base_path: str = "/ai-news/"

    @field_validator("llm_base_url", "llm_model", "llm_protocol", mode="before")
    @classmethod
    def strip_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("value must not be empty")
        return value

    @field_validator("llm_base_url")
    @classmethod
    def require_safe_https_base_url(cls, value: str) -> str:
        return validate_llm_base_url(value)

    @model_validator(mode="after")
    def require_protocol_auth_compatibility(self) -> Settings:
        if self.llm_protocol == "openai-chat" and self.llm_auth_scheme != "bearer":
            raise ValueError("openai-chat requires bearer authentication")
        return self
```

Keep the existing token stripping and site base path validators unchanged.

Replace `load_settings()` mapping in `src/ai_news/config.py`:

```python
environment_names = {
    "llm_protocol": "LLM_PROTOCOL",
    "llm_base_url": "LLM_BASE_URL",
    "llm_token": "LLM_API_KEY",
    "llm_model": "LLM_MODEL",
    "llm_auth_scheme": "LLM_AUTH_SCHEME",
    "site_base_path": "SITE_BASE_PATH",
}
```

- [ ] **Step 4: Verify GREEN and all direct Settings call sites**

Run:

```bash
.venv/bin/pytest tests/test_config.py tests/test_anthropic_client.py tests/test_pipeline_run.py tests/test_cli.py tests/test_site_builder.py -q
```

Expected: all selected tests pass with explicit protocols.

- [ ] **Step 5: Commit the configuration migration**

```bash
git add src/ai_news/models.py src/ai_news/config.py tests
git commit -m "feat: migrate to generic LLM settings"
```

Before committing, inspect `git diff --cached --name-only`; only Settings-related test updates
belong in this commit.

## Task 3: Extract the protocol-neutral analysis core

**Files:**

- Create: `src/ai_news/llm/base.py`
- Modify: `src/ai_news/llm/anthropic.py`
- Modify: `tests/test_anthropic_client.py`

This is a behavior-preserving refactor performed while the complete Anthropic suite is green.

- [ ] **Step 1: Record the green refactor baseline**

Run:

```bash
.venv/bin/pytest tests/test_anthropic_client.py -q
```

Expected: all Anthropic client tests pass.

- [ ] **Step 2: Move shared validation and request orchestration into `base.py`**

Move, without semantic changes:

```text
AnalysisError
_AnalysisRow
_read_bounded
_remove_outer_fence
_parse_analysis
_escape_delimiters
_repair_prompt
batch iteration
retry loop
safe exception normalization
```

Define this protocol-neutral class shape in `src/ai_news/llm/base.py`:

```python
class BaseAnalyzer:
    endpoint_suffix: str

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        try:
            self._endpoint = build_llm_endpoint(settings.llm_base_url, self.endpoint_suffix)
        except InvalidLLMEndpoint as error:
            raise AnalysisError("invalid LLM base URL") from error
        self._token = settings.llm_token.get_secret_value()
        self._model = settings.llm_model
        self._auth_scheme = settings.llm_auth_scheme
        self._client = client
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _body(self, prompt: str) -> dict[str, Any]:
        raise NotImplementedError

    def _extract_text(self, response_body: bytes) -> str | None:
        raise NotImplementedError
```

The shared `_request()` must call `_extract_text()`. Preserve:

```python
response = await client.send(
    self._build_request(prompt),
    stream=True,
    auth=None,
    follow_redirects=False,
)
```

and retain the 30-second per-request timeout extension, four total attempts, `1, 2, 4`
delays, response closing, 2 MB bound, one repair, and Secret redaction.

- [ ] **Step 3: Reduce Anthropic implementation to a codec**

`src/ai_news/llm/anthropic.py` must import and re-export `AnalysisError` for compatibility:

```python
from ai_news.llm.base import AnalysisError, BaseAnalyzer
from ai_news.llm.prompt import SYSTEM_PROMPT

__all__ = ["AnalysisError", "AnthropicAnalyzer"]


class AnthropicAnalyzer(BaseAnalyzer):
    endpoint_suffix = "v1/messages"

    def _headers(self) -> dict[str, str]:
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if self._auth_scheme == "bearer":
            headers["Authorization"] = f"Bearer {self._token}"
        else:
            headers["x-api-key"] = self._token
        return headers

    def _body(self, prompt: str) -> dict[str, Any]:
        return {
            "model": self._model,
            "max_tokens": 6000,
            "temperature": 0.2,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }

    def _extract_text(self, response_body: bytes) -> str | None:
        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
            return None
        for block in payload["content"]:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                return text if isinstance(text, str) else None
        return None
```

- [ ] **Step 4: Adopt the endpoint helper and update exact endpoint expectations**

Update Anthropic tests so the default legal test proxy is
`https://proxy.example/tenant/`, with expected endpoint
`https://proxy.example/tenant/v1/messages`. Keep a non-Ark path named `api/coding` in one
test to prove rejection is scoped to the Ark host.

- [ ] **Step 5: Verify the refactor preserves behavior**

Run:

```bash
.venv/bin/pytest tests/test_llm_endpoint.py tests/test_anthropic_client.py -q
```

Expected: all tests pass with identical retry, repair, size, closing, and safe-error assertions.

- [ ] **Step 6: Commit the shared core refactor**

```bash
git add src/ai_news/llm/base.py src/ai_news/llm/anthropic.py tests/test_anthropic_client.py
git commit -m "refactor: share strict LLM analysis core"
```

## Task 4: Add OpenAI Chat codec and analyzer factory

**Files:**

- Create: `src/ai_news/llm/openai_chat.py`
- Create: `src/ai_news/llm/factory.py`
- Create: `tests/test_openai_chat_client.py`
- Create: `tests/test_llm_factory.py`

- [ ] **Step 1: Write failing OpenAI request/response tests**

Create `tests/test_openai_chat_client.py` with these imports and local helpers:

```python
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
from ai_news.models import Candidate, Category, RawItem, Settings
from ai_news.pipeline.normalize import to_candidate


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


def _candidate(slug: str) -> Candidate:
    return to_candidate(
        RawItem(
            source_id=f"source-{slug}",
            source_name=f"Source {slug}",
            source_weight=7,
            title=f"Title {slug}",
            url=f"https://example.com/{slug}",
            published_at=datetime(2026, 7, 27, 8, 30, tzinfo=UTC),
            excerpt="Factual excerpt.",
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


def _analysis_row(candidate: Candidate) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "title": candidate.raw.title,
        "category": candidate.raw.category_hint.value,
        "summary": "这是一段满足最小长度要求且完全基于候选事实的中文摘要内容。",
        "importance": 7,
        "why_it_matters": "这项进展可能影响后续产品能力与行业采用。",
        "tags": ["模型", "发布"],
        "is_official": True,
        "marketing_risk": "low",
        "tracking_signal": "关注官方后续评测和更新。",
    }


def _run_analyze(
    handler: Any,
    items: list[Candidate],
    *,
    sleep: Any = asyncio.sleep,
):
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            analyzer = OpenAIChatAnalyzer(_settings(), client=client, sleep=sleep)
            return await analyzer.analyze(items)

    return asyncio.run(exercise())
```

The central request test must assert:

```python
def test_openai_chat_request_uses_standard_endpoint_bearer_and_messages() -> None:
    candidate = _candidate("standard-request")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_openai_response([_analysis_row(candidate)]))

    result = _run_analyze(handler, [candidate])

    assert list(result) == [candidate.id]
    request = captured[0]
    assert str(request.url) == (
        "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    )
    assert request.headers["authorization"] == "Bearer top-secret-token"
    assert "x-api-key" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == "ark-standard-model"
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 6000
    assert body["n"] == 1
    assert body["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert body["messages"][1]["role"] == "user"
    assert candidate.id in body["messages"][1]["content"]
```

Use this response helper:

```python
def _openai_response(rows: list[dict[str, Any]] | str) -> dict[str, Any]:
    text = rows if isinstance(rows, str) else json.dumps(rows, ensure_ascii=False)
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}
```

Also add:

```python
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": 123}}]},
        {"choices": [{"message": {"content": ""}}]},
    ],
)
def test_openai_chat_rejects_missing_or_non_text_content(payload: dict[str, Any]) -> None:
    candidate = _candidate("invalid-content")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    with pytest.raises(AnalysisError, match="invalid LLM response"):
        _run_analyze(handler, [candidate])


def test_openai_chat_invalid_output_is_repaired_once_without_key_leak() -> None:
    candidate = _candidate("repair")
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts.append(body["messages"][1]["content"])
        if len(prompts) == 1:
            return httpx.Response(
                200,
                json=_openai_response("top-secret-token {invalid"),
                request=request,
            )
        return httpx.Response(
            200,
            json=_openai_response([_analysis_row(candidate)]),
            request=request,
        )

    result = _run_analyze(handler, [candidate])
    assert list(result) == [candidate.id]
    assert len(prompts) == 2
    assert "修复" in prompts[1]
    assert "top-secret-token" not in prompts[1]


def test_openai_chat_inherits_bounded_retry_contract() -> None:
    candidate = _candidate("retry")
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            json=_openai_response([_analysis_row(candidate)]),
            request=request,
        )

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = _run_analyze(handler, [candidate], sleep=record_sleep)
    assert list(result) == [candidate.id]
    assert attempts == 4
    assert delays == [1, 2, 4]
```

Add the bounded-response and redirect tests:

```python
def test_openai_chat_closes_and_rejects_response_over_two_mb() -> None:
    candidate = _candidate("large")
    stream = ClosingStream([b"x" * 1_000_000, b"y" * 1_000_001])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    with pytest.raises(AnalysisError, match="response"):
        _run_analyze(handler, [candidate])
    assert stream.iterations == 2
    assert stream.closed


def test_openai_chat_closes_and_does_not_retry_redirect() -> None:
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

    with pytest.raises(AnalysisError, match="redirect"):
        _run_analyze(handler, [candidate])
    assert attempts == 1
    assert stream.closed
```

- [ ] **Step 2: Write failing factory tests**

Create `tests/test_llm_factory.py`:

```python
from pydantic import SecretStr

from ai_news.llm.anthropic import AnthropicAnalyzer
from ai_news.llm.factory import create_analyzer
from ai_news.llm.openai_chat import OpenAIChatAnalyzer
from ai_news.models import Settings


def _settings(protocol: str) -> Settings:
    return Settings(
        llm_protocol=protocol,
        llm_base_url="https://api.example.com",
        llm_token=SecretStr("test-secret"),
        llm_model="test-model",
        llm_auth_scheme="bearer",
    )


def test_factory_selects_openai_chat() -> None:
    assert isinstance(create_analyzer(_settings("openai-chat")), OpenAIChatAnalyzer)


def test_factory_selects_anthropic() -> None:
    assert isinstance(create_analyzer(_settings("anthropic")), AnthropicAnalyzer)
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_openai_chat_client.py tests/test_llm_factory.py -q
```

Expected: imports for `openai_chat` and `factory` fail.

- [ ] **Step 4: Implement `OpenAIChatAnalyzer`**

Create `src/ai_news/llm/openai_chat.py`:

```python
from __future__ import annotations

import json
from typing import Any

from ai_news.llm.base import BaseAnalyzer
from ai_news.llm.prompt import SYSTEM_PROMPT


class OpenAIChatAnalyzer(BaseAnalyzer):
    endpoint_suffix = "chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _body(self, prompt: str) -> dict[str, Any]:
        return {
            "model": self._model,
            "temperature": 0.2,
            "max_tokens": 6000,
            "n": 1,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }

    def _extract_text(self, response_body: bytes) -> str | None:
        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
            return None
        content = first["message"].get("content")
        return content if isinstance(content, str) and content else None
```

- [ ] **Step 5: Implement the analyzer factory**

Create `src/ai_news/llm/factory.py`:

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable

import asyncio
import httpx

from ai_news.llm.anthropic import AnthropicAnalyzer
from ai_news.llm.base import BaseAnalyzer
from ai_news.llm.openai_chat import OpenAIChatAnalyzer
from ai_news.models import Settings


def create_analyzer(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> BaseAnalyzer:
    analyzer_type = (
        OpenAIChatAnalyzer if settings.llm_protocol == "openai-chat" else AnthropicAnalyzer
    )
    return analyzer_type(settings, client=client, sleep=sleep)
```

- [ ] **Step 6: Verify GREEN and shared safety behavior**

Run:

```bash
.venv/bin/pytest \
  tests/test_llm_endpoint.py \
  tests/test_anthropic_client.py \
  tests/test_openai_chat_client.py \
  tests/test_llm_factory.py \
  -q
```

Expected: all protocol, shared-core, and factory tests pass.

- [ ] **Step 7: Commit the OpenAI protocol**

```bash
git add src/ai_news/llm/openai_chat.py src/ai_news/llm/factory.py \
  tests/test_openai_chat_client.py tests/test_llm_factory.py
git commit -m "feat: support OpenAI-compatible analysis"
```

## Task 5: Integrate factory selection and CLI preflight

**Files:**

- Modify: `src/ai_news/pipeline/run.py`
- Modify: `src/ai_news/cli.py`
- Modify: `tests/test_pipeline_run.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Change the live pipeline test to expect the factory**

In the existing live collector/client reuse test, replace the direct analyzer patch:

```python
def analyzer_factory(settings: Settings, *, client: object) -> _Analyzer:
    assert settings.llm_protocol == "openai-chat"
    assert settings.llm_model == "live-model"
    seen_clients.append(client)
    return analyzer

monkeypatch.setattr(run_module, "create_analyzer", analyzer_factory)
```

Construct settings with:

```python
Settings(
    llm_protocol="openai-chat",
    llm_base_url="https://api.example.com/v3",
    llm_token="llm-secret",
    llm_model="live-model",
)
```

Add a regression test that makes the factory raise and asserts
`AnalysisPipelineError("analyzer initialization failed")` without exposing the key.

- [ ] **Step 2: Write CLI preflight tests**

Add to `tests/test_cli.py`:

```python
def test_check_config_succeeds_without_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda: object())
    assert cli.main(["check-config"]) == 0
    assert capsys.readouterr() == ("", "")


def test_check_config_returns_safe_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "live-standard-api-key"
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: (_ for _ in ()).throw(ValueError(f"invalid config {secret}")),
    )
    assert cli.main(["check-config"]) == 2
    captured = capsys.readouterr()
    assert "configuration_error" in captured.err
    assert secret not in captured.err
    assert captured.out == ""
```

The existing `_safe_error` contract must be used; do not print the exception.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
.venv/bin/pytest \
  tests/test_pipeline_run.py::test_live_collectors_analyzer_and_one_client_are_wired \
  tests/test_cli.py -q
```

Expected: pipeline still imports `AnthropicAnalyzer`, and `check-config` is not recognized.

- [ ] **Step 4: Use the factory in the pipeline**

Replace:

```python
from ai_news.llm.anthropic import AnthropicAnalyzer
```

with:

```python
from ai_news.llm.factory import create_analyzer
```

and replace construction with:

```python
selected_analyzer = create_analyzer(settings, client=client)
```

Keep the existing safe `AnalysisPipelineError` wrapping.

- [ ] **Step 5: Add `check-config` without network access**

In the parser:

```python
commands.add_parser("check-config", help="validate model configuration without a request")
```

Add:

```python
def _check_config() -> int:
    try:
        load_settings()
    except Exception as error:
        _safe_error("configuration_error", error)
        return 2
    return 0
```

Dispatch it before `generate`:

```python
if arguments.command == "check-config":
    return _check_config()
```

- [ ] **Step 6: Verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_pipeline_run.py tests/test_cli.py tests/test_llm_factory.py -q
```

Expected: all selected tests pass, and preflight performs no HTTP request.

- [ ] **Step 7: Commit pipeline and preflight integration**

```bash
git add src/ai_news/pipeline/run.py src/ai_news/cli.py \
  tests/test_pipeline_run.py tests/test_cli.py
git commit -m "feat: select and preflight LLM protocol"
```

## Task 6: Migrate Actions and strengthen Secret scanning

**Files:**

- Modify: `.github/workflows/daily-news.yml`
- Modify: `scripts/check_no_secrets.py`
- Modify: `tests/test_workflows.py`
- Modify: `tests/test_secret_scan.py`

- [ ] **Step 1: Change workflow tests before YAML**

Update the exact generate environment assertion:

```python
assert generate_step["env"] == {
    "LLM_PROTOCOL": "${{ vars.LLM_PROTOCOL }}",
    "LLM_BASE_URL": "${{ vars.LLM_BASE_URL }}",
    "LLM_API_KEY": "${{ secrets.LLM_API_KEY }}",
    "LLM_MODEL": "${{ vars.LLM_MODEL }}",
    "LLM_AUTH_SCHEME": "${{ vars.LLM_AUTH_SCHEME || 'bearer' }}",
    "SITE_BASE_PATH": "/ai-news/",
    "GITHUB_TOKEN": "${{ github.token }}",
    "INPUT_DATE": "${{ inputs.date }}",
}
```

Add:

```python
def test_daily_preflights_config_in_the_generate_step_without_echoing_values() -> None:
    workflow = _load_workflow(DAILY_PATH)
    step = next(
        item for item in _steps(workflow, "generate")
        if item.get("name") == "Generate daily report"
    )
    command = str(step["run"])
    assert command.index("python -m ai_news check-config") < command.index(
        'python -m ai_news generate "${args[@]}"'
    )
    assert "set -x" not in command
    assert "printenv" not in command
    assert "env |" not in command


def test_workflows_contain_no_legacy_model_variables_or_coding_plan_url() -> None:
    source = DAILY_PATH.read_text(encoding="utf-8")
    assert "ANTHROPIC_" not in source
    assert "/api/coding" not in source
```

- [ ] **Step 2: Add scanner tests for the generic key while preserving legacy detection**

Parameterize assignment-key tests over both names:

```python
@pytest.mark.parametrize("key", ["LLM_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
@pytest.mark.parametrize(
    "separator",
    ["=live-standard-secret", ": live-standard-secret"],
)
def test_scan_detects_new_and_legacy_model_credentials(
    tmp_path: Path,
    key: str,
    separator: str,
) -> None:
    repository = _tracked_repository(tmp_path, {"settings.env": f"{key}{separator}"})
    findings = scan_repository(repository)
    assert [(item.path, item.line_number, item.rule) for item in findings] == [
        ("settings.env", 1, "credential assignment")
    ]
    assert "live-standard-secret" not in findings[0].render()
```

Add a placeholder test for `${{ secrets.LLM_API_KEY }}` and retain all old scanner tests.

- [ ] **Step 3: Run tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_workflows.py tests/test_secret_scan.py -q
```

Expected: workflow environment and scanner tests fail against the old names.

- [ ] **Step 4: Migrate the daily workflow**

Change only the `Generate daily report` environment:

```yaml
env:
  LLM_PROTOCOL: ${{ vars.LLM_PROTOCOL }}
  LLM_BASE_URL: ${{ vars.LLM_BASE_URL }}
  LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
  LLM_MODEL: ${{ vars.LLM_MODEL }}
  LLM_AUTH_SCHEME: ${{ vars.LLM_AUTH_SCHEME || 'bearer' }}
  SITE_BASE_PATH: /ai-news/
  GITHUB_TOKEN: ${{ github.token }}
  INPUT_DATE: ${{ inputs.date }}
```

At the start of the existing run script, after `set -euo pipefail`, add:

```bash
python -m ai_news check-config
```

Do not create a separate preflight step because the Secret must remain scoped to the generate
step.

- [ ] **Step 5: Extend the assignment scanner**

Change the credential-key portion of `_ASSIGNMENT_PATTERN` to:

```python
(?P<credential_key>ANTHROPIC_AUTH_TOKEN|LLM_API_KEY)
```

Support quoted and unquoted forms without changing placeholder, multiline, binary, tracked-file,
control-path, or bearer behavior. Keep the legacy name deliberately scanned even though runtime
configuration no longer reads it.

- [ ] **Step 6: Verify workflow and scanner GREEN**

Run:

```bash
.venv/bin/pytest tests/test_workflows.py tests/test_secret_scan.py -q
.venv/bin/python scripts/check_no_secrets.py
```

Expected: tests pass and the repository scan exits 0 without output.

- [ ] **Step 7: Commit workflow and scanner migration**

```bash
git add .github/workflows/daily-news.yml scripts/check_no_secrets.py \
  tests/test_workflows.py tests/test_secret_scan.py
git commit -m "ci: use safe generic LLM credentials"
```

## Task 7: Update operator documentation and examples

**Files:**

- Modify: `.env.example`
- Modify: `README.md`
- Modify: `tests/test_readme_contract.py`
- Modify: `docs/superpowers/specs/2026-07-26-ai-news-github-pages-design.md`

- [ ] **Step 1: Write documentation contract failures**

Replace old variable assertions in `tests/test_readme_contract.py` with:

```python
for name in (
    "LLM_PROTOCOL",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_AUTH_SCHEME",
):
    assert name in readme
assert "https://ark.cn-beijing.volces.com/api/v3" in readme
assert "gh secret set LLM_API_KEY" in readme
assert "/api/coding" in readme
assert "不能用于" in readme
```

Add repository-wide runtime-document assertions:

```python
def test_runtime_docs_do_not_instruct_use_of_legacy_model_variables() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    env_example = (REPOSITORY / ".env.example").read_text(encoding="utf-8")
    assert "ANTHROPIC_" not in readme
    assert "ANTHROPIC_" not in env_example


def test_env_example_uses_safe_non_secret_placeholders() -> None:
    env_example = (REPOSITORY / ".env.example").read_text(encoding="utf-8")
    assert "LLM_PROTOCOL=openai-chat" in env_example
    assert "LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3" in env_example
    assert "LLM_API_KEY=replace-with-a-repository-secret" in env_example
    assert "LLM_MODEL=replace-with-a-standard-api-model-id" in env_example
```

- [ ] **Step 2: Run documentation tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_readme_contract.py -q
```

Expected: tests fail because README and `.env.example` still use `ANTHROPIC_*`.

- [ ] **Step 3: Replace `.env.example`**

Use exactly:

```dotenv
LLM_PROTOCOL=openai-chat
LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
LLM_API_KEY=replace-with-a-repository-secret
LLM_MODEL=replace-with-a-standard-api-model-id
LLM_AUTH_SCHEME=bearer
SITE_BASE_PATH=/ai-news/
```

- [ ] **Step 4: Update README implementation and operations sections**

Document:

```bash
export LLM_PROTOCOL="openai-chat"
export LLM_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
export LLM_MODEL="$(read -r value; printf '%s' "$value")"
export LLM_AUTH_SCHEME="bearer"
read -r -s LLM_API_KEY
export LLM_API_KEY
python -m ai_news check-config
python -m ai_news generate --root .
```

Use a clearer two-line interactive model example instead of command substitution if shell
linting or readability is worse:

```bash
read -r LLM_MODEL
export LLM_MODEL
```

State definitively:

- `/api/coding` and Coding Plan keys cannot be used by this project;
- standard Ark uses `/api/v3`;
- the standard account model ID must be confirmed in the provider console;
- the key goes directly to GitHub Secret or hidden terminal input, never chat or a file;
- first Pages deployment still requires one successful real report.

Update project structure from “Anthropic 协议适配” to “双协议模型适配与严格响应校验”.

- [ ] **Step 5: Align the original architecture design**

Add a dated amendment to
`docs/superpowers/specs/2026-07-26-ai-news-github-pages-design.md` pointing to the new
canonical supplement:

```markdown
## 2026-07-27 模型协议补充

模型配置、协议选择和 Coding Plan 安全边界已由
`docs/superpowers/specs/2026-07-27-standard-ark-api-design.md` 更新。发生冲突时，
以该补充设计为准；采集、排序、存储、站点和发布阈值继续以本文为准。
```

- [ ] **Step 6: Verify documentation and Secret scan**

Run:

```bash
.venv/bin/pytest tests/test_readme_contract.py tests/test_secret_scan.py -q
.venv/bin/python scripts/check_no_secrets.py
```

Expected: tests and scan pass with no credential value printed.

- [ ] **Step 7: Commit documentation migration**

```bash
git add .env.example README.md tests/test_readme_contract.py \
  docs/superpowers/specs/2026-07-26-ai-news-github-pages-design.md
git commit -m "docs: explain standard Ark API setup"
```

## Task 8: Complete local verification and independent review

**Files:**

- Modify only files required by review findings

- [ ] **Step 1: Run formatting and lint**

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Expected: both commands exit 0.

- [ ] **Step 2: Run the full test and coverage gate**

```bash
.venv/bin/pytest --cov=ai_news --cov-report=term-missing --cov-fail-under=85
```

Expected: all tests pass and total coverage is at least 85%.

- [ ] **Step 3: Run security and offline site gates**

Use a fresh temporary root so existing demo artifacts cannot hide failures:

```bash
verify_root="$(mktemp -d /tmp/ai-news-ark-verify.XXXXXX)"
.venv/bin/python scripts/check_no_secrets.py
.venv/bin/python -m ai_news demo --root "$verify_root/demo" --output "$verify_root/dist"
.venv/bin/python scripts/validate_site.py "$verify_root/dist" --base-path /ai-news/
git diff --check
git status --short --branch
```

Expected: all commands exit 0; only intentional feature files appear before final commit.

- [ ] **Step 4: Run workflow lint**

Use the repository-pinned actionlint image if Docker is available. Otherwise use the same
version through Go:

```bash
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7 \
  -ignore 'element of "schedule" section must be mapping and must contain one key "cron"' \
  .github/workflows/ci.yml .github/workflows/daily-news.yml
```

Expected: exit 0.

- [ ] **Step 5: Dispatch specification review**

Give a fresh reviewer:

- the full text of `docs/superpowers/specs/2026-07-27-standard-ark-api-design.md`;
- the commit range `acc48e6..HEAD`;
- instructions to report missing, extra, or contradictory behavior only.

Expected: APPROVED. Any finding goes back to the same implementer, followed by re-review.

- [ ] **Step 6: Dispatch code-quality review**

Only after specification approval, ask a fresh quality reviewer to inspect:

- endpoint normalization and bypass resistance;
- Secret handling and error state;
- shared-core regression risk;
- OpenAI response edge cases;
- workflow permissions and Secret scope;
- test quality and maintainability.

Expected: APPROVED. Important findings return to the same implementer and are re-reviewed.

- [ ] **Step 7: Commit review fixes and re-run the complete gate**

Use narrowly scoped commit messages for actual findings. Re-run Steps 1–4 after the last fix.

## Task 9: Merge, migrate repository configuration, and publish the first real report

**Files:**

- No planned source edits
- External state: `main`, GitHub Variables, GitHub Secret, Actions runs, Pages deployment

- [ ] **Step 1: Verify branch scope and fast-forward eligibility**

From the feature worktree:

```bash
git status --short --branch
git log --oneline --decorate main..HEAD
git diff --stat main...HEAD
git merge-base --is-ancestor main HEAD
```

Expected: clean branch, only intentional commits, and the ancestor check exits 0.

- [ ] **Step 2: Fast-forward local main**

From `/Users/suhuashan/Documents/AI资讯`:

```bash
git fetch origin
git status --short --branch
git merge --ff-only codex/ark-standard-api
git rev-parse HEAD
```

Expected: clean `main` fast-forwards to the reviewed feature HEAD.

- [ ] **Step 3: Push and verify remote parity**

```bash
git push origin main
git fetch origin
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
gh repo view mount-su/ai-news --json visibility,defaultBranchRef,url
```

Expected: push succeeds, SHAs match, repository remains public with default branch `main`.

- [ ] **Step 4: Verify online CI before changing model configuration**

```bash
ci_run_id="$(
  gh run list --repo mount-su/ai-news --workflow ci.yml --limit 1 \
    --json databaseId --jq '.[0].databaseId'
)"
test -n "$ci_run_id"
gh run view "$ci_run_id" --repo mount-su/ai-news \
  --json headSha,status,conclusion,url
gh run watch "$ci_run_id" --repo mount-su/ai-news --exit-status
unset ci_run_id
```

Expected: CI for the pushed HEAD concludes `success`.

- [ ] **Step 5: Remove the inert legacy Repository Variables**

```bash
gh variable delete ANTHROPIC_BASE_URL --repo mount-su/ai-news
gh variable delete ANTHROPIC_DEFAULT_OPUS_MODEL --repo mount-su/ai-news
gh variable delete ANTHROPIC_AUTH_SCHEME --repo mount-su/ai-news
```

If a variable is already absent, verify with `gh variable list` and continue.

- [ ] **Step 6: Configure non-secret standard Ark Variables**

```bash
gh variable set LLM_PROTOCOL --body "openai-chat" --repo mount-su/ai-news
gh variable set LLM_BASE_URL \
  --body "https://ark.cn-beijing.volces.com/api/v3" \
  --repo mount-su/ai-news
gh variable set LLM_AUTH_SCHEME --body "bearer" --repo mount-su/ai-news
read -r LLM_MODEL_VALUE
test -n "$LLM_MODEL_VALUE"
gh variable set LLM_MODEL --body "$LLM_MODEL_VALUE" --repo mount-su/ai-news
unset LLM_MODEL_VALUE
gh variable list --repo mount-su/ai-news
```

The value entered for `LLM_MODEL_VALUE` must be copied from the standard Ark account's
available model or endpoint list. Do not reuse `glm-5.2` merely because it appeared in Coding
Plan configuration.

- [ ] **Step 7: Set the standard API key without chat or shell-history exposure**

Use one of:

```bash
gh secret set LLM_API_KEY --repo mount-su/ai-news
```

or GitHub → Settings → Secrets and variables → Actions. Paste the standard Ark API key only
into the hidden prompt or GitHub form. Do not pass it through `--body`, a file, environment dump,
chat, or command-line argument.

Verify only the Secret name:

```bash
gh secret list --repo mount-su/ai-news
```

Expected: `LLM_API_KEY` appears; its value is never returned.

- [ ] **Step 8: Dispatch the first real generation**

```bash
gh workflow run daily-news.yml --repo mount-su/ai-news
gh run list --repo mount-su/ai-news --workflow daily-news.yml --limit 1 \
  --json databaseId,status,conclusion,headSha,url
```

Watch the exact run ID:

```bash
run_id="$(
  gh run list --repo mount-su/ai-news --workflow daily-news.yml --limit 1 \
    --json databaseId --jq '.[0].databaseId'
)"
test -n "$run_id"
gh run watch "$run_id" --repo mount-su/ai-news --exit-status
unset run_id
```

Expected: generate, build, and deploy all conclude `success`.

- [ ] **Step 9: Verify generated commit scope and remote data**

```bash
git fetch origin
git diff --name-only HEAD..origin/main
git show --stat --oneline origin/main
git show --name-only --format= origin/main
```

Expected: the bot commit contains only `data/` and `content/`; workflow and source files are
unchanged. Fast-forward local `main` after inspection:

```bash
git merge --ff-only origin/main
```

- [ ] **Step 10: Verify production Pages and interactions**

First verify HTTP and Pages metadata:

```bash
gh api repos/mount-su/ai-news/pages \
  --jq '{html_url,build_type,https_enforced,status}'
curl -fsS -o /dev/null -w '%{http_code}\n' https://mount-su.github.io/ai-news/
```

Expected: `build_type` is `workflow`, HTTPS is enforced, and the page returns `200`.

Use the browser at desktop 1440 px and mobile 390 px to verify:

- homepage assets load without failed requests or console errors;
- latest real report date and at least five cards are visible;
- search narrows results;
- category filter changes `aria-pressed`;
- details disclosure opens;
- archive and category links return 200;
- no horizontal overflow occurs;
- keyboard focus is visible.

- [ ] **Step 11: Final Secret and schedule audit**

```bash
.venv/bin/python scripts/check_no_secrets.py
gh run list --repo mount-su/ai-news --limit 10 \
  --json name,event,status,conclusion,headSha,url
gh api repos/mount-su/ai-news/actions/permissions
```

Inspect the deployed HTML and JSON for credential names or values without printing any Secret.
Confirm `.github/workflows/daily-news.yml` still schedules `17 8 * * *` with
`Asia/Shanghai`.

Completion requires:

- reviewed code on remote `main`;
- online CI success for that SHA;
- one real report committed;
- Daily workflow generate/build/deploy success;
- production Pages HTTP 200 and browser checks passing;
- Secret scan clean and no credential in repository, logs, or Pages artifacts.
