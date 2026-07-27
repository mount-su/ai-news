> [!WARNING]
> **本文的模型配置步骤已废弃，请勿执行。** 当前模型接入方案已由
> `docs/superpowers/plans/2026-07-27-standard-ark-api.md` 取代；配置端点、模型和
> 凭证时必须使用该当前实施计划。

# AI 情报雷达 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建并上线 `mount-su/ai-news`，使 GitHub Actions 每天采集 AI 资讯、调用兼容 Anthropic 协议的 LLM 生成中文分析，并把静态站点部署到 GitHub Pages。

**Architecture:** Python 包负责来源配置、采集、标准化、去重、排序、LLM 分析、JSON/Markdown 持久化和 Jinja 静态构建。生成数据提交到 `main`，`dist/` 只作为 Pages artifact 部署；采集失败或模型失败不会覆盖最后一次成功站点。

**Tech Stack:** Python 3.12、httpx、feedparser、Pydantic 2、PyYAML、Jinja2、Bleach、pytest、ruff、GitHub Actions、GitHub Pages。

---

## File Map

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Python 包、运行依赖、开发依赖、ruff 与 pytest 配置 |
| `.env.example` | 无真实值的模型和站点环境变量模板 |
| `.gitignore` | 忽略虚拟环境、缓存、构建产物和本地密钥 |
| `sources/feeds.yaml` | 首批 RSS、Atom、arXiv 和 GitHub Release 来源 |
| `src/ai_news/models.py` | 来源、候选资讯、分析结果、日报和运行状态模型 |
| `src/ai_news/config.py` | YAML 与环境变量配置加载 |
| `src/ai_news/http.py` | 有界重试、超时和安全响应大小限制 |
| `src/ai_news/collectors/feed.py` | RSS 与 Atom 采集 |
| `src/ai_news/collectors/arxiv.py` | arXiv Atom 查询 |
| `src/ai_news/collectors/github.py` | GitHub Releases API 采集 |
| `src/ai_news/pipeline/normalize.py` | 标题、时间和 URL 规范化 |
| `src/ai_news/pipeline/dedupe.py` | URL、指纹和标题相似度去重 |
| `src/ai_news/pipeline/rank.py` | 确定性预评分与候选数量控制 |
| `src/ai_news/llm/prompt.py` | 不可信来源隔离和结构化提示词 |
| `src/ai_news/llm/anthropic.py` | Anthropic 协议兼容调用、重试和 JSON 修复 |
| `src/ai_news/storage/repository.py` | 原子 JSON、Markdown 和历史索引写入 |
| `src/ai_news/pipeline/run.py` | 每日任务编排与降级门槛 |
| `src/ai_news/site/builder.py` | 首页、分类、归档、日报和搜索索引构建 |
| `src/ai_news/site/templates/` | Jinja 页面模板 |
| `src/ai_news/site/static/` | CSS 与渐进增强 JavaScript |
| `src/ai_news/cli.py` | `generate`、`build` 和 `demo` 命令 |
| `scripts/check_no_secrets.py` | 敏感信息模式扫描 |
| `scripts/validate_site.py` | 静态 HTML、资源和内部链接检查 |
| `tests/fixtures/` | 固定 RSS、Atom、GitHub 与 LLM 样本 |
| `tests/` | 单元、集成、离线端到端和安全测试 |
| `.github/workflows/ci.yml` | 质量门禁 |
| `.github/workflows/daily-news.yml` | 生成、提交、构建和 Pages 部署 |
| `README.md` | 本地运行、Secrets、Variables、工作流和恢复说明 |

### Task 1: Scaffold the Python package and typed configuration

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `src/ai_news/__init__.py`
- Create: `src/ai_news/models.py`
- Create: `src/ai_news/config.py`
- Create: `sources/feeds.yaml`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing configuration tests**

Create `tests/test_config.py`:

```python
from pathlib import Path

import pytest

from ai_news.config import load_source_config, load_settings


def test_load_source_config_requires_known_source_kind(tmp_path: Path) -> None:
    path = tmp_path / "feeds.yaml"
    path.write_text(
        "sources:\n  - id: bad\n    name: Bad\n    kind: webpage\n    url: https://example.com\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_source_config(path)


def test_load_settings_reads_anthropic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-secret")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "test-model")
    monkeypatch.setenv("SITE_BASE_PATH", "/ai-news/")

    settings = load_settings()

    assert settings.llm_base_url == "https://api.example.com"
    assert settings.llm_token.get_secret_value() == "test-secret"
    assert settings.llm_model == "test-model"
    assert settings.site_base_path == "/ai-news/"
```

- [ ] **Step 2: Run the tests and verify the import failure**

Run:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/pytest tests/test_config.py -q
```

Expected: collection fails because `ai_news.config` does not exist.

- [ ] **Step 3: Add package and tool configuration**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=75", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "ai-news-radar"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "bleach>=6.2,<7",
  "feedparser>=6.0,<7",
  "httpx>=0.28,<0.29",
  "jinja2>=3.1,<4",
  "pydantic>=2.11,<3",
  "python-dateutil>=2.9,<3",
  "pyyaml>=6.0,<7",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.4,<9",
  "pytest-cov>=6.2,<7",
  "respx>=0.22,<0.23",
  "ruff>=0.12,<0.13",
]

[project.scripts]
ai-news = "ai_news.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
addopts = "-q --strict-markers"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "RUF"]
```

