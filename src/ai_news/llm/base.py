# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections import Counter
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
from ai_news.models import (
    Analysis,
    Candidate,
    Category,
    EditorialLane,
    Settings,
)

_LLM_REQUEST_TIMEOUT_SECONDS = 120
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_REPAIR_TEXT_CHARS = 20_000
_OUTER_FENCE = re.compile(r"\A```(?:json)?[ \t]*\r?\n(.*?)\r?\n?```[ \t]*\Z", re.I | re.S)
_SENSITIVE_TEXT = re.compile(
    r"""(?ix)
    (?:
        (?<!\w)(?:\\?["'])?(?:
            (?:llm[_-]?)?api[_ -]?key
          | anthropic[_-]auth[_-]token
          | github[_-]token
          | access[_-]token
          | secret[_-](?:access[_-])?key
          | client[_-]secret
        )(?:\\?["'])?
        \s*[:=]\s*(?:\\?["'])?[A-Za-z0-9_./+=-]{8,}
      | \bauthorization\b\s*[:=]\s*bearer\s+[A-Za-z0-9_./+=-]{8,}
      | \bsk-[A-Za-z0-9][A-Za-z0-9_-]{15,}
      | \bgh[pousr]_[A-Za-z0-9]{20,}
      | \bgithub_pat_[A-Za-z0-9_]{20,}
      | \bAIza[A-Za-z0-9_-]{20,}
      | \bAKIA[A-Z0-9]{16}\b
      | \bxox[baprs]-[A-Za-z0-9-]{16,}
    )
    """
)


class AnalysisError(RuntimeError):
    """A safe public error for all analysis failures."""

    def __init__(self, message: str, *, safe_code: str = "internal_error") -> None:
        super().__init__(message)
        self.safe_code = safe_code


class _AnalysisRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{16}$")]
    title: Annotated[StrictStr, Field(min_length=1, max_length=80)]
    editorial_lane: EditorialLane
    category: Category
    summary: Annotated[StrictStr, Field(min_length=20, max_length=160)]
    importance: Annotated[StrictInt, Field(ge=1, le=10)]
    why_it_matters: Annotated[StrictStr, Field(min_length=10, max_length=100)]
    tags: list[Annotated[StrictStr, Field(min_length=1, max_length=50)]] = Field(
        min_length=1,
        max_length=3,
    )
    is_official: StrictBool
    marketing_risk: Literal["low", "medium", "high"]
    tracking_signal: Annotated[StrictStr, Field(min_length=5, max_length=80)]

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


def _json_payload(text: str) -> object:
    stripped = _remove_outer_fence(text)
    decoder = json.JSONDecoder()
    try:
        payload, _ = decoder.raw_decode(stripped)
        return payload
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        pass

    for index, character in enumerate(stripped):
        if character not in "[{":
            continue
        try:
            payload, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        return payload
    raise json.JSONDecodeError("no JSON payload", stripped, 0)


def _analysis_rows(payload: object) -> list[object] | None:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("items", "rows", "analyses", "analysis", "data"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return rows
    return None


def _trim_text(value: object, *, max_length: int) -> object:
    if not isinstance(value, str):
        return value
    return value.strip()[:max_length]


def _normalize_tags(value: object) -> object:
    if not isinstance(value, list):
        return value
    tags: list[str] = []
    for raw_tag in value:
        if not isinstance(raw_tag, str):
            return value
        tag = raw_tag.strip()
        if len(tag) > 50:
            continue
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) == 3:
            break
    return tags


def _normalize_analysis_row(raw_row: object) -> object:
    if not isinstance(raw_row, Mapping):
        return raw_row
    row = dict(raw_row)
    text_limits = {
        "title": 80,
        "summary": 160,
        "why_it_matters": 100,
        "tracking_signal": 80,
    }
    for field_name, max_length in text_limits.items():
        if field_name in row:
            row[field_name] = _trim_text(row[field_name], max_length=max_length)
    if isinstance(row.get("importance"), str):
        stripped = row["importance"].strip()
        if stripped.isdigit():
            row["importance"] = int(stripped)
    if isinstance(row.get("is_official"), str):
        match row["is_official"].strip().casefold():
            case "true":
                row["is_official"] = True
            case "false":
                row["is_official"] = False
    if "tags" in row:
        row["tags"] = _normalize_tags(row["tags"])
    return row


