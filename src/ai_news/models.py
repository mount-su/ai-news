from __future__ import annotations

import re
from datetime import datetime
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
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[0-9a-f]{16}$")
    canonical_url: HttpUrl
    raw: RawItem


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