Create `.gitignore`:

```gitignore
.DS_Store
.env
.venv/
.pytest_cache/
.ruff_cache/
__pycache__/
*.py[cod]
*.egg-info/
coverage.xml
htmlcov/
dist/
```

Create `.env.example`:

```dotenv
ANTHROPIC_BASE_URL=https://api.example.com
ANTHROPIC_AUTH_TOKEN=replace-with-a-repository-secret
ANTHROPIC_DEFAULT_OPUS_MODEL=replace-with-a-model-name
ANTHROPIC_AUTH_SCHEME=bearer
SITE_BASE_PATH=/ai-news/
```

Create `src/ai_news/__init__.py`:

```python
"""AI News Radar."""

__version__ = "0.1.0"
```

- [ ] **Step 4: Implement the typed models and loaders**

Implement these exact public models in `src/ai_news/models.py`:

```python
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, SecretStr, model_validator


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
    def validate_locator(self) -> SourceSpec:
        if self.kind in {"feed", "arxiv"} and self.url is None:
            raise ValueError(f"{self.kind} source requires url")
        if self.kind == "github" and not self.repo:
            raise ValueError("github source requires repo")
        return self


class SourceConfig(BaseModel):
    sources: list[SourceSpec]


class Settings(BaseModel):
    llm_base_url: str
    llm_token: SecretStr
    llm_model: str
    llm_auth_scheme: Literal["bearer", "x-api-key"] = "bearer"
    site_base_path: str = "/ai-news/"

    @model_validator(mode="after")
    def validate_base_path(self) -> Settings:
        if not self.site_base_path.startswith("/") or not self.site_base_path.endswith("/"):
            raise ValueError("SITE_BASE_PATH must start and end with /")
        return self


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
```

Implement `src/ai_news/config.py` with `load_source_config(path: Path) -> SourceConfig` using `yaml.safe_load`, and `load_settings() -> Settings` using only the five environment variables in `.env.example`. Reject an empty source list and missing model variables with Pydantic validation errors.

- [ ] **Step 5: Add the initial source manifest**

Create `sources/feeds.yaml` with the verified RSS endpoints and these GitHub repositories:

```yaml
sources:
  - id: openai-news
    name: OpenAI
    kind: feed
    url: https://openai.com/news/rss.xml
    category: 大模型
    weight: 10
    official: true
  - id: google-ai
    name: Google AI
    kind: feed
    url: https://blog.google/technology/ai/rss/
    category: 大模型
    weight: 9
    official: true
  - id: deepmind
    name: Google DeepMind
    kind: feed
    url: https://deepmind.google/blog/rss.xml
    category: 论文研究
    weight: 9
    official: true
  - id: huggingface-blog
    name: Hugging Face
    kind: feed
    url: https://huggingface.co/blog/feed.xml
    category: 开源项目
    weight: 8
    official: true
  - id: arxiv-ai
    name: arXiv AI
    kind: arxiv
    url: https://export.arxiv.org/api/query?search_query=cat%3Acs.AI%20OR%20cat%3Acs.CL%20OR%20cat%3Acs.LG&sortBy=submittedDate&sortOrder=descending&start=0&max_results=40
    category: 论文研究
    weight: 7
    official: true
  - id: openai-codex
    name: OpenAI Codex
    kind: github
    repo: openai/codex
    category: Coding Agent
    weight: 10
    official: true
  - id: claude-code
    name: Claude Code
    kind: github
    repo: anthropics/claude-code
    category: Coding Agent
    weight: 10
    official: true
  - id: gemini-cli
    name: Gemini CLI
    kind: github
    repo: google-gemini/gemini-cli
    category: Coding Agent
    weight: 9
    official: true
  - id: langchain
    name: LangChain
    kind: github
    repo: langchain-ai/langchain
    category: Agent
    weight: 8
    official: true
  - id: crewai
    name: CrewAI
    kind: github
    repo: crewAIInc/crewAI
    category: Agent
    weight: 8
    official: true
  - id: llama-index
    name: LlamaIndex
    kind: github
    repo: run-llama/llama_index
    category: Agent
    weight: 7
    official: true
  - id: transformers
    name: Transformers
    kind: github
    repo: huggingface/transformers
    category: 开源项目
    weight: 8
    official: true
  - id: vllm
    name: vLLM
    kind: github
    repo: vllm-project/vllm
    category: 开源项目
    weight: 8
    official: true
  - id: ollama
    name: Ollama
    kind: github
    repo: ollama/ollama
    category: AI 工具
    weight: 8
    official: true
  - id: cline
    name: Cline
    kind: github
    repo: cline/cline
    category: Coding Agent
    weight: 8
    official: true
```

- [ ] **Step 6: Run focused and quality tests**

Run:

