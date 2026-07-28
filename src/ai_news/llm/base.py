# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

from ai_news.llm.endpoint import InvalidLLMEndpoint, build_llm_endpoint
from ai_news.llm.prompt import build_analysis_prompt
from ai_news.models import Analysis, Candidate, Category, Settings

_BATCH_SIZE = 10
_LLM_REQUEST_TIMEOUT_SECONDS = 120
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_REPAIR_TEXT_CHARS = 20_000
_OUTER_FENCE = re.compile(r"\A```(?:json)?[ \t]*\r?\n(.*?)\r?\n?```[ \t]*\Z", re.I | re.S)


class AnalysisError(RuntimeError):
    """A safe public error for all analysis failures."""

    def __init__(self, message: str, *, safe_code: str = "internal_error") -> None:
        super().__init__(message)
        self.safe_code = safe_code


class _AnalysisRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{16}$")]
    title: StrictStr
    category: Category
    summary: Annotated[StrictStr, Field(min_length=20, max_length=1200)]
    importance: Annotated[StrictInt, Field(ge=1, le=10)]
    why_it_matters: Annotated[StrictStr, Field(min_length=10, max_length=800)]
    tags: list[Annotated[StrictStr, Field(min_length=1, max_length=50)]] = Field(
        min_length=1,
        max_length=6,
    )
    is_official: StrictBool
    marketing_risk: Literal["low", "medium", "high"]
    tracking_signal: Annotated[StrictStr, Field(min_length=5, max_length=500)]

    @field_validator("tags")
    @classmethod
    def reject_blank_tags(cls, tags: list[str]) -> list[str]:
        if any(not tag.strip() for tag in tags):
            raise ValueError("tags must not be blank")
        return tags


async def _read_bounded(response: httpx.Response) -> bytes | None:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > _MAX_RESPONSE_BYTES:
            return None

    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > _MAX_RESPONSE_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _remove_outer_fence(text: str) -> str:
    stripped = text.strip()
    match = _OUTER_FENCE.fullmatch(stripped)
    return match.group(1) if match else stripped


def _parse_analysis(
    text: str,
    expected_ids: list[str],
) -> tuple[dict[str, Analysis] | None, str | None]:
    try:
        payload = json.loads(_remove_outer_fence(text))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None, "json"
    if not isinstance(payload, list):
        return None, "schema"

    parsed_rows: list[_AnalysisRow] = []
    try:
        for raw_row in payload:
            parsed_rows.append(_AnalysisRow.model_validate(raw_row))
    except (ValidationError, TypeError, ValueError):
        return None, "schema"

    row_ids = [row.id for row in parsed_rows]
    if len(row_ids) != len(set(row_ids)):
        return None, "duplicate"
    expected_set = set(expected_ids)
    if any(row_id not in expected_set for row_id in row_ids):
        return None, "unknown"
    if any(expected_id not in set(row_ids) for expected_id in expected_ids):
        return None, "missing"

    by_id = {row.id: Analysis.model_validate(row.model_dump(exclude={"id"})) for row in parsed_rows}
    return {candidate_id: by_id[candidate_id] for candidate_id in expected_ids}, None


def _contains_secret(value: object, token: str) -> bool:
    if not token:
        return False
    if isinstance(value, str):
        return token in value
    if isinstance(value, BaseModel):
        return _contains_secret(value.model_dump(mode="python"), token)
    if isinstance(value, Mapping):
        return any(
            _contains_secret(key, token) or _contains_secret(item, token)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple | set | frozenset):
        return any(_contains_secret(item, token) for item in value)
    return False


def _escape_delimiters(value: str) -> str:
    return value.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _repair_prompt(
    items: list[Candidate],
    invalid_text: str,
    error_kind: str,
    token: str,
) -> str:
    safe_invalid_text = invalid_text.replace(token, "[REDACTED]") if token else invalid_text
    safe_invalid_text = _escape_delimiters(safe_invalid_text)[:_MAX_REPAIR_TEXT_CHARS]
    return f"""{build_analysis_prompt(items)}
上一次输出无效，错误种类：{error_kind}。
请修复为符合固定 schema 和候选 ID 集合的结果，仍然仅输出 JSON array。
<invalid_output>
{safe_invalid_text}
</invalid_output>"""


