# Official Model Vendor and Reddit RSS Sources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有日报栏目、36 小时时间窗、编辑口径和 LLM 配置的前提下，新增 10 个大模型厂商官方来源和一个无需密钥的 Reddit RSS 线索源，并在 GitHub Actions 与正式 Pages 站点完成验证。

**Architecture:** 保留现有 `SourceSpec -> RawItem -> 去重/过滤 -> LLM -> 静态站点` 主链路。新增受允许列表约束的 `official_page` 适配器注册表和独立 `reddit_rss` 采集器；页面解析器只解析固定官方结构，Reddit 只解析两条合并 Atom，并把符合主题、域名和发布日期要求的外部原文转换为 `RawItem`。所有请求复用现有受限 HTTP 客户端，单源和单篇失败在采集边界内隔离。

**Tech Stack:** Python 3.12、Pydantic 2、HTTPX、feedparser、Beautiful Soup 4、python-dateutil、PyYAML、pytest/respx、Ruff、GitHub Actions、Jinja/GitHub Pages。

---

## 执行规则

- 全程在 `/Users/suhuashan/Documents/AI资讯/.worktrees/add-official-model-blogs` 和分支 `codex/add-official-model-blogs` 工作，不改主检出目录。
- 每个任务先写失败测试，再做最小实现，再运行该任务测试和 `git diff --check`，通过后提交。
- 固定样本不得来自运行时网络；实时页面只用于合并前健康检查。
- 不读取、打印或改动 `LLM_API_KEY`。Reddit 方案不增加 Secret、Cookie 或 OAuth。
- 不恢复 GitHub Release 或 arXiv，不改页面样式、栏目、日报条数上限和 LLM 接口。

## Task 1: 扩展来源配置模型

**Files:**