```bash
.venv/bin/pytest tests/test_config.py -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit the foundation**

```bash
git add pyproject.toml .gitignore .env.example sources src/ai_news tests/test_config.py
git commit -m "build: scaffold AI news pipeline"
```

### Task 2: Build bounded HTTP and RSS/Atom collectors

**Files:**
- Create: `src/ai_news/http.py`
- Create: `src/ai_news/collectors/__init__.py`
- Create: `src/ai_news/collectors/feed.py`
- Create: `tests/fixtures/openai-feed.xml`
- Test: `tests/test_http.py`
- Test: `tests/test_feed_collector.py`

- [ ] **Step 1: Write failing retry and feed parsing tests**

`tests/test_http.py` must prove that `429` is retried and `401` is not retried by injecting a no-op sleep function. `tests/test_feed_collector.py` must load a two-entry RSS fixture, reject an entry without a valid HTTPS URL, normalize timezone-aware timestamps, and preserve source metadata.

Use this fixture shape:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>OpenAI News</title>
    <item>
      <title>Model launch</title>
      <link>https://openai.com/index/model-launch/</link>
      <pubDate>Sat, 25 Jul 2026 10:00:00 GMT</pubDate>
      <description><![CDATA[<p>A model update.</p>]]></description>
    </item>
    <item>
      <title>Missing URL</title>
      <pubDate>Sat, 25 Jul 2026 11:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
```

- [ ] **Step 2: Verify the tests fail**

Run:

```bash
.venv/bin/pytest tests/test_http.py tests/test_feed_collector.py -q
```

Expected: imports fail because the HTTP and feed modules are absent.

- [ ] **Step 3: Implement bounded HTTP behavior**

`src/ai_news/http.py` must expose:

```python
async def get_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    attempts: int = 3,
    max_bytes: int = 2_000_000,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bytes:
```

Behavior:

- Allow only `https`.
- Use a 20-second request timeout on the shared client.
- Retry `429` and `500`–`599` with delays 1, 2, and 4 seconds.
- Raise immediately for other `400` responses.
- Read the response body before checking `max_bytes`.
- Raise `ValueError("response exceeds byte limit")` when the limit is exceeded.
- Send `User-Agent: mount-su-ai-news/0.1`.

- [ ] **Step 4: Implement the feed collector**

`src/ai_news/collectors/feed.py` must expose:

```python
def parse_feed(payload: bytes, source: SourceSpec) -> list[RawItem]:
```

Use `feedparser.loads`, `dateutil.parser.parse`, `bleach.clean(entry_description, tags=[], strip=True)`, and `HttpUrl` validation through `RawItem`. Skip malformed entries individually. Never fail the whole feed because one item is invalid.

- [ ] **Step 5: Run collector tests**

Run:

```bash
.venv/bin/pytest tests/test_http.py tests/test_feed_collector.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit collectors**

```bash
git add src/ai_news/http.py src/ai_news/collectors tests/fixtures/openai-feed.xml tests/test_http.py tests/test_feed_collector.py
git commit -m "feat: collect bounded RSS and Atom feeds"
```

### Task 3: Add arXiv and GitHub Release collectors

**Files:**
- Create: `src/ai_news/collectors/arxiv.py`
- Create: `src/ai_news/collectors/github.py`
- Create: `tests/fixtures/arxiv-feed.xml`
- Create: `tests/fixtures/github-releases.json`
- Test: `tests/test_arxiv_collector.py`
- Test: `tests/test_github_collector.py`

- [ ] **Step 1: Write failing parser tests**

The arXiv fixture must contain one paper with `id`, `title`, `summary`, `published`, and authors. The GitHub fixture must contain one published release, one prerelease, and one draft. Assert that only the published non-draft release becomes a `RawItem`, its URL is `html_url`, and its timestamp is `published_at`.

- [ ] **Step 2: Verify failure**

Run:

```bash
.venv/bin/pytest tests/test_arxiv_collector.py tests/test_github_collector.py -q
```

Expected: module import failures.

- [ ] **Step 3: Implement arXiv parsing**

`parse_arxiv(payload: bytes, source: SourceSpec) -> list[RawItem]` must:

- Parse Atom with `feedparser`.
- Collapse whitespace in titles and summaries.
- Use `entry.link` as the canonical paper URL.
- Use `entry.published` as UTC-aware `published_at`.
- Preserve the configured `论文研究` category hint.

- [ ] **Step 4: Implement GitHub Release parsing**

`parse_github_releases(payload: bytes, source: SourceSpec) -> list[RawItem]` must:

- Parse a JSON list.
- Ignore `draft: true` and `prerelease: true`.
- Use `name` or `tag_name` for the title.
- Strip release body HTML and cap the excerpt at 4,000 characters.
- Use `published_at` and `html_url`.

Add:

```python
def releases_url(source: SourceSpec) -> str:
    return f"https://api.github.com/repos/{source.repo}/releases?per_page=10"
```

The live collector passes `Authorization: Bearer ${GITHUB_TOKEN}` when present.

- [ ] **Step 5: Run and commit**

```bash
.venv/bin/pytest tests/test_arxiv_collector.py tests/test_github_collector.py -q
.venv/bin/ruff check .
git add src/ai_news/collectors tests/fixtures tests/test_arxiv_collector.py tests/test_github_collector.py
git commit -m "feat: collect arXiv and GitHub releases"
```

### Task 4: Normalize, deduplicate, and pre-rank candidates

**Files:**
- Modify: `src/ai_news/models.py`
- Create: `src/ai_news/pipeline/__init__.py`
- Create: `src/ai_news/pipeline/normalize.py`
- Create: `src/ai_news/pipeline/dedupe.py`
- Create: `src/ai_news/pipeline/rank.py`
- Test: `tests/test_normalize.py`
- Test: `tests/test_dedupe.py`
- Test: `tests/test_rank.py`

- [ ] **Step 1: Write failing normalization tests**

Cover:

```python
def test_canonical_url_removes_tracking_and_fragment() -> None:
    assert canonical_url(
        "https://Example.com/post/?utm_source=x&ref=home&id=7#section"
    ) == "https://example.com/post?id=7"