def _parse_analysis(
    text: str,
    candidates: list[Candidate],
    *,
    normalize_provider_drift: bool = False,
) -> tuple[dict[str, Analysis] | None, str | None]:
    try:
        payload = _json_payload(text)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None, "json"
    rows = _analysis_rows(payload)
    if rows is None:
        return None, "schema"

    parsed_rows: list[_AnalysisRow] = []
    try:
        for raw_row in rows:
            row = _normalize_analysis_row(raw_row) if normalize_provider_drift else raw_row
            parsed_rows.append(_AnalysisRow.model_validate(row))
    except (ValidationError, TypeError, ValueError):
        return None, "schema"

    selection_count = min(9, len(candidates))
    minimum_selection_count = 5 if len(candidates) >= 5 else 1
    row_ids = [row.id for row in parsed_rows]
    if len(row_ids) != len(set(row_ids)):
        return None, "duplicate"
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    if any(row_id not in candidate_by_id for row_id in row_ids):
        return None, "unknown"
    if not row_ids:
        return None, "missing"
    if len(row_ids) < minimum_selection_count:
        return None, "count"
    if len(row_ids) > selection_count:
        return None, "count"
    if selection_count == 9:
        # Legacy 1.1 fixtures used a balanced three-lane contract. Keep
        # rejecting malformed legacy responses while allowing the current
        # consumer brief to use whichever lanes have eligible stories.
        legacy_lanes = {
            EditorialLane.PRODUCT_ENGINEERING,
            EditorialLane.BUSINESS,
            EditorialLane.RESEARCH,
        }
        lane_counts = Counter(row.editorial_lane for row in parsed_rows)
        if set(lane_counts) <= legacy_lanes and any(count > 4 for count in lane_counts.values()):
            return None, "lane_distribution"
        source_counts = Counter(candidate_by_id[row.id].raw.source_id for row in parsed_rows)
        if any(count > 3 for count in source_counts.values()):
            return None, "source_distribution"

    return {
        row.id: Analysis.model_validate(row.model_dump(exclude={"id"})) for row in parsed_rows
    }, None


def _contains_sensitive_data(value: object, token: str) -> bool:
    if isinstance(value, str):
        return bool(token and token in value) or _SENSITIVE_TEXT.search(value) is not None
    if isinstance(value, BaseModel):
        return _contains_sensitive_data(value.model_dump(mode="python"), token)
    if isinstance(value, Mapping):
        return any(
            _contains_sensitive_data(key, token) or _contains_sensitive_data(item, token)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple | set | frozenset):
        return any(_contains_sensitive_data(item, token) for item in value)
    return False


def _redact_sensitive_text(value: str, token: str) -> str:
    redacted = value.replace(token, "[REDACTED]") if token else value
    return _SENSITIVE_TEXT.sub("[REDACTED]", redacted)


def _escape_delimiters(value: str) -> str:
    return value.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _repair_prompt(
    items: list[Candidate],
    invalid_text: str,
    error_kind: str,
    token: str,
) -> str:
    safe_invalid_text = _redact_sensitive_text(invalid_text, token)
    safe_invalid_text = _escape_delimiters(safe_invalid_text)[:_MAX_REPAIR_TEXT_CHARS]
    return f"""{build_analysis_prompt(items)}
上一次输出无效，错误种类：{error_kind}。
请修复为符合固定 schema 和候选 ID 集合的结果，仍然仅输出 JSON array。
<invalid_output>
{safe_invalid_text}
</invalid_output>"""


class BaseAnalyzer(ABC):
    endpoint_suffix: str
    normalize_provider_drift = False

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

    async def _analyze_editorial_selection(
        self,
        client: httpx.AsyncClient,
        items: list[Candidate],
    ) -> dict[str, Analysis]:
        first_text = await self._request(client, build_analysis_prompt(items))
        parsed, error_kind = _parse_analysis(
            first_text,
            items,
            normalize_provider_drift=self.normalize_provider_drift,
        )
        if parsed is not None:
            if _contains_sensitive_data(parsed, self._token):
                raise AnalysisError(
                    "LLM analysis contains sensitive data",
                    safe_code="sensitive_output",
                )
            return parsed

        repair = _repair_prompt(items, first_text, error_kind or "schema", self._token)
        repaired_text = await self._request(client, repair)
        repaired, _ = _parse_analysis(
            repaired_text,
            items,
            normalize_provider_drift=self.normalize_provider_drift,
        )
        if repaired is None:
            raise AnalysisError(
                "invalid LLM analysis after repair",
                safe_code="invalid_after_repair",
            )
        if _contains_sensitive_data(repaired, self._token):
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
        return await self._analyze_editorial_selection(client, items)

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