class BaseAnalyzer(ABC):
    endpoint_suffix: str

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        try:
            endpoint = build_llm_endpoint(settings.llm_base_url, self.endpoint_suffix)
        except (InvalidLLMEndpoint, httpx.InvalidURL, TypeError, ValueError):
            raise AnalysisError("invalid LLM base URL", safe_code="invalid_endpoint") from None
        self._endpoint = endpoint
        self._token = settings.llm_token.get_secret_value()
        self._model = settings.llm_model
        self._auth_scheme = settings.llm_auth_scheme
        self._client = client
        self._sleep = sleep

    @abstractmethod
    def _headers(self) -> dict[str, str]:
        """Return protocol-specific request headers."""

    @abstractmethod
    def _body(self, prompt: str) -> dict[str, Any]:
        """Return the protocol-specific JSON request body."""

    @abstractmethod
    def _extract_text(self, response_body: bytes) -> str | None:
        """Extract analysis text from a protocol-specific response."""

    def _build_request(self, prompt: str) -> httpx.Request:
        return httpx.Request(
            "POST",
            self._endpoint,
            headers=self._headers(),
            json=self._body(prompt),
            extensions={
                "timeout": httpx.Timeout(_LLM_REQUEST_TIMEOUT_SECONDS).as_dict(),
            },
        )

    async def _request(self, client: httpx.AsyncClient, prompt: str) -> str:
        for retry_number in range(4):
            retry = False
            terminal_error: str | None = None
            terminal_code: str | None = None
            response_body: bytes | None = None
            try:
                response = await client.send(
                    self._build_request(prompt),
                    stream=True,
                    auth=None,
                    follow_redirects=False,
                )
                try:
                    if response.status_code == 429 or 500 <= response.status_code <= 599:
                        retry = True
                    elif 300 <= response.status_code <= 399:
                        terminal_error = "LLM redirect rejected"
                        terminal_code = "redirect"
                    elif not 200 <= response.status_code <= 299:
                        terminal_error = "LLM HTTP request failed"
                        terminal_code = f"http_{response.status_code}"
                    else:
                        response_body = await _read_bounded(response)
                        if response_body is None:
                            terminal_error = "LLM response exceeds size limit"
                            terminal_code = "response_too_large"
                finally:
                    await response.aclose()
            except httpx.TransportError:
                retry = True

            if terminal_error is not None:
                raise AnalysisError(
                    terminal_error,
                    safe_code=terminal_code or "internal_error",
                )
            if not retry:
                if response_body is None:
                    raise AnalysisError("invalid LLM response", safe_code="invalid_response")
                text = self._extract_text(response_body)
                if text is None:
                    raise AnalysisError("invalid LLM response", safe_code="invalid_response")
                return text
            if retry_number == 3:
                raise AnalysisError(
                    "LLM request failed after retries",
                    safe_code="retry_exhausted",
                )
            await self._sleep(2**retry_number)
        raise RuntimeError("unreachable")

    async def _analyze_batch(
        self,
        client: httpx.AsyncClient,
        items: list[Candidate],
    ) -> dict[str, Analysis]:
        expected_ids = [item.id for item in items]
        first_text = await self._request(client, build_analysis_prompt(items))
        parsed, error_kind = _parse_analysis(first_text, expected_ids)
        if parsed is not None:
            if _contains_secret(parsed, self._token):
                raise AnalysisError(
                    "LLM analysis contains sensitive data",
                    safe_code="sensitive_output",
                )
            return parsed

        repair = _repair_prompt(items, first_text, error_kind or "schema", self._token)
        repaired_text = await self._request(client, repair)
        repaired, _ = _parse_analysis(repaired_text, expected_ids)
        if repaired is None:
            raise AnalysisError(
                "invalid LLM analysis after repair",
                safe_code="invalid_after_repair",
            )
        if _contains_secret(repaired, self._token):
            raise AnalysisError(
                "LLM analysis contains sensitive data",
                safe_code="sensitive_output",
            )
        return repaired

    async def _analyze_with_client(
        self,
        client: httpx.AsyncClient,
        items: list[Candidate],
    ) -> dict[str, Analysis]:
        aggregate: dict[str, Analysis] = {}
        for offset in range(0, len(items), _BATCH_SIZE):
            batch = items[offset : offset + _BATCH_SIZE]
            aggregate.update(await self._analyze_batch(client, batch))
        return aggregate

    async def analyze(self, items: list[Candidate]) -> dict[str, Analysis]:
        if not items:
            raise AnalysisError("analysis items must not be empty", safe_code="invalid_input")
        ids = [item.id for item in items]
        if len(ids) != len(set(ids)):
            raise AnalysisError("duplicate input candidate id", safe_code="invalid_input")

        safe_error: AnalysisError
        try:
            if self._client is not None:
                return await self._analyze_with_client(self._client, items)
            async with httpx.AsyncClient() as client:
                return await self._analyze_with_client(client, items)
        except AnalysisError as error:
            safe_error = error
        except Exception:
            safe_error = AnalysisError("analysis failed")
        raise safe_error