```

Also test repeated whitespace, Unicode title case preservation, and UTC conversion.

- [ ] **Step 2: Write failing dedupe tests**

Assert:

- Identical canonical URLs collapse.
- Same host and normalized title collapse.
- Titles with `SequenceMatcher` ratio at least `0.92` collapse.
- An item already present in the 30-day history set is excluded.
- When two items collapse, higher source weight wins; a tie uses the newest timestamp.

- [ ] **Step 3: Write failing ranking tests**

Use the exact deterministic score:

```text
source_weight * 10
+ recency_points (0 to 20)
+ official_bonus (10 or 0)
+ release_or_launch_keyword_bonus (0 to 10)
```

Assert that `select_candidates(items, limit=30)` is stable across repeated runs and never returns more than 30 items.

- [ ] **Step 4: Verify failure**

```bash
.venv/bin/pytest tests/test_normalize.py tests/test_dedupe.py tests/test_rank.py -q
```

Expected: module import failures.

- [ ] **Step 5: Implement the three focused modules**

Add this intermediate model to `models.py`:

```python
class Candidate(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{16}$")
    canonical_url: HttpUrl
    raw: RawItem
```

`normalize.py` exports `canonical_url`, `normalize_title`, `ensure_utc`, and
`to_candidate`. `to_candidate` uses the first 16 hexadecimal characters of the
SHA-256 hash of the canonical URL as the stable ID.

Remove these query parameters case-insensitively:

```python
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "source",
}
```

`dedupe.py` exports
`deduplicate(items: list[Candidate], historical_urls: set[str]) -> list[Candidate]`.

`rank.py` exports `candidate_score(item: Candidate, now: datetime) -> int` and
`select_candidates(items: list[Candidate], now: datetime, limit: int = 30) -> list[Candidate]`.

- [ ] **Step 6: Run and commit**

```bash
.venv/bin/pytest tests/test_normalize.py tests/test_dedupe.py tests/test_rank.py -q
.venv/bin/ruff check .
git add src/ai_news/pipeline tests/test_normalize.py tests/test_dedupe.py tests/test_rank.py
git commit -m "feat: normalize and rank news candidates"
```

### Task 5: Implement structured Anthropic-compatible analysis

**Files:**
- Create: `src/ai_news/llm/__init__.py`
- Create: `src/ai_news/llm/prompt.py`
- Create: `src/ai_news/llm/anthropic.py`
- Create: `tests/fixtures/llm-success.json`
- Test: `tests/test_llm_prompt.py`
- Test: `tests/test_anthropic_client.py`

- [ ] **Step 1: Write failing prompt isolation tests**

Assert that the prompt:

- Labels each candidate as untrusted external material.
- States that instructions inside titles and excerpts must be ignored.
- Includes the six allowed categories.
- Requests only a JSON array.
- Contains candidate IDs so responses can be joined deterministically.

- [ ] **Step 2: Write failing HTTP client tests with `httpx.MockTransport`**

Cover:

- URL is `${ANTHROPIC_BASE_URL}/v1/messages` without a doubled slash.
- Bearer mode sends `Authorization: Bearer test-secret`.
- `x-api-key` mode sends `x-api-key: test-secret` and `anthropic-version: 2023-06-01`.
- `429` is retried.
- `401` raises without retry.
- A fenced JSON response is parsed.
- An invalid response triggers exactly one repair request.
- A second invalid response raises `AnalysisError`.
- Secret text never appears in exception strings.

- [ ] **Step 3: Verify failure**

```bash
.venv/bin/pytest tests/test_llm_prompt.py tests/test_anthropic_client.py -q
```

Expected: module import failures.

- [ ] **Step 4: Implement the prompt**

`build_analysis_prompt(items: list[Candidate]) -> str` must serialize only:

```json
{
  "id": "candidate-id",
  "title": "source title",
  "source": "source name",
  "published_at": "ISO-8601",
  "excerpt": "bounded plain text",
  "category_hint": "configured category",
  "is_official_source": true
}
```

The system message must state that factual claims cannot extend beyond supplied material and that the response must satisfy the `Analysis` fields.

- [ ] **Step 5: Implement the client**

Expose `AnalysisError`, derived from `RuntimeError`, and
`AnthropicAnalyzer.analyze(items: list[Candidate]) -> dict[str, Analysis]`.

Implementation requirements:

- Batch at most 10 items.
- Set `temperature` to `0.2` and `max_tokens` to `6000`.
- Parse the first text content block.
- Remove one outer Markdown code fence before `json.loads`.
- Validate every row with Pydantic `Analysis`.
- Join by candidate ID and reject unknown or duplicate IDs.
- Retry network, `429`, and `5xx` up to 3 attempts.
- Execute one JSON repair request only after parsing or validation failure.
- Redact the token from all raised messages.

- [ ] **Step 6: Run and commit**

```bash
.venv/bin/pytest tests/test_llm_prompt.py tests/test_anthropic_client.py -q
.venv/bin/ruff check .
git add src/ai_news/llm tests/fixtures/llm-success.json tests/test_llm_prompt.py tests/test_anthropic_client.py
git commit -m "feat: analyze news with structured LLM output"
```

### Task 6: Add atomic repository storage and Markdown reports

**Files:**
- Modify: `src/ai_news/models.py`
- Create: `src/ai_news/storage/__init__.py`
- Create: `src/ai_news/storage/repository.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Add failing storage tests**

