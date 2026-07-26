from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator, model_validator


class Category(StrEnum):
    MODEL = "大模型"
    AGENT = "Agent"
    CODING_AGENT = "Coding Agent"
    TOOL = "AI 工具"
    OPEN_SOURCE = "开源项目"
    RESEARCH = "论文研究"


class SourceSpec(BaseModel):
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
        if self.kind == "github" and self.repo is None:
            raise ValueError("github sources require a repo")
        return self


class SourceConfig(BaseModel):
    sources: list[SourceSpec] = Field(min_length=1)


class Settings(BaseModel):
    llm_base_url: str
    llm_token: SecretStr
    llm_model: str
    llm_auth_scheme: Literal["bearer", "x-api-key"] = "bearer"
    site_base_path: str = "/ai-news/"

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