- Modify: `pyproject.toml`
- Modify: `src/ai_news/models.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: 写配置模型失败测试**

在 `tests/test_config.py` 增加下列覆盖：

```python
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "id": "anthropic",
                "name": "Anthropic",
                "kind": "official_page",
                "category": "大模型",
                "adapter": "anthropic_news",
            },
            "official_page sources require a url",
        ),
        (
            {
                "id": "anthropic",
                "name": "Anthropic",
                "kind": "official_page",
                "url": "https://www.anthropic.com/news",
                "category": "大模型",
            },
            "official_page sources require an adapter",
        ),
        (
            {
                "id": "anthropic",
                "name": "Anthropic",
                "kind": "official_page",
                "url": "https://www.anthropic.com/news",
                "adapter": "unknown_adapter",
                "category": "大模型",
            },
            "unknown official page adapter",
        ),
        (
            {
                "id": "reddit-ai",
                "name": "Reddit AI",
                "kind": "reddit_rss",
                "category": "AI 工具",
                "communities": [],
                "official": False,
            },
            "reddit_rss sources require communities",
        ),
        (
            {
                "id": "reddit-ai",
                "name": "Reddit AI",
                "kind": "reddit_rss",
                "url": "https://www.reddit.com/.rss",
                "category": "AI 工具",
                "communities": ["LocalLLaMA"],
                "official": False,
            },
            "reddit_rss sources do not accept a url",
        ),
        (
            {
                "id": "reddit-ai",
                "name": "Reddit AI",
                "kind": "reddit_rss",
                "category": "AI 工具",
                "communities": ["LocalLLaMA"],
                "official": True,
            },
            "reddit_rss sources must be unofficial with weight 5",
        ),
    ],
)
def test_source_spec_rejects_invalid_extended_source_configuration(
    payload: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        SourceSpec.model_validate(payload)


def test_source_spec_accepts_known_official_page_adapter() -> None:
    source = SourceSpec(
        id="anthropic",
        name="Anthropic",
        kind="official_page",
        url="https://www.anthropic.com/news",
        adapter="anthropic_news",
        category=Category.MODEL,
    )

    assert source.adapter == "anthropic_news"


def test_source_spec_accepts_unique_reddit_communities() -> None:
    source = SourceSpec(
        id="reddit-ai",
        name="Reddit AI",
        kind="reddit_rss",
        communities=["LocalLLaMA", "OpenAI"],
        category=Category.TOOL,
        weight=5,
        official=False,
    )

    assert source.communities == ["LocalLLaMA", "OpenAI"]
```

- [ ] **Step 2: 确认新测试失败**

Run: `python -m pytest tests/test_config.py -q`

Expected: FAIL，错误集中在 `official_page`、`reddit_rss`、`adapter` 或 `communities` 尚未定义。

- [ ] **Step 3: 增加解析依赖和严格字段模型**

在 `pyproject.toml` 的 `dependencies` 中加入：

```toml
"beautifulsoup4>=4.13,<5",
```

在 `src/ai_news/models.py` 增加固定适配器类型和字段：

```python
OfficialPageAdapter = Literal[
    "anthropic_news",
    "meta_ai_blog",
    "deepseek_news",
    "zhipu_releases",
    "minimax_releases",
    "tencent_hunyuan",
    "volcengine_doubao",
    "baidu_ernie",
]


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    name: str
    kind: Literal["feed", "arxiv", "github", "official_page", "reddit_rss"]
    url: HttpUrl | None = None
    repo: str | None = None
    adapter: OfficialPageAdapter | None = None
    communities: list[str] = Field(default_factory=list, max_length=20)
    category: Category
    weight: int = Field(default=5, ge=1, le=10)
    official: bool = True
    enabled: bool = True
```

扩展 `require_source_locator`，严格拒绝无关字段：

```python
if self.kind == "official_page":
    if self.url is None:
        raise ValueError("official_page sources require a url")
    if self.adapter is None:
        raise ValueError("official_page sources require an adapter")
    if self.repo is not None or self.communities:
        raise ValueError("official_page sources accept only url and adapter locators")
elif self.kind == "reddit_rss":
    if self.url is not None:
        raise ValueError("reddit_rss sources do not accept a url")
    if self.repo is not None or self.adapter is not None:
        raise ValueError("reddit_rss sources do not accept repo or adapter")
    if not self.communities:
        raise ValueError("reddit_rss sources require communities")
    if len(self.communities) != len(set(self.communities)):
        raise ValueError("reddit communities must be unique")
    if any(not re.fullmatch(r"[A-Za-z0-9_]{2,21}", value) for value in self.communities):
        raise ValueError("reddit communities must use valid subreddit names")
    if self.official or self.weight != 5:
        raise ValueError("reddit_rss sources must be unofficial with weight 5")
elif self.adapter is not None or self.communities:
    raise ValueError(f"{self.kind} sources do not accept adapter or communities")
```

Pydantic 的 `Literal` 会在未知适配器进入运行时前拒绝它。为满足稳定错误契约，在 `model_validator(mode="before")` 中把未知 `official_page.adapter` 统一转换为 `ValueError("unknown official page adapter")`。

- [ ] **Step 4: 运行配置回归**

Run: `python -m pytest tests/test_config.py -q`

Expected: PASS。

- [ ] **Step 5: 提交配置模型**

Run: `git add pyproject.toml src/ai_news/models.py tests/test_config.py && git diff --cached --check && git commit -m "feat: validate extended news source types"`

## Task 2: 建立官网页面解析公共边界

**Files:**

- Create: `src/ai_news/collectors/official_pages/__init__.py`
- Create: `src/ai_news/collectors/official_pages/base.py`
- Create: `tests/test_official_page_base.py`

- [ ] **Step 1: 写公共工具失败测试**

在 `tests/test_official_page_base.py` 验证文本清理、官方域名、日期时区、结构错误和元数据提取：

```python
from datetime import UTC, datetime

import pytest

from ai_news.collectors.official_pages.base import (
    OfficialEntry,
    OfficialPageStructureError,
    article_metadata,
    article_excerpt,
    clean_text,
    require_allowed_https_url,
    zoned_date,
)


def test_clean_text_strips_markup_entities_and_bounds_excerpt() -> None:
    value = clean_text("<b>A&amp;B</b>\n" + "x" * 5_000)
    assert value.startswith("A&B x")
    assert len(value) == 4_000


def test_require_allowed_https_url_accepts_only_configured_hosts() -> None:
    assert require_allowed_https_url(
        "/news/claude-opus-5", base_url="https://www.anthropic.com/news", allowed_hosts={"www.anthropic.com"}
    ) == "https://www.anthropic.com/news/claude-opus-5"
    with pytest.raises(ValueError, match="allowed official host"):
        require_allowed_https_url(
            "https://example.com/story", base_url="https://www.anthropic.com/news", allowed_hosts={"www.anthropic.com"}
        )


def test_zoned_date_converts_local_midnight_to_utc() -> None:
    assert zoned_date("2026-08-20", "Asia/Shanghai") == datetime(2026, 8, 19, 16, tzinfo=UTC)


def test_article_metadata_requires_explicit_publication_date() -> None:
    html = b'''<html><head><meta property="og:title" content="A useful AI product">
    <meta property="og:description" content="A concrete release with user impact."></head></html>'''
    assert article_metadata(html, "https://www.anthropic.com/news/item") is None


def test_article_metadata_reads_json_ld_publication_date() -> None:
    html = b'''<script type="application/ld+json">{
      "@type":"NewsArticle","headline":"A useful AI product",
      "description":"A concrete release with user impact.",
      "datePublished":"2026-08-20T09:00:00+08:00"
    }</script>'''
    metadata = article_metadata(html, "https://www.anthropic.com/news/item")
    assert metadata == OfficialEntry(
        title="A useful AI product",
        url="https://www.anthropic.com/news/item",
        published_at=datetime(2026, 8, 20, 1, tzinfo=UTC),
        excerpt="A concrete release with user impact.",
    )


def test_article_excerpt_does_not_invent_a_publication_date() -> None:
    html = b'''<meta property="og:description" content="A concrete release with user impact.">'''
    assert article_excerpt(html) == "A concrete release with user impact."
```

- [ ] **Step 2: 确认公共工具测试失败**

Run: `python -m pytest tests/test_official_page_base.py -q`

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: 实现公共类型和安全助手**

`src/ai_news/collectors/official_pages/base.py` 的公共契约为：

```python
@dataclass(frozen=True, slots=True)
class OfficialEntry:
    title: str
    url: str
    published_at: datetime
    excerpt: str = ""


class OfficialPageStructureError(ValueError):
    """The reviewed official page structure is no longer recognizable."""
```

实现以下约束：

- `clean_text` 用 `bleach.clean(..., tags=[], strip=True)`、`html.unescape` 和空白折叠，最长 4,000 字符。
- `require_allowed_https_url` 用 `urljoin` 解析相对链接，只接受 HTTPS、无 userinfo，hostname 必须精确命中适配器允许集合。
- `zoned_date` 用 `ZoneInfo` 解释只有日期、没有时区的官方日期，再转换为 UTC。
- `article_metadata` 只读 `NewsArticle`/`Article` JSON-LD 或 `article:published_time`、`og:title`、`og:description`；没有明确发布日期返回 `None`。
- `article_excerpt` 只返回清理后的 `description`/`og:description`/JSON-LD description，供已有索引日期的官网条目补摘要，绝不生成日期。
- `looks_like_challenge` 识别 `captcha`、`verify you are human`、`access denied` 和 `cf-chl-`。
- `to_raw_item(entry, source, source_name=None)` 集中构造 `RawItem`，保持 4,000 字符边界。

`src/ai_news/collectors/official_pages/__init__.py` 暂只导出公共类型；注册表在各适配器完成后补齐。

- [ ] **Step 4: 运行公共工具测试**

Run: `python -m pytest tests/test_official_page_base.py tests/test_http.py -q`

Expected: PASS；现有 HTTP 安全测试不变。

- [ ] **Step 5: 提交公共边界**

Run: `git add src/ai_news/collectors/official_pages tests/test_official_page_base.py && git diff --cached --check && git commit -m "feat: add safe official page parsing primitives"`

## Task 3: 实现 Anthropic、Meta AI 和 DeepSeek 适配器

**Files:**

- Create: `src/ai_news/collectors/official_pages/anthropic.py`
- Create: `src/ai_news/collectors/official_pages/meta.py`
- Create: `src/ai_news/collectors/official_pages/deepseek.py`
- Create: `tests/fixtures/official_pages/anthropic.html`
- Create: `tests/fixtures/official_pages/meta.html`
- Create: `tests/fixtures/official_pages/deepseek.html`
- Create: `tests/test_official_page_international.py`

- [ ] **Step 1: 固化最小真实结构样本并写失败测试**

每个 fixture 只保留已验证的语义结构和一条噪音记录，不复制完整网页。测试契约：

```python
@pytest.mark.parametrize(
    ("adapter", "fixture_name", "source", "expected_title", "expected_host"),
    [
        (anthropic.parse, "anthropic.html", ANTHROPIC, "Introducing Claude Opus 5", "www.anthropic.com"),
        (meta.parse, "meta.html", META, "Meta AI launches a new product", "ai.meta.com"),
        (deepseek.parse, "deepseek.html", DEEPSEEK, "DeepSeek-V4 Preview is live", "www.deepseek.com"),
    ],
)
def test_international_adapter_extracts_verified_entry(
    adapter: Parser,
    fixture_name: str,
    source: SourceSpec,
    expected_title: str,
    expected_host: str,
) -> None:
    entries = adapter(fixture(fixture_name), source)
    assert len(entries) == 1
    assert entries[0].title == expected_title
    assert urlsplit(entries[0].url).hostname == expected_host
    assert entries[0].published_at.tzinfo is UTC
    assert entries[0].excerpt


@pytest.mark.parametrize("adapter,source", [(anthropic.parse, ANTHROPIC), (meta.parse, META), (deepseek.parse, DEEPSEEK)])
def test_international_adapter_raises_on_structure_change(adapter: Parser, source: SourceSpec) -> None:
    with pytest.raises(OfficialPageStructureError):
        adapter(b"<html><body>unrecognized</body></html>", source)
```

- [ ] **Step 2: 确认适配器测试失败**

Run: `python -m pytest tests/test_official_page_international.py -q`

Expected: FAIL，三个适配器模块尚不存在。

- [ ] **Step 3: 实现三个语义结构解析器**

实现固定规则：

- Anthropic：只接受包含 `/news/` 的文章锚点；从同一锚点内的 `h2`、`time`、摘要段落读取；日期按 `America/Los_Angeles` 解释。
- Meta AI：按规范 href 聚合同一卡片的多个锚点，排除 `Learn More`、`Blog`、`Research`、`Open Source`、`Featured`，选最长的非通用标题；从最近祖先文本读取英文日期和摘要；日期按 `America/Los_Angeles` 解释。
- DeepSeek：只接受 `a.ds-news-hero-card[href]`；从 `h2`、包含英文日期的 `span` 和正文段落读取；日期按 `Asia/Shanghai` 解释。
- 每个解析器先检查挑战页；看不到已审查容器时抛 `OfficialPageStructureError`；容器有效但没有可用条目时返回空列表。

每个模块都公开 `parse(payload: bytes, source: SourceSpec) -> list[OfficialEntry]`，不接受运行时选择器或额外 URL。

- [ ] **Step 4: 运行国际厂商适配器测试**

Run: `python -m pytest tests/test_official_page_international.py tests/test_official_page_base.py -q`

Expected: PASS。

- [ ] **Step 5: 提交国际厂商适配器**

Run: `git add src/ai_news/collectors/official_pages tests/fixtures/official_pages tests/test_official_page_international.py && git diff --cached --check && git commit -m "feat: parse international model vendor news"`

## Task 4: 实现智谱和 MiniMax Markdown 适配器

**Files:**

- Create: `src/ai_news/collectors/official_pages/zhipu.py`
- Create: `src/ai_news/collectors/official_pages/minimax.py`
- Create: `tests/fixtures/official_pages/zhipu.md`
- Create: `tests/fixtures/official_pages/minimax.md`
- Create: `tests/test_official_page_markdown.py`

- [ ] **Step 1: 写 Markdown 解析失败测试**

```python
def test_zhipu_parses_update_block_and_ignores_non_release_text() -> None:
    entries = zhipu.parse(fixture("zhipu.md"), ZHIPU)
    assert [(entry.title, entry.url) for entry in entries] == [
        ("GLM-5.3 新一代旗舰模型上线", "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3")
    ]
    assert entries[0].published_at == datetime(2026, 8, 18, 16, tzinfo=UTC)


def test_minimax_requires_full_day_heading_and_model_card() -> None:
    entries = minimax.parse(fixture("minimax.md"), MINIMAX)
    assert [entry.title for entry in entries] == ["MiniMax H3"]
    assert entries[0].published_at == datetime(2026, 7, 30, 16, tzinfo=UTC)


@pytest.mark.parametrize("adapter,source", [(zhipu.parse, ZHIPU), (minimax.parse, MINIMAX)])
def test_markdown_adapter_raises_when_reviewed_markers_disappear(adapter: Parser, source: SourceSpec) -> None:
    with pytest.raises(OfficialPageStructureError):
        adapter(b"# ordinary markdown", source)
```

- [ ] **Step 2: 确认 Markdown 测试失败**

Run: `python -m pytest tests/test_official_page_markdown.py -q`

Expected: FAIL，适配器尚不存在。

- [ ] **Step 3: 实现固定 Markdown 结构**

- 智谱按 `<Update label="YYYY-M-D" description="...">...</Update>` 分块；标题取 `description`，链接取块内首个 HTTPS Markdown 链接，摘要取清理后的项目符号；按 `Asia/Shanghai` 解释日期。
- MiniMax 按 `## YYYY 年 M 月 D 日` 建立当前完整日期，只采集该日期下的 `<Card title="..." href="...">...</Card>`；`## YYYY 年 M 月` 这种只有月份的标题不生成条目；只接受 `minimaxi.com` 官方链接。
- 智谱只保留模型、产品或平台发布关键词；MiniMax 只保留模型 Card，接口参数和普通修复文本不生成条目。

- [ ] **Step 4: 运行 Markdown 适配器测试**

Run: `python -m pytest tests/test_official_page_markdown.py tests/test_official_page_base.py -q`

Expected: PASS。

- [ ] **Step 5: 提交中文 Markdown 适配器**

Run: `git add src/ai_news/collectors/official_pages tests/fixtures/official_pages tests/test_official_page_markdown.py && git diff --cached --check && git commit -m "feat: parse Chinese model release notes"`

## Task 5: 实现腾讯、火山引擎和百度适配器

**Files:**

- Create: `src/ai_news/collectors/official_pages/tencent.py`
- Create: `src/ai_news/collectors/official_pages/volcengine.py`
- Create: `src/ai_news/collectors/official_pages/baidu.py`
- Create: `tests/fixtures/official_pages/tencent.html`
- Create: `tests/fixtures/official_pages/volcengine.html`
- Create: `tests/fixtures/official_pages/baidu.html`
- Create: `tests/test_official_page_china.py`

- [ ] **Step 1: 写品牌预筛和结构失败测试**

```python
def test_tencent_keeps_only_hunyuan_article() -> None:
    entries = tencent.parse(fixture("tencent.html"), TENCENT)
    assert [entry.title for entry in entries] == ["腾讯混元发布全新产品能力"]


def test_volcengine_reads_router_data_and_keeps_doubao_or_ark() -> None:
    entries = volcengine.parse(fixture("volcengine.html"), VOLCENGINE)
    assert [entry.url for entry in entries] == ["https://www.volcengine.com/news/detail/123456"]
    assert entries[0].excerpt == "豆包与火山方舟面向用户发布重要能力。"


def test_baidu_reads_next_data_and_keeps_ernie_or_qianfan() -> None:
    entries = baidu.parse(fixture("baidu.html"), BAIDU)
    assert [entry.url for entry in entries] == ["https://cloud.baidu.com/news/news_98765"]
    assert entries[0].excerpt == "文心与千帆平台的重要产品更新。"


@pytest.mark.parametrize("adapter,source", [(tencent.parse, TENCENT), (volcengine.parse, VOLCENGINE), (baidu.parse, BAIDU)])
def test_chinese_newsroom_adapter_rejects_missing_reviewed_structure(adapter: Parser, source: SourceSpec) -> None:
    with pytest.raises(OfficialPageStructureError):
        adapter(b"<html><body>empty newsroom</body></html>", source)
```

- [ ] **Step 2: 确认中文官网测试失败**

Run: `python -m pytest tests/test_official_page_china.py -q`

Expected: FAIL，三个适配器模块尚不存在。

- [ ] **Step 3: 实现固定结构和品牌过滤**

- 腾讯：只解析 `article[id^="post-"]`，从 `h2.blog-title a` 和 `.tc-blogpost-date` 取链接、标题和日期；卡片全文必须命中 `混元|Hunyuan`；日期按 `Asia/Shanghai` 解释。
- 火山引擎：定位 `window._ROUTER_DATA = {...};`，解析 `loaderData["__ssr_without_user/news/page"]` 下的 `banner` 和 `listOnlineArticle.List`；用 `DocumentID` 生成 `/news/detail/{id}`；标题、摘要、标签或分类必须命中 `豆包|方舟|大模型|Doubao|ARK`。
- 百度：解析 `script#__NEXT_DATA__` 的 `props.pageProps.pageData.listData.result`；使用 `newsId`、`title`、`time`、`abstract` 或清理后的 `content`；标题、摘要或关键词必须命中 `文心|ERNIE|千帆`。
- JSON 缺失、类型不符或根结构改变时抛安全的 `OfficialPageStructureError`，错误中不包含页面内容。

- [ ] **Step 4: 运行中文官网适配器测试**

Run: `python -m pytest tests/test_official_page_china.py tests/test_official_page_base.py -q`

Expected: PASS。

- [ ] **Step 5: 提交中文官网适配器**

Run: `git add src/ai_news/collectors/official_pages tests/fixtures/official_pages tests/test_official_page_china.py && git diff --cached --check && git commit -m "feat: parse Chinese model vendor newsrooms"`

## Task 6: 接通官网采集器和流水线分派

**Files:**

- Modify: `src/ai_news/collectors/official_pages/__init__.py`
- Modify: `src/ai_news/pipeline/run.py`
- Create: `tests/test_official_page_collector.py`
- Modify: `tests/test_pipeline_run.py`

- [ ] **Step 1: 写注册表、请求上限和流水线失败隔离测试**

测试必须覆盖：

```python
def test_official_collector_limits_index_and_detail_fetches() -> None:
    # 固定解析器返回 30 条，其中 10 条摘要不足；只允许前 8 条触发详情读取。
    assert len(detail_requests) == 8
    assert len(items) <= 30


def test_official_collector_drops_only_failed_detail_entry() -> None:
    # 一个详情页失败，其他条目仍转换为 RawItem。
    assert {item.title for item in items} == {"Complete index item", "Successful detail item"}


def test_pipeline_dispatches_official_page_and_isolates_structure_failure() -> None:
    assert runs_by_id["anthropic"].success is False
    assert runs_by_id["openai"].success is True
    assert report.degraded is True
```

使用依赖注入的 `fetcher` 和固定 parser，不发真实网络请求。

- [ ] **Step 2: 确认采集和分派测试失败**

Run: `python -m pytest tests/test_official_page_collector.py tests/test_pipeline_run.py -q`

Expected: FAIL，注册表和 `official_page` 分派尚未实现。

- [ ] **Step 3: 实现注册表和受限详情补充**

`src/ai_news/collectors/official_pages/__init__.py` 使用显式注册表：

```python
PARSERS: dict[OfficialPageAdapter, Parser] = {
    "anthropic_news": anthropic.parse,
    "meta_ai_blog": meta.parse,
    "deepseek_news": deepseek.parse,
    "zhipu_releases": zhipu.parse,
    "minimax_releases": minimax.parse,
    "tencent_hunyuan": tencent.parse,
    "volcengine_doubao": volcengine.parse,
    "baidu_ernie": baidu.parse,
}
MAX_INDEX_ENTRIES = 30
MAX_DETAIL_FETCHES = 8
MIN_INDEX_EXCERPT_LENGTH = 80
```

实现：

```python
async def collect_official_page(
    client: httpx.AsyncClient,
    source: SourceSpec,
    *,
    fetcher: Fetcher = get_bytes,
) -> list[RawItem]:
    if source.kind != "official_page" or source.url is None or source.adapter is None:
        raise ValueError("official_page collector requires a valid source")
    payload = await fetcher(client, str(source.url))
    entries = PARSERS[source.adapter](payload, source)[:MAX_INDEX_ENTRIES]
    items: list[RawItem] = []
    detail_fetches = 0
    for entry in entries:
        selected_entry = entry
        if len(entry.excerpt) < MIN_INDEX_EXCERPT_LENGTH:
            if detail_fetches == MAX_DETAIL_FETCHES:
                continue
            detail_fetches += 1
            try:
                detail_payload = await fetcher(client, entry.url)
                excerpt = article_excerpt(detail_payload)
            except Exception:
                continue
            if len(excerpt) < MIN_INDEX_EXCERPT_LENGTH:
                continue
            selected_entry = dataclasses.replace(entry, excerpt=excerpt)
        items.append(to_raw_item(selected_entry, source))
    return items
```

详情 URL 必须已由适配器验证为允许官方 host；`article_excerpt` 只用于补摘要，日期始终使用索引中已验证的官方日期，不能把抓取时间作为发布日期。摘要仍不足 80 个字符且详情读取失败的条目丢弃，其他条目保留。实现时把宽泛的 `except Exception` 收紧为 HTTP、解析和验证边界已声明的异常类型，并让测试注入的读取失败使用同一异常类型。

- [ ] **Step 4: 在 `_collect_live` 增加官网分派**

在 `src/ai_news/pipeline/run.py` 导入并分派：

```python
if source.kind == "official_page":
    return await collect_official_page(client, source)
```

保留 `_collect_one` 的现有异常隔离和 `SourceRun.failed` 行为。

- [ ] **Step 5: 运行采集和流水线测试**

Run: `python -m pytest tests/test_official_page_collector.py tests/test_pipeline_run.py -q`

Expected: PASS。

- [ ] **Step 6: 提交官网采集链路**

Run: `git add src/ai_news/collectors/official_pages src/ai_news/pipeline/run.py tests/test_official_page_collector.py tests/test_pipeline_run.py && git diff --cached --check && git commit -m "feat: collect official model vendor pages"`

## Task 7: 实现无需密钥的 Reddit RSS 线索采集器

**Files:**

- Create: `src/ai_news/collectors/reddit_rss.py`
- Create: `tests/fixtures/reddit/new.atom`
- Create: `tests/fixtures/reddit/top.atom`
- Create: `tests/fixtures/reddit/article.html`
- Create: `tests/test_reddit_rss_collector.py`
- Modify: `src/ai_news/pipeline/run.py`
- Modify: `tests/test_pipeline_run.py`

- [ ] **Step 1: 写 Atom 解析、过滤和限流失败测试**

固定 Atom 样本同时包含可信外链、自帖、Reddit 媒体、图片、短链接、未知域名、求助、传闻、Meme 和自我推广。测试契约：

```python
def test_reddit_feed_urls_are_two_combined_fixed_feeds() -> None:
    urls = reddit_feed_urls(REDDIT_SOURCE.communities)
    joined = "+".join(REDDIT_SOURCE.communities)
    assert urls == (
        f"https://www.reddit.com/r/{joined}/new/.rss?limit=100",
        f"https://www.reddit.com/r/{joined}/top/.rss?sort=top&t=day&limit=100",
    )


def test_parse_reddit_atom_keeps_only_external_link_and_community() -> None:
    entries = parse_reddit_atom(fixture("new.atom"), REDDIT_SOURCE)
    assert [(entry.community, entry.external_url) for entry in entries] == [
        ("LocalLLaMA", "https://www.anthropic.com/news/claude-product")
    ]


def test_reddit_requests_are_sequential_and_at_least_twenty_seconds_apart() -> None:
    sleeps: list[float] = []
    items = asyncio.run(
        collect_reddit_rss(
            client,
            REDDIT_SOURCE,
            official_domains={"anthropic.com"},
            fetcher=fake_fetcher,
            sleep=fake_sleep(sleeps),
        )
    )
    assert request_order[:2] == ["new", "top"]
    assert 20 in sleeps
    assert items[0].source_name == "Reddit · r/LocalLLaMA"
    assert items[0].source_weight == 5
    assert items[0].is_official_source is False


def test_original_article_requires_allowed_domain_topic_and_date() -> None:
    assert build_reddit_item(entry, article_without_date, REDDIT_SOURCE, allowed_domains) is None
    assert build_reddit_item(entry, article_wrong_topic, REDDIT_SOURCE, allowed_domains) is None
    assert build_reddit_item(entry, article_unknown_domain, REDDIT_SOURCE, allowed_domains) is None
```

- [ ] **Step 2: 确认 Reddit 测试失败**

Run: `python -m pytest tests/test_reddit_rss_collector.py -q`

Expected: FAIL，Reddit 模块尚不存在。

- [ ] **Step 3: 实现固定 RSS 地址和 Atom 预筛**

`src/ai_news/collectors/reddit_rss.py` 固定边界：

```python
TRUSTED_MEDIA_DOMAINS = frozenset(
    {"reuters.com", "apnews.com", "theverge.com", "arstechnica.com", "techcrunch.com", "wired.com"}
)
MAX_FEED_ENTRIES = 100
MAX_EXTERNAL_FETCHES = 8
REQUEST_SPACING_SECONDS = 20
```

- URL 只能由配置的社区名构造，不接受配置 URL。
- `feedparser` 解析 Atom；社区从 category term 或 `/r/<name>/` 链接提取，且必须属于配置白名单。
- 正文用 Beautiful Soup 找锚文本精确为 `[link]` 的 HTTPS 目标。
- 拒绝 `reddit.com`、`redd.it`、`i.redd.it`、`v.redd.it`、`preview.redd.it`、`external-preview.redd.it`，以及图片/视频扩展名和短链接。
- 标题负面规则拒绝 `help`、`question`、`rumor`、`unconfirmed`、`meme`、`self promo`、`my app`、`I built`、`showcase` 等求助、传闻和自推表达。
- 初筛主题至少命中 `AI`、`artificial intelligence`、`LLM`、`large language model`、`GPT`、`Claude`、`Gemini`、`Llama`、`Mistral`、`DeepSeek`、`ChatGPT`、`Copilot`、`Perplexity`、`Stable Diffusion`、`agent`、`generative`、`大模型`、`人工智能`、`生成式`、`智能体`、`豆包`、`文心`、`千问`、`混元`、`智谱` 或 `MiniMax`。
- `new` 和 `top/day` 先按规范外链去重，最多对排序最前的 8 个候选补读原文。

- [ ] **Step 4: 实现原文验证和 RawItem 转换**

外部域名必须命中启用官方来源 hostname 或 `TRUSTED_MEDIA_DOMAINS`；允许 `www.` 和可信根域的子域，不允许相似后缀。使用 `article_metadata` 读取原文标题、摘要和明确发布日期：

```python
RawItem(
    source_id=source.id,
    source_name=f"Reddit · r/{entry.community}",
    source_weight=5,
    title=metadata.title,
    url=metadata.url,
    published_at=metadata.published_at,
    excerpt=metadata.excerpt,
    category_hint=source.category,
    is_official_source=False,
)
```

原文没有明确日期、主题不匹配、跳转离开允许域名或读取失败时只丢弃该条。RSS 两次请求必须串行，中间 `await sleep(20)`；RSS 自身最终 `403/429` 直接抛出，让 `_collect_one` 将 `reddit-ai` 标为单源失败，不做 HTML 或登录降级。

- [ ] **Step 5: 让流水线动态提供启用官方域名**

在 `src/ai_news/pipeline/run.py` 从启用且 `official`、有 URL 的来源生成 hostname 集合，并为默认采集器建立闭包：

```python
official_domains = frozenset(
    urlsplit(str(source.url)).hostname
    for source in source_config.sources
    if source.enabled and source.official and source.url is not None
)

async def live_collector(client: httpx.AsyncClient, source: SourceSpec) -> list[RawItem]:
    if source.kind == "reddit_rss":
        return await collect_reddit_rss(
            client, source, official_domains={value for value in official_domains if value is not None}
        )
    return await _collect_live(client, source)
```

仅在没有注入测试 collector 时使用此闭包，保持现有测试注入协议不变。

- [ ] **Step 6: 运行 Reddit 和流水线测试**

Run: `python -m pytest tests/test_reddit_rss_collector.py tests/test_pipeline_run.py tests/test_http.py -q`

Expected: PASS；测试证明两条 RSS 不并发、间隔 20 秒、外部读取最多 8 次、无密钥、单源失败隔离。

- [ ] **Step 7: 提交 Reddit RSS 链路**

Run: `git add src/ai_news/collectors/reddit_rss.py src/ai_news/pipeline/run.py tests/fixtures/reddit tests/test_reddit_rss_collector.py tests/test_pipeline_run.py && git diff --cached --check && git commit -m "feat: add bounded Reddit RSS discovery"`

## Task 8: 更新正式来源清单和运维文档

**Files:**

- Modify: `sources/feeds.yaml`
- Modify: `tests/test_config.py`
- Modify: `README.md`
- Modify: `tests/test_readme_contract.py`

- [ ] **Step 1: 先扩展清单契约测试**

把 `test_repository_source_manifest_contains_only_verified_sources` 的 tuple 加入 `adapter` 和 `communities`，并将预期启用来源固定为：

```python
expected_enabled_ids = {
    "openai", "google-ai", "deepmind", "hugging-face",
    "mistral", "qwen", "anthropic", "meta-ai", "deepseek",
    "zhipu-glm", "minimax", "tencent-hunyuan", "volcengine-doubao",
    "baidu-ernie", "reddit-ai",
}
assert {source.id for source in config.sources if source.enabled} == expected_enabled_ids
assert config.sources[-1].communities == [
    "LocalLLaMA", "OpenAI", "ClaudeAI", "GeminiAI", "artificial",
    "ChatGPT", "aiagents", "perplexity_ai", "StableDiffusion",
]
```

增加 README 合约：必须说明 Reddit RSS 无需 Secret、官网页面只走允许列表适配器、没有通用网页抓取，以及 README 链接本设计和本实施计划。

- [ ] **Step 2: 确认清单和文档测试失败**

Run: `python -m pytest tests/test_config.py tests/test_readme_contract.py -q`

Expected: FAIL，新增来源和文档尚未落地。

- [ ] **Step 3: 按确认规格更新 `sources/feeds.yaml`**

新增两项 `feed`：

```yaml
  - id: mistral
    name: Mistral
    kind: feed
    url: https://mistral.ai/news/rss
    category: 大模型
    weight: 9
    official: true
  - id: qwen
    name: 通义千问 Qwen
    kind: feed
    url: https://qwenlm.github.io/blog/index.xml
    category: 大模型
    weight: 10
    official: true
```

新增八项 `official_page`，URL 与 adapter 精确为：

```yaml
  - {id: anthropic, name: Anthropic, kind: official_page, url: https://www.anthropic.com/news, adapter: anthropic_news, category: 大模型, weight: 10, official: true}
  - {id: meta-ai, name: Meta AI, kind: official_page, url: https://ai.meta.com/blog/, adapter: meta_ai_blog, category: 大模型, weight: 9, official: true}
  - {id: deepseek, name: DeepSeek, kind: official_page, url: https://www.deepseek.com/en/news/, adapter: deepseek_news, category: 大模型, weight: 10, official: true}
  - {id: zhipu-glm, name: 智谱 GLM, kind: official_page, url: https://docs.bigmodel.cn/cn/update/new-releases.md, adapter: zhipu_releases, category: 大模型, weight: 9, official: true}
  - {id: minimax, name: MiniMax, kind: official_page, url: https://platform.minimaxi.com/docs/release-notes/models.md, adapter: minimax_releases, category: 大模型, weight: 9, official: true}
  - {id: tencent-hunyuan, name: 腾讯混元, kind: official_page, url: https://www.tencent.com/zh-cn/newsroom/all-news/, adapter: tencent_hunyuan, category: 大模型, weight: 8, official: true}
  - {id: volcengine-doubao, name: 豆包与火山方舟, kind: official_page, url: https://www.volcengine.com/news, adapter: volcengine_doubao, category: 大模型, weight: 9, official: true}
  - {id: baidu-ernie, name: 百度文心, kind: official_page, url: https://cloud.baidu.com/news/news, adapter: baidu_ernie, category: 大模型, weight: 8, official: true}
```

新增唯一 `reddit_rss`：

```yaml
  - id: reddit-ai
    name: Reddit AI 线索
    kind: reddit_rss
    communities:
      - LocalLLaMA
      - OpenAI
      - ClaudeAI
      - GeminiAI
      - artificial
      - ChatGPT
      - aiagents
      - perplexity_ai
      - StableDiffusion
    category: AI 工具
    weight: 5
    official: false
```

保留 `arxiv-ai` 为 disabled，不添加任何 GitHub source。

- [ ] **Step 4: 更新 README 的真实数据流和安全说明**

替换 `Feed、GitHub Releases 和 arXiv` 的过时描述，说明：

- 官方 RSS/Atom 优先；无 RSS 的厂商只由代码内允许列表适配器解析。
- Reddit 只用两条合并 RSS，不需要 `REDDIT_CLIENT_ID`、`REDDIT_CLIENT_SECRET`、Cookie 或登录。
- Reddit 仅作为线索，最终展示经允许域名、主题、发布日期验证后的外部原文。
- 页面改版标记来源失败，不允许通用抓取或绕过挑战。
- 数据源目录说明更新为 `Feed、官方页面适配器、Reddit RSS`。
- 链接：`docs/superpowers/specs/2026-08-23-official-model-vendor-sources-design.md` 和 `docs/superpowers/plans/2026-08-23-official-model-vendor-sources.md`。

- [ ] **Step 5: 运行配置和文档测试**

Run: `python -m pytest tests/test_config.py tests/test_readme_contract.py -q`

Expected: PASS。

- [ ] **Step 6: 提交来源清单和文档**

Run: `git add sources/feeds.yaml tests/test_config.py README.md tests/test_readme_contract.py && git diff --cached --check && git commit -m "docs: register official and Reddit RSS sources"`

## Task 9: 完整离线验证与实时来源预检

**Files:**

- Create: `src/ai_news/source_health.py`
- Create: `scripts/check_source_health.py`
- Create: `tests/test_source_health.py`
- Create: `.github/workflows/source-health.yml`

- [ ] **Step 1: 先写安全输出、成功门槛和失败退出测试**

`tests/test_source_health.py` 使用注入的 collector，不访问网络：

```python
def test_health_check_requires_nonempty_result_for_every_enabled_new_source() -> None:
    results = asyncio.run(
        check_source_health(CONFIG, collector=fake_collector_with_one_empty_source)
    )
    assert [(result.source_id, result.healthy) for result in results] == [
        ("anthropic", True),
        ("reddit-ai", False),
    ]
    assert results[1].error_type == "EmptySourceResult"


def test_health_output_contains_only_safe_fields() -> None:
    line = format_health_result(
        SourceHealthResult(
            source_id="anthropic",
            healthy=False,
            item_count=0,
            latest_date=None,
            error_type="HttpStatusFetchError",
        )
    )
    assert line == "anthropic failed 0 - HttpStatusFetchError"
    assert "https://" not in line


def test_disabled_reddit_is_skipped_without_failing_official_sources() -> None:
    results = asyncio.run(check_source_health(CONFIG_WITH_REDDIT_DISABLED, collector=fake_collector))
    assert [result.source_id for result in results] == ["anthropic"]
    assert all(result.healthy for result in results)
```

- [ ] **Step 2: 确认来源健康测试失败**

Run: `python -m pytest tests/test_source_health.py -q`

Expected: FAIL，健康检查模块尚不存在。

- [ ] **Step 3: 实现可测试的健康检查和只读脚本**

`src/ai_news/source_health.py` 固定只检查本次新增来源：

```python
NEW_OFFICIAL_SOURCE_IDS = frozenset(
    {
        "mistral", "qwen", "anthropic", "meta-ai", "deepseek", "zhipu-glm",
        "minimax", "tencent-hunyuan", "volcengine-doubao", "baidu-ernie",
    }
)


@dataclass(frozen=True, slots=True)
class SourceHealthResult:
    source_id: str
    healthy: bool
    item_count: int
    latest_date: date | None
    error_type: str | None
```

默认 collector 对 `feed` 调用 `get_bytes + parse_feed`，对 `official_page` 调用 `collect_official_page`，对 `reddit_rss` 调用 `collect_reddit_rss` 并动态传入启用官方来源 hostname。`check_source_health` 按 source id 串行检查 10 个官方来源和启用的 Reddit；零条视为 `EmptySourceResult`，异常只保留类型名，不保留异常对象或 URL。`format_health_result` 只输出 source id、`ok/failed`、条数、最新日期和安全错误类型。

`scripts/check_source_health.py` 的完整入口行为：

```python
from __future__ import annotations

import asyncio
from pathlib import Path

from ai_news.config import load_source_config
from ai_news.source_health import check_source_health, format_health_result


def main() -> int:
    config = load_source_config(Path("sources/feeds.yaml"))
    results = asyncio.run(check_source_health(config))
    for result in results:
        print(format_health_result(result))
    return 0 if results and all(result.healthy for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 增加 GitHub Actions 合并前实时检查**

`.github/workflows/source-health.yml`：

```yaml
name: Source health

on:
  pull_request:
    paths:
      - sources/feeds.yaml
      - "src/ai_news/collectors/**"
      - src/ai_news/http.py
      - src/ai_news/models.py
      - src/ai_news/source_health.py
      - scripts/check_source_health.py
      - .github/workflows/source-health.yml
  workflow_dispatch: {}

permissions:
  contents: read

jobs:
  live-source-health:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Check out repository
        uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6
      - name: Set up Python
        uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1 # v6
        with:
          python-version: "3.12"
          cache: pip
      - name: Install dependencies
        run: python -m pip install .
      - name: Check live source health
        run: python scripts/check_source_health.py
```

这项工作流不注入任何 Secret。若 GitHub Actions 中 Reddit 返回 `403/429` 或无法形成可信外链，PR 检查会失败；将 `reddit-ai.enabled` 改为 `false` 后重跑，官方厂商来源仍必须全部通过。

- [ ] **Step 5: 运行健康模块测试并提交**

Run: `python -m pytest tests/test_source_health.py -q`

Expected: PASS。

Run: `git add src/ai_news/source_health.py scripts/check_source_health.py tests/test_source_health.py .github/workflows/source-health.yml && git diff --cached --check && git commit -m "ci: verify live source health"`

- [ ] **Step 6: 更新环境并运行静态检查**

Run: `python -m pip install -e '.[dev]'`

Expected: 安装成功，包含 `beautifulsoup4`。

Run: `python -m ruff check . && python -m ruff format --check .`

Expected: 两条命令均退出 0。

- [ ] **Step 7: 运行完整测试和覆盖率门槛**

Run: `python -m pytest --cov=ai_news --cov-report=term-missing --cov-fail-under=85 -q`

Expected: 全部通过，覆盖率不低于 85%。

- [ ] **Step 8: 运行离线端到端、密钥扫描、构建和校验**

Run: `python -m ai_news demo --root .demo --output .preview/ai-news`

Expected: 生成演示日报且不访问网络。

Run: `python scripts/check_no_secrets.py`

Expected: 退出 0，不输出真实密钥。

Run: `python -m ai_news build --root . --output .preview/ai-news && python scripts/validate_site.py .preview/ai-news --base-path /ai-news/`

Expected: `dist/` 构建和静态校验成功。

Run:

```bash
docker run --rm \
  -v "$PWD:/repo" \
  -w /repo \
  rhysd/actionlint:1.7.7@sha256:887a259a5a534f3c4f36cb02dca341673c6089431057242cdc931e9f133147e9 \
  -ignore 'element of "schedule" section must be mapping and must contain one key "cron"' \
  .github/workflows/ci.yml .github/workflows/daily-news.yml .github/workflows/source-health.yml
```

Expected: GitHub Actions 工作流语法通过。

- [ ] **Step 9: 在本机运行同一套新增来源实时预检**

Run: `python scripts/check_source_health.py`

验收：

- 10 个厂商来源都能访问且每个至少解析一条带明确日期的历史记录；任何未通过者先修复或从启用清单移除。
- Reddit 能得到 Atom 且至少识别一条外链结构；若 GitHub 执行环境继续返回 403/429，则只把 `reddit-ai.enabled` 设为 `false`，10 个官方厂商来源继续上线。
- 输出不得包含 Reddit 用户、帖子正文、评论、投票或 Secret。

- [ ] **Step 10: 复跑所有验证并检查工作树**

Run: `python -m ruff check . && python -m ruff format --check . && python -m pytest --cov=ai_news --cov-fail-under=85 -q && python scripts/check_no_secrets.py && git diff --check && git status --short`

Expected: 所有验证退出 0；`git status --short` 只包含实时验证后确有必要的修复。

- [ ] **Step 11: 若实时检查产生修复，单独提交**

Run: `git add src tests sources scripts .github/workflows README.md pyproject.toml && git diff --cached --check && git commit -m "fix: harden live source adapters"`

如果没有修复，跳过提交，不创建空 commit。

## Task 10: PR、Actions 和正式站点验收

**Files:**

- No planned source changes; production validation only.

- [ ] **Step 1: 推送分支并创建 PR**

Run: `git push -u origin codex/add-official-model-blogs`

Expected: 推送成功。

Run: `gh pr create --base main --head codex/add-official-model-blogs --title "feat: expand official AI sources and Reddit RSS" --body "新增 10 个大模型厂商官方来源和一个受限 Reddit RSS 线索源；Reddit 无需密钥。包含固定页面适配器、20 秒 RSS 间隔、可信外链与发布日期校验、单源失败隔离，以及完整离线测试。"`

Expected: 返回 `mount-su/ai-news` 的 PR URL。

- [ ] **Step 2: 等待并核对 PR 检查**

Run: `gh pr checks --watch`

Expected: CI、Ruff、pytest/coverage、Secret 扫描、build/validate 和 actionlint 全部成功。

- [ ] **Step 3: 合并后手动运行每日流水线**

Run: `gh workflow run daily-news.yml --ref main`

Run: `gh run list --workflow daily-news.yml --branch main --limit 1`

Run:

```bash
news_run_id="$(gh run list --workflow daily-news.yml --branch main --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "$news_run_id" --exit-status
```

Expected: 生成与 Pages 部署 job 全部成功；`news_run_id` 来自刚触发后的最新 `main` 运行，不复用旧 run。

- [ ] **Step 4: 核对来源健康记录和正式页面**

Run: `gh run view "$news_run_id" --log`

Expected: 10 个新增厂商来源和 `reddit-ai` 各有独立安全状态；零条可以是正常无更新，结构错误必须显示为单源失败而非静默零条；日志没有正文、凭证或完整异常对象。

打开并检查：

- `https://mount-su.github.io/ai-news/`
- `https://mount-su.github.io/ai-news/archive/`
- 最新一期：先从 `origin/main:data/index.json` 读取首条日期，再访问对应 `/days/<日期>/` 路由。

Run:

```bash
git fetch origin main
news_latest_date="$(git show origin/main:data/index.json | python -c 'import json,sys; print(json.load(sys.stdin)["reports"][0]["date"])')"
curl --fail --silent --show-error "https://mount-su.github.io/ai-news/days/${news_latest_date}/" >/dev/null
```

验收：最新日报链接只指向厂商官方域名或 Reddit 允许的可信外部域名；内容仍精炼，技术文章具有明确影响；桌面和移动视口均无样式回归。

- [ ] **Step 5: 交付证据**

最终回复同时列出：PR URL、合并 commit、Actions run URL、Pages URL、测试数量与覆盖率、新增来源健康结果，以及 Reddit 是否启用。明确区分“本地通过”“Actions 通过”和“正式页面已更新”。