The test must use `tmp_path` and prove:

- `data/2026/07/2026-07-26.json` is valid UTF-8 JSON.
- `content/2026-07-26.md` contains the top-three section and source links.
- `data/index.json` contains the date, item count, and relative data path.
- Re-running the same date replaces one record rather than appending a duplicate.
- A simulated write failure leaves existing files unchanged.
- Historical canonical URLs are loaded only from the requested 30-day window.

- [ ] **Step 2: Verify failure**

```bash
.venv/bin/pytest tests/test_storage.py -q
```

Expected: import failure.

- [ ] **Step 3: Add report models**

Extend `models.py` with `NewsItem`, `SourceRun`, and `DailyReport`. `DailyReport` fields must be:

```python
schema_version: Literal["1.0"] = "1.0"
date: date
generated_at: datetime
model: str
degraded: bool
candidate_count: int
items: list[NewsItem]
source_runs: list[SourceRun]
```

`NewsItem` combines the flat public fields from `Candidate.raw`, `Candidate.id`,
`Candidate.canonical_url`, and `Analysis`; it must not serialize the nested
`raw` or `analysis` objects.

- [ ] **Step 4: Implement atomic storage**

Use a temporary sibling file, `flush`, `os.fsync`, and `Path.replace`. Do not use an in-place rewrite. Render Markdown from a deterministic Python function so tests do not depend on Jinja.

Export:

```python
def save_report(root: Path, report: DailyReport) -> None:
def load_history_urls(root: Path, run_date: date, days: int = 30) -> set[str]:
def load_reports(root: Path) -> list[DailyReport]:
```

- [ ] **Step 5: Run and commit**

```bash
.venv/bin/pytest tests/test_storage.py -q
.venv/bin/ruff check .
git add src/ai_news/models.py src/ai_news/storage tests/test_storage.py
git commit -m "feat: persist daily reports atomically"
```

### Task 7: Orchestrate the daily pipeline and CLI

**Files:**
- Create: `src/ai_news/pipeline/run.py`
- Create: `src/ai_news/cli.py`
- Create: `src/ai_news/__main__.py`
- Create: `tests/test_pipeline_run.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing orchestration tests**

Use fake collectors and a fake analyzer. Prove:

- Source failures are recorded and do not stop healthy sources.
- Items outside the 36-hour window are discarded.
- Fewer than 5 analyzed items raises `PublicationThresholdError`.
- At least 5 items produces a report.
- A partial source failure sets `degraded=True`.
- The top three items are the highest `importance`, then newest timestamp.
- No files are written before the completed report passes validation.

- [ ] **Step 2: Write failing CLI tests**

Assert:

```text
ai-news generate --date 2026-07-26 --root <tmp>
ai-news build --root <tmp> --output <tmp>/dist
ai-news demo --root <tmp> --output <tmp>/dist
```

`--date` must reject invalid ISO dates. `demo` must not require model environment variables.

- [ ] **Step 3: Verify failure**

```bash
.venv/bin/pytest tests/test_pipeline_run.py tests/test_cli.py -q
```

Expected: import failures.

- [ ] **Step 4: Implement the orchestrator**

`run_daily` must accept injected collector and analyzer dependencies, which lets tests remain offline. Live wiring:

- Creates one `httpx.AsyncClient(timeout=20)`.
- Runs enabled sources with a concurrency limit of 6.
- Records elapsed time and normalized error type per source.
- Loads 30-day history.
- Deduplicates and preselects at most 30 candidates.
- Calls the analyzer in batches.
- Requires at least 5 valid final items.
- Keeps at most 20 and derives top-three order.
- Saves only after `DailyReport` validation succeeds.

- [ ] **Step 5: Implement CLI exit contracts**

Exit codes:

```text
0 success
2 configuration error
3 publication threshold not met
4 collection or LLM failure
5 site build failure
```

Use `argparse`; never print environment variable values.

- [ ] **Step 6: Run and commit**

```bash
.venv/bin/pytest tests/test_pipeline_run.py tests/test_cli.py -q
.venv/bin/ruff check .
git add src/ai_news/pipeline/run.py src/ai_news/cli.py src/ai_news/__main__.py tests/test_pipeline_run.py tests/test_cli.py
git commit -m "feat: orchestrate daily news generation"
```

### Task 8: Build the static site and progressive interactions

**Files:**
- Create: `src/ai_news/site/__init__.py`
- Create: `src/ai_news/site/builder.py`
- Create: `src/ai_news/site/templates/base.html`
- Create: `src/ai_news/site/templates/index.html`
- Create: `src/ai_news/site/templates/day.html`
- Create: `src/ai_news/site/templates/archive.html`
- Create: `src/ai_news/site/templates/category.html`
- Create: `src/ai_news/site/templates/_card.html`
- Create: `src/ai_news/site/static/styles.css`
- Create: `src/ai_news/site/static/app.js`
- Test: `tests/test_site_builder.py`
- Test: `tests/test_site_accessibility.py`

- [ ] **Step 1: Write failing site structure tests**

Build from a two-day fixture report set and assert:

- `dist/index.html`
- `dist/archive/index.html`
- `dist/days/2026-07-26/index.html`
- `dist/categories/coding-agent/index.html`
- `dist/assets/styles.css`
- `dist/assets/app.js`
- `dist/search.json`

Assert every asset path begins with `/ai-news/`, all internal targets exist, and external links use `rel="noopener noreferrer"`.

- [ ] **Step 2: Write failing accessibility tests**

Using `html.parser` or a focused parser helper, assert:

- One `main` landmark.
- A skip link targets `#main-content`.
- Search has a visible label.
- Filter buttons have `aria-pressed`.
- Expandable analysis uses native `details` and `summary`.
- Importance has visible text in addition to color.
- HTML escaping prevents `<script>` from source titles becoming an element.

