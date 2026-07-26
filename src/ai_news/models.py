from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)
_GITHUB_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_GITHUB_REPO_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class Category(StrEnum):
    MODEL = "大模型"
    AGENT = "Agent"
    CODING_AGENT = "Coding Agent"
    TOOL = "AI 工具"
    OPEN_SOURCE = "开源项目"
    RESEARCH = "论文研究"


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    name: str
    kind: Literal["feed", "arxiv", "github"]
    url: HttpUrl | None = None
    repo: str | None = None
    category: Category
    weight: int = Field(default=5, ge=1, le=10)
    official: bool = True
    enabled: bool = True

    @model_validator(mode="after")
    def require_source_locator(self) -> SourceSpec:
        if self.kind in {"feed", "arxiv"} and self.url is None:
            raise ValueError(f"{self.kind} sources require a url")
        if self.kind == "github":
            if self.repo is None:
                raise ValueError("github sources require a repo")
            repo = self.repo.strip()
            parts = repo.split("/")
            if len(parts) != 2:
                raise ValueError("github repo must use owner/repo format")
            owner, repo_name = parts
            if (
                len(owner) > 39
                or not _GITHUB_OWNER_PATTERN.fullmatch(owner)
                or repo_name in {".", ".."}
                or not _GITHUB_REPO_NAME_PATTERN.fullmatch(repo_name)
            ):
                raise ValueError("github repo must use a valid GitHub owner and repository name")
            self.repo = repo
        return self


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[SourceSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_source_ids(self) -> SourceConfig:
        source_ids = [source.id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source ids must be unique")
        return self


class Settings(BaseModel):
    llm_base_url: str
    llm_token: SecretStr
    llm_model: str
    llm_auth_scheme: Literal["bearer", "x-api-key"] = "bearer"
    site_base_path: str = "/ai-news/"

    @field_validator("llm_base_url", "llm_model", mode="before")
    @classmethod
    def strip_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("value must not be empty")
        return value

    @field_validator("llm_base_url")
    @classmethod
    def require_https_base_url(cls, value: str) -> str:
        parsed_url = _HTTP_URL_ADAPTER.validate_python(value)
        original_url = urlsplit(value)
        if parsed_url.scheme != "https" or original_url.hostname is None:
            raise ValueError("llm_base_url must use https")
        return value

    @field_validator("llm_token")
    @classmethod
    def strip_and_require_token(cls, value: SecretStr) -> SecretStr:
        secret_value = value.get_secret_value().strip()
        if not secret_value:
            raise ValueError("llm_token must not be empty")
        return SecretStr(secret_value)

    @field_validator("site_base_path")
    @classmethod
    def require_boundary_slashes(cls, value: str) -> str:
        if not value.startswith("/") or not value.endswith("/"):
            raise ValueError("site_base_path must start and end with '/'")
        return value


class RawItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    source_name: str
    source_weight: int = Field(ge=1, le=10)
    title: str
    url: HttpUrl
    published_at: datetime
    excerpt: str = ""
    category_hint: Category
    is_official_source: bool


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[0-9a-f]{16}$")
    canonical_url: HttpUrl
    raw: RawItem

    @field_validator("canonical_url", mode="before")
    @classmethod
    def reject_noncanonical_input(cls, value: object) -> object:
        from ai_news.pipeline.normalize import canonical_url

        original_url = str(value)
        try:
            normalized_url = canonical_url(original_url)
        except (TypeError, ValueError) as error:
            raise ValueError("canonical_url must be a canonical HTTPS URL") from error
        if normalized_url != original_url:
            raise ValueError("canonical_url must already be canonical")
        return value

    @model_validator(mode="after")
    def require_canonical_consistency(self) -> Candidate:
        from ai_news.pipeline.normalize import canonical_url

        candidate_url = str(self.canonical_url)
        try:
            normalized_candidate_url = canonical_url(candidate_url)
        except ValueError as error:
            raise ValueError("canonical_url must be a canonical HTTPS URL") from error
        if normalized_candidate_url != candidate_url:
            raise ValueError("canonical_url must already be canonical")

        expected_id = hashlib.sha256(candidate_url.encode("utf-8")).hexdigest()[:16]
        if self.id != expected_id:
            raise ValueError("id must match the canonical_url SHA-256 prefix")

        if canonical_url(self.raw.url) != candidate_url:
            raise ValueError("raw URL must resolve to canonical_url")
        return self


class Analysis(BaseModel):
    title: str
    category: Category
    summary: str = Field(min_length=20, max_length=1200)
    importance: int = Field(ge=1, le=10)
    why_it_matters: str = Field(min_length=10, max_length=800)
    tags: list[str] = Field(min_length=1, max_length=6)
    is_official: bool
    marketing_risk: Literal["low", "medium", "high"]
    tracking_signal: str = Field(min_length=5, max_length=500)


def _require_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class NewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[0-9a-f]{16}$")
    canonical_url: HttpUrl
    original_title: str = Field(min_length=1, max_length=1000)
    source: str = Field(min_length=1, max_length=300)
    published_at: datetime
    title: str = Field(min_length=1, max_length=1000)
    category: Category
    summary: str = Field(min_length=20, max_length=1200)
    importance: int = Field(ge=1, le=10)
    why_it_matters: str = Field(min_length=10, max_length=800)
    tags: list[str] = Field(min_length=1, max_length=6)
    is_official: bool
    marketing_risk: Literal["low", "medium", "high"]
    tracking_signal: str = Field(min_length=5, max_length=500)

    @field_validator("canonical_url", mode="before")
    @classmethod
    def require_canonical_https_url(cls, value: object) -> object:
        from ai_news.pipeline.normalize import canonical_url

        original_url = str(value)
        try:
            normalized_url = canonical_url(original_url)
        except (TypeError, ValueError) as error:
            raise ValueError("canonical_url must be a canonical HTTPS URL") from error
        if normalized_url != original_url:
            raise ValueError("canonical_url must be a canonical HTTPS URL")
        return value

    @field_validator("published_at")
    @classmethod
    def require_aware_published_at(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value)

    @field_validator("original_title", "source", "title", "summary", "why_it_matters")
    @classmethod
    def require_nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value

    @field_validator("tracking_signal")
    @classmethod
    def require_nonblank_tracking_signal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tracking_signal must not be blank")
        return value

    @field_validator("tags")
    @classmethod
    def require_safe_tags(cls, value: list[str]) -> list[str]:
        if any(not tag.strip() or len(tag) > 100 for tag in value):
            raise ValueError("tags must contain nonblank values of at most 100 characters")
        if len(value) != len(set(value)):
            raise ValueError("tags must be unique")
        return value

    @model_validator(mode="after")
    def require_canonical_identity(self) -> NewsItem:
        expected_id = hashlib.sha256(str(self.canonical_url).encode("utf-8")).hexdigest()[:16]
        if self.id != expected_id:
            raise ValueError("id must match the canonical_url SHA-256 prefix")
        return self

    @classmethod
    def from_candidate_analysis(
        cls,
        candidate: Candidate,
        analysis: Analysis,
    ) -> NewsItem:
        return cls(
            id=candidate.id,
            canonical_url=candidate.canonical_url,
            original_title=candidate.raw.title,
            source=candidate.raw.source_name,
            published_at=candidate.raw.published_at,
            title=analysis.title,
            category=analysis.category,
            summary=analysis.summary,
            importance=analysis.importance,
            why_it_matters=analysis.why_it_matters,
            tags=analysis.tags,
            is_official=analysis.is_official,
            marketing_risk=analysis.marketing_risk,
            tracking_signal=analysis.tracking_signal,
        )


class SourceRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$", max_length=100)
    source_name: str = Field(min_length=1, max_length=300)
    success: bool
    item_count: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    error_type: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$",
    )

    @field_validator("source_name")
    @classmethod
    def require_nonblank_source_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_name must not be blank")
        return value

    @model_validator(mode="after")
    def require_consistent_error_state(self) -> SourceRun:
        if self.success and self.error_type is not None:
            raise ValueError("successful source runs must not contain an error_type")
        if not self.success and self.error_type is None:
            raise ValueError("failed source runs require an error_type")
        return self

    @classmethod
    def succeeded(
        cls,
        *,
        source_id: str,
        source_name: str,
        item_count: int,
        elapsed_ms: int,
    ) -> SourceRun:
        return cls(
            source_id=source_id,
            source_name=source_name,
            success=True,
            item_count=item_count,
            elapsed_ms=elapsed_ms,
            error_type=None,
        )

    @classmethod
    def failed(
        cls,
        *,
        source_id: str,
        source_name: str,
        elapsed_ms: int,
        error: BaseException,
        item_count: int = 0,
    ) -> SourceRun:
        error_name = type(error).__name__
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", error_name) is None:
            error_name = "Error"
        return cls(
            source_id=source_id,
            source_name=source_name,
            success=False,
            item_count=item_count,
            elapsed_ms=elapsed_ms,
            error_type=error_name,
        )


class DailyReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    date: date
    generated_at: datetime
    model: str = Field(min_length=1, max_length=300)
    degraded: bool
    candidate_count: int = Field(ge=0)
    items: list[NewsItem]
    source_runs: list[SourceRun]

    @field_validator("generated_at")
    @classmethod
    def require_aware_generated_at(cls, value: datetime) -> datetime:
        return _require_aware_datetime(value)

    @field_validator("model")
    @classmethod
    def require_nonblank_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be blank")
        return value

    @model_validator(mode="after")
    def require_consistent_counts_and_sources(self) -> DailyReport:
        if self.candidate_count < len(self.items):
            raise ValueError("candidate_count must be at least the number of report items")
        source_ids = [source_run.source_id for source_run in self.source_runs]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_runs must contain unique source ids")
        return self