- [ ] **Step 3: Verify failure**

```bash
.venv/bin/pytest tests/test_site_builder.py tests/test_site_accessibility.py -q
```

Expected: import failure.

- [ ] **Step 4: Implement the builder**

`build_site(root: Path, output: Path, base_path: str) -> None` must:

- Delete only the explicit `output` directory after verifying it is neither `/`, the repository root, nor the user home.
- Load reports from storage.
- Render the latest report as the homepage.
- Render every date and every category.
- Write `search.json` with only public card fields.
- Copy static assets with `shutil.copytree`.
- Use one `asset_url` and one `page_url` helper for the project subpath.

- [ ] **Step 5: Implement the editorial UI**

Use CSS custom properties:

```css
:root {
  --ink: #07111f;
  --panel: #0c1b2d;
  --panel-strong: #10253d;
  --line: #23415d;
  --text: #edf8ff;
  --muted: #9ab3c7;
  --signal: #52e3ff;
  --amber: #ffbd59;
  --danger: #ff7d8a;
  --max-width: 1240px;
}
```

Required layout:

- Compact masthead with current successful generation time.
- Four-metric status strip.
- Top-three editorial grid.
- Tracking-signal rail.
- Sticky search and category controls.
- Responsive two-column feed at 900 pixels and single column below 720 pixels.
- Visible `:focus-visible` outline.
- `@media (prefers-reduced-motion: reduce)` disables transitions.

`app.js` must:

- Read search and category controls.
- Filter existing cards by `data-search` and `data-category`.
- Update `aria-pressed`.
- Update a visible result count.
- Leave content readable when JavaScript is disabled.

- [ ] **Step 6: Run and commit**

```bash
.venv/bin/pytest tests/test_site_builder.py tests/test_site_accessibility.py -q
.venv/bin/ruff check .
git add src/ai_news/site tests/test_site_builder.py tests/test_site_accessibility.py
git commit -m "feat: build the AI intelligence static site"
```

### Task 9: Add deterministic offline end-to-end coverage and safety checks

**Files:**
- Create: `tests/fixtures/demo-report-input.json`
- Create: `tests/test_e2e_demo.py`
- Create: `scripts/check_no_secrets.py`
- Create: `scripts/validate_site.py`
- Test: `tests/test_secret_scan.py`
- Test: `tests/test_validate_site.py`

- [ ] **Step 1: Write failing end-to-end test**

Run the demo CLI in a temporary root. Assert it creates at least 5 analyzed items, a valid daily JSON file, Markdown, homepage, category page, archive, search index, CSS, and JavaScript without network access.

- [ ] **Step 2: Write failing secret scanner tests**

The scanner must fail on:

```text
ANTHROPIC_AUTH_TOKEN=real-value
Authorization: Bearer real-value
```

It must pass `.env.example` because its values are recognized non-secret examples.

- [ ] **Step 3: Write failing site validator tests**

The validator must report:

- Missing internal link target.
- Missing asset.
- Duplicate HTML ID.
- Absent `main`.
- A page whose asset URL escapes `/ai-news/`.

- [ ] **Step 4: Implement the scripts**

`check_no_secrets.py` scans tracked text files returned by `git ls-files`. It exits 1 with paths and line numbers but never prints the matched secret value.

`validate_site.py dist --base-path /ai-news/` parses every HTML file, builds the set of generated paths, and validates internal anchors and assets.

- [ ] **Step 5: Run the complete local gate**

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest --cov=ai_news --cov-report=term-missing
.venv/bin/python scripts/check_no_secrets.py
.venv/bin/python -m ai_news demo --root .demo --output .demo/dist
.venv/bin/python scripts/validate_site.py .demo/dist --base-path /ai-news/
```

Expected: all commands exit 0 and statement coverage is at least 85%.

- [ ] **Step 6: Commit offline verification**

```bash
git add tests/fixtures/demo-report-input.json tests/test_e2e_demo.py tests/test_secret_scan.py tests/test_validate_site.py scripts
git commit -m "test: verify the offline publishing pipeline"
```

### Task 10: Add CI and Pages workflows

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/daily-news.yml`
- Create: `tests/test_workflows.py`

- [ ] **Step 1: Write failing workflow contract tests**

Parse the YAML with `yaml.safe_load` after configuring a loader that preserves the `on` key as text. Assert:

- CI runs on pull requests and pushes to `main`.
- Daily schedule uses cron `17 8 * * *` and timezone `Asia/Shanghai`.
- Manual dispatch includes optional ISO `date`.
- Generate job has `contents: write`.
- Build job has `contents: read`.
- Deploy job has `pages: write` and `id-token: write`.
- `concurrency.group` is `ai-news-daily`.
- Pages upload path is `dist`.
- Deploy needs build.
- The generated data commit happens before the build job.

- [ ] **Step 2: Verify failure**

```bash
.venv/bin/pytest tests/test_workflows.py -q
```

Expected: workflow files are missing.

- [ ] **Step 3: Implement CI**

`ci.yml` must:

- Checkout with `actions/checkout@v6`.
- Set up Python 3.12 with `actions/setup-python@v6` and pip caching.
- Install `.[dev]`.
- Run ruff check, ruff format check, pytest with coverage, secret scan, demo build, and site validation.
- Run `rhysd/actionlint:1.7.7` through Docker against both workflow files.
- Use only `contents: read`.

- [ ] **Step 4: Implement daily generation and Pages deployment**

`daily-news.yml` must have three jobs:

1. `generate`: runs only for schedule or manual dispatch, checks out `main`, installs Python, runs `ai-news generate`, scans secrets, commits `data/` and `content/`, and pushes with the default token.
2. `build`: runs after `generate` succeeds or is skipped, checks out current `main`, builds `dist`, validates it, and uploads the artifact.
3. `deploy`: needs `build`, uses environment `github-pages`, and runs `actions/deploy-pages@v4`.

Use:

```yaml
on:
  push:
    branches: [main]
  schedule:
    - cron: "17 8 * * *"
      timezone: "Asia/Shanghai"
  workflow_dispatch:
    inputs:
      date:
        description: "Optional ISO date to regenerate"
        required: false
        type: string
```

The generate step maps:

```yaml
env:
  ANTHROPIC_BASE_URL: ${{ vars.ANTHROPIC_BASE_URL }}
  ANTHROPIC_AUTH_TOKEN: ${{ secrets.ANTHROPIC_AUTH_TOKEN }}
  ANTHROPIC_DEFAULT_OPUS_MODEL: ${{ vars.ANTHROPIC_DEFAULT_OPUS_MODEL }}
  ANTHROPIC_AUTH_SCHEME: ${{ vars.ANTHROPIC_AUTH_SCHEME || 'bearer' }}
  SITE_BASE_PATH: /ai-news/
  GITHUB_TOKEN: ${{ github.token }}
```

- [ ] **Step 5: Run workflow and full tests**

```bash
.venv/bin/pytest tests/test_workflows.py -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest --cov=ai_news --cov-fail-under=85
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.7
```

Expected: all checks pass.

- [ ] **Step 6: Commit automation**

```bash
git add .github/workflows tests/test_workflows.py
git commit -m "ci: automate daily Pages publishing"
```

### Task 11: Document operation and recovery

**Files:**
- Create: `README.md`
- Modify: `docs/superpowers/specs/2026-07-26-ai-news-github-pages-design.md`
- Test: `tests/test_readme_contract.py`

- [ ] **Step 1: Write the failing documentation contract**

Assert README contains:

- Local setup and Python 3.12.
- Offline demo command.
- Live generation command.
- The three GitHub Variables and one Secret.
- The 08:17 Asia/Shanghai schedule.
- Manual date backfill instructions.
- GitHub Pages URL.
- Failure behavior and recovery.
- Coding Plan credential warning.
- Data and copyright boundary.

- [ ] **Step 2: Verify failure**

```bash
.venv/bin/pytest tests/test_readme_contract.py -q
```

Expected: README is missing.

- [ ] **Step 3: Write the implementation-focused README**

Use these top-level sections:

```text
# AI 情报雷达
## 它如何工作
## 页面与数据结构
## 本地快速开始
## 离线演示
## 使用真实模型生成
## GitHub Secrets 与 Variables
## GitHub Actions
## 数据源维护
## 故障与补跑
## 安全与版权
## 项目结构
```

Do not include screenshots. Link the design and implementation plan.

- [ ] **Step 4: Synchronize the design with the implemented variable names**

If implementation changed any exact variable or path, update the design in the same commit. Do not weaken approved behavior.

- [ ] **Step 5: Run and commit**

```bash
.venv/bin/pytest tests/test_readme_contract.py -q
.venv/bin/python scripts/check_no_secrets.py
git add README.md docs/superpowers/specs/2026-07-26-ai-news-github-pages-design.md tests/test_readme_contract.py
git commit -m "docs: explain AI news operations"
```

### Task 12: Perform local visual and behavioral verification

**Files:**
- Modify only files whose behavior fails verification.

- [ ] **Step 1: Run the complete gate from a clean environment**

```bash
python3 -m venv .verify-venv
.verify-venv/bin/python -m pip install --upgrade pip
.verify-venv/bin/python -m pip install -e ".[dev]"
.verify-venv/bin/ruff check .
.verify-venv/bin/ruff format --check .
.verify-venv/bin/pytest --cov=ai_news --cov-fail-under=85
.verify-venv/bin/python scripts/check_no_secrets.py
.verify-venv/bin/python -m ai_news demo --root .demo --output .demo/dist
.verify-venv/bin/python scripts/validate_site.py .demo/dist --base-path /ai-news/
```

Expected: every command exits 0.

- [ ] **Step 2: Serve the generated site**

```bash
.verify-venv/bin/python -m http.server 4173 --directory .demo/dist
```

Expected: `http://127.0.0.1:4173/` returns 200.

- [ ] **Step 3: Inspect desktop and mobile behavior in the browser**

Verify:

- No console errors.
- No failed resources.
- Top-three cards render.
- Search reduces the visible count.
- Category buttons update `aria-pressed`.
- `details` analysis expands.
- Archive and category links resolve.
- 390-pixel viewport has no horizontal overflow.
- Keyboard focus is visible.
- Reduced-motion emulation removes transitions.

- [ ] **Step 4: Fix any failures with a failing regression test first**

For every defect, add a focused test, run it to see the failure, make the smallest fix, and rerun the full gate.

- [ ] **Step 5: Commit verified UI fixes**

```bash
git add src tests scripts README.md
git commit -m "fix: polish verified site behavior"
```

Skip this commit when verification requires no code changes.

### Task 13: Create the GitHub repository and publish the branch

**Files:**
- No new files expected.

- [ ] **Step 1: Reconfirm local scope**

```bash
git status --short --branch
git log --oneline --decorate --max-count=20
.verify-venv/bin/python scripts/check_no_secrets.py
```

Expected: clean `main`, intentional commits only, secret scan exits 0.

- [ ] **Step 2: Create the public repository**

```bash
gh repo create mount-su/ai-news \
  --public \
  --description "每日自动生成的中文 AI 情报雷达" \
  --source . \
  --remote origin \
  --push
```

Expected: `origin` points to `https://github.com/mount-su/ai-news.git` and remote `main` matches local `HEAD`.

- [ ] **Step 3: Configure repository Variables**

```bash
gh variable set ANTHROPIC_BASE_URL --repo mount-su/ai-news --body "https://ark.cn-beijing.volces.com/api/coding"
gh variable set ANTHROPIC_DEFAULT_OPUS_MODEL --repo mount-su/ai-news --body "glm-5.2"
gh variable set ANTHROPIC_AUTH_SCHEME --repo mount-su/ai-news --body "bearer"
```

Expected: `gh variable list --repo mount-su/ai-news` shows all three names.

- [ ] **Step 4: Configure the model Secret without command-line exposure**

Read the token from a protected local input mechanism and pipe it to:

```bash
gh secret set ANTHROPIC_AUTH_TOKEN --repo mount-su/ai-news
```

Do not place the token in shell history, plan files, logs, environment dumps, or command arguments. Confirm only the secret name:

```bash
gh secret list --repo mount-su/ai-news
```

- [ ] **Step 5: Enable Pages for workflow deployment**

```bash
gh api --method POST repos/mount-su/ai-news/pages -f build_type=workflow
```

If the endpoint returns an existing-site conflict, verify instead:

```bash
gh api repos/mount-su/ai-news/pages --jq '{status,build_type,html_url}'
```

Expected: `build_type` is `workflow`.

- [ ] **Step 6: Verify remote parity**

```bash
git fetch origin
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
gh repo view mount-su/ai-news --json visibility,defaultBranchRef,url
```

Expected: public visibility, default branch `main`, matching SHAs.

### Task 14: Run the real pipeline and prove production deployment

**Files:**
- Modify code only when a verified production defect requires a regression test and fix.

- [ ] **Step 1: Confirm credential authorization**

Before the live run, confirm that the configured token is permitted for unattended, non-coding news summarization. If it is restricted to supported coding tools, replace it with a standard model API credential; do not bypass provider restrictions.

- [ ] **Step 2: Dispatch the workflow**

```bash
gh workflow run daily-news.yml --repo mount-su/ai-news -f date="$(date +%F)"
```

Capture the run ID:

```bash
gh run list --repo mount-su/ai-news --workflow daily-news.yml --limit 1
```

- [ ] **Step 3: Watch the real run to completion**

```bash
gh run watch RUN_ID --repo mount-su/ai-news --exit-status
```

Expected: generate, build, and deploy jobs all succeed.

- [ ] **Step 4: Inspect logs without printing secrets**

```bash
gh run view RUN_ID --repo mount-su/ai-news --log
```

Verify:

- At least 5 final items.
- At least one live official RSS source succeeds.
- GitHub Releases and arXiv are represented when the time window contains eligible items.
- LLM model name is present but token is masked and absent.
- A data/content commit is pushed before deployment.

- [ ] **Step 5: Verify the generated commit**

```bash
git fetch origin
git log --oneline --decorate origin/main -5
git diff --name-only HEAD..origin/main
```

Expected: generated changes are limited to `data/` and `content/`. Fast-forward local `main` with:

```bash
git pull --ff-only origin main
```

- [ ] **Step 6: Verify Pages and public behavior**

```bash
curl -I --fail --silent --show-error https://mount-su.github.io/ai-news/
curl --fail --silent --show-error https://mount-su.github.io/ai-news/ | rg "AI 情报雷达|今日最重要"
```

Expected: HTTP 200 and both texts appear.

Open the production URL in a browser and repeat desktop, 390-pixel mobile, search, category, archive, details, console, and resource checks against production.

- [ ] **Step 7: Verify schedule and repository security**

```bash
gh api repos/mount-su/ai-news/contents/.github/workflows/daily-news.yml --jq .content | base64 --decode | rg '17 8|Asia/Shanghai'
gh secret list --repo mount-su/ai-news
gh variable list --repo mount-su/ai-news
gh api repos/mount-su/ai-news/pages --jq '{status,build_type,html_url}'
```

Expected: schedule and timezone are present, the secret name exists, variables exist, and Pages uses workflow deployment.

- [ ] **Step 8: Record final evidence**

Report:

- Local test and coverage result.
- CI and daily workflow run URLs.
- Repository URL and final SHA.
- Pages URL and HTTP status.
- Generated date and number of items.
- Source failures or degraded state.
- Confirmation that no secret appeared in tracked files or logs.
