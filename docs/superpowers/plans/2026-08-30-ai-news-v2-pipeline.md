# AI News V2 Pipeline and Source Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make daily generation quality-first, diagnosable, source-diverse, and safe for historical regeneration without adding credentials or a server.

**Architecture:** Keep the existing collector → normalize → filter → rank → LLM → storage flow. Add an explicit source role to facts as they enter the pipeline, carry per-source stage counts through one immutable preparation result, persist a safe run record separately from a published report, and let the LLM choose at most seven items without a hard minimum.

**Tech Stack:** Python 3.12, Pydantic 2, HTTPX, feedparser, pytest, Ruff, YAML, GitHub Actions.

---

### Task 1: Add source roles and register vetted media feeds

**Files:**
- Modify: `src/ai_news/models.py`
- Modify: `src/ai_news/collectors/feed.py`
- Modify: `src/ai_news/collectors/arxiv.py`
- Modify: `src/ai_news/collectors/github.py`
- Modify: `src/ai_news/collectors/official_pages/base.py`
- Modify: `src/ai_news/collectors/reddit_rss.py`
- Modify: `src/ai_news/pipeline/run.py`
- Modify: `sources/feeds.yaml`
- Test: `tests/test_config.py`
- Test: `tests/test_arxiv_collector.py`
- Test: `tests/test_feed_collector.py`
- Test: `tests/test_github_collector.py`
- Test: `tests/test_official_page_base.py`
- Test: `tests/test_reddit_rss_collector.py`

- [ ] **Step 1: Write failing role and source-manifest tests**

Add assertions equivalent to:

```python
assert SourceSpec(
    id="media",
    name="Media",
    kind="feed",
    url="https://example.com/feed.xml",
    category=Category.TOOL,
    official=False,
).role is SourceRole.TRUSTED_MEDIA

assert SourceSpec(
    id="official",
    name="Official",
    kind="feed",
    url="https://example.com/feed.xml",
    category=Category.MODEL,
).role is SourceRole.PRIMARY
```

Extend `test_repository_source_manifest_contains_only_verified_sources` with these exact sources:

```python
("techcrunch-ai", "https://techcrunch.com/category/artificial-intelligence/feed/", "trusted_media")
("venturebeat-ai", "https://venturebeat.com/category/ai/feed", "trusted_media")
("the-verge-ai", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "trusted_media")
("the-decoder", "https://the-decoder.com/feed/", "trusted_media")
```

Assert `producthunt-ai` and `reddit-ai` are `discovery`. Assert a parsed `RawItem` preserves its configured role.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python -m pytest tests/test_config.py::test_repository_source_manifest_contains_only_verified_sources tests/test_feed_collector.py tests/test_reddit_rss_collector.py -q
```

Expected: FAIL because `SourceRole`, `role`, and the four feeds do not exist.

- [ ] **Step 3: Implement the source-role model and propagation**

Add the enum and fields:

```python
class SourceRole(StrEnum):
    PRIMARY = "primary"
    TRUSTED_MEDIA = "trusted_media"
    DISCOVERY = "discovery"


class SourceSpec(BaseModel):
    role: SourceRole

    @model_validator(mode="before")
    @classmethod
    def default_source_role(cls, values: object) -> object:
        if not isinstance(values, Mapping):
            return values
        normalized = dict(values)
        normalized.setdefault(
            "role",
            "primary" if normalized.get("official", True) else "trusted_media",
        )
        return normalized


class RawItem(BaseModel):
    source_role: SourceRole | None = None
    discovery_verified: bool = False
```

Extend the existing locator after-validator to require official sources to be primary and `reddit_rss` sources to be discovery.

Normalize a missing `RawItem.source_role` from `is_official_source` for legacy fixtures and direct constructors, using the same primary/trusted-media rule as `SourceSpec`; the validated field must never remain `None`. Every collector must still set `source_role=source.role` explicitly. Only the Reddit collector may set `discovery_verified=True`, and only after an external URL is HTTPS, belongs to a configured primary/trusted-media host, and its article body was fetched successfully. Product Hunt remains unverified discovery. Replace the Reddit trusted-host constant with the enabled config hosts passed from `_run_with_client`: hosts from sources whose role is `PRIMARY` or `TRUSTED_MEDIA`. Keep HTTPS and host allowlist validation in the collector.

- [ ] **Step 4: Add the exact source configuration**

Register the four feeds as `official: false`, `role: trusted_media`, weight 7, using the neutral `AI 工具` category hint for all four; per-item analysis determines whether a story belongs to Agent or another channel. Set Product Hunt and Reddit to `role: discovery`. Do not enable arXiv.

- [ ] **Step 5: Re-run focused tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_config.py tests/test_arxiv_collector.py tests/test_feed_collector.py tests/test_github_collector.py tests/test_official_page_base.py tests/test_reddit_rss_collector.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the source-role slice**

```bash
git add src/ai_news/models.py src/ai_news/collectors src/ai_news/pipeline/run.py sources/feeds.yaml tests/test_config.py tests/test_feed_collector.py tests/test_reddit_rss_collector.py
git commit -m "feat: classify and expand AI news sources"
```

### Task 2: Carry per-source stage diagnostics through the pipeline

**Files:**
- Modify: `src/ai_news/models.py`
- Modify: `src/ai_news/pipeline/run.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_run_records.py`
- Test: `tests/test_pipeline_run.py`

- [ ] **Step 1: Write failing diagnostic compatibility tests**

Add tests that load an old `SourceRun` with only existing fields and receive `None` for new fields. Add a pipeline test with two sources proving exact values for `recent_count`, `new_count`, `eligible_count`, `candidate_count`, `selected_count`, and `latest_published_at`.

Use this expected shape:

```python
assert source_run.model_dump(mode="json") == {
    "source_id": "official",
    "source_name": "Official",
    "success": True,
    "item_count": 4,
    "elapsed_ms": 10,
    "error_type": None,
    "recent_count": 3,
    "new_count": 2,
    "eligible_count": 1,
    "candidate_count": 1,
    "selected_count": 1,
    "latest_published_at": "2026-08-30T01:00:00Z",
}
```

- [ ] **Step 2: Run diagnostic tests and verify RED**

Run:

```bash
python -m pytest tests/test_storage.py::test_source_run_factories_enforce_safe_error_metadata tests/test_pipeline_run.py -k source_diagnostics -q
```

Expected: FAIL because the fields and stage accounting do not exist.

- [ ] **Step 3: Extend `SourceRun` compatibly**

Add optional non-negative integer fields and an aware datetime:

```python
item_count: int | None = Field(default=None, ge=0)
recent_count: int | None = Field(default=None, ge=0)
new_count: int | None = Field(default=None, ge=0)
eligible_count: int | None = Field(default=None, ge=0)
candidate_count: int | None = Field(default=None, ge=0)
selected_count: int | None = Field(default=None, ge=0)
latest_published_at: datetime | None = None
```

Validate `latest_published_at` when present. Add `with_diagnostics(**values) -> SourceRun` implemented with `model_copy(update=values)` followed by `SourceRun.model_validate(...)`; never mutate frozen instances.

Use `item_count=None` for request/parse failure and `item_count=0` for a successful empty response. This distinction is part of the public diagnostics contract.

- [ ] **Step 4: Introduce one immutable pipeline-preparation result**

In `pipeline/run.py`, add:

```python
@dataclass(frozen=True)
class _PreparedCandidates:
    selected: list[Candidate]
    latest_by_source: dict[str, datetime]
    recent_counts: Counter[str]
    new_counts: Counter[str]
    eligible_counts: Counter[str]
    candidate_counts: Counter[str]
```

Refactor normalization and preparation to populate this result. Preserve current exact dedupe behavior and bounds. After analysis, compute `selected_counts` from candidate source IDs and replace each `SourceRun` with a validated diagnostic copy.

- [ ] **Step 5: Verify diagnostics without changing publication semantics**

Run:

```bash
python -m pytest tests/test_pipeline_run.py tests/test_storage.py -q
```

Expected: PASS, including existing 120-hour, dedupe, bounded-state, and transaction tests.

- [ ] **Step 6: Commit diagnostics**

```bash
git add src/ai_news/models.py src/ai_news/pipeline/run.py tests/test_storage.py tests/test_pipeline_run.py
git commit -m "feat: record source contribution diagnostics"
```

### Task 3: Fix editorial false positives and academic leakage

**Files:**
- Modify: `src/ai_news/pipeline/editorial.py`
- Test: `tests/test_editorial_filter.py`

- [ ] **Step 1: Write the content and provenance regressions first**

Add positive cases whose source category is research/open-source but content is a product release:

```python
("Gemini Omni 1.1 Flash is now available", "New product availability and lower latency for users")
('Gemini 3.5 Transcribe launches for production', 'Production speech service with pricing and availability')
```

Add negative English academic cases:

```python
("Randomized controlled study of ChatGPT students", "We evaluate a treatment group and report a paper")
('A new preprint on alignment behavior', 'Research evaluation without product availability')
```

Add negative sponsored-content cases such as `Presented by`, `Partner Content`, and `Sponsored` even when the source is trusted media.

Add provenance cases proving unverified discovery is rejected before model selection while a verified discovery item with substantive content can proceed to content rules.

- [ ] **Step 2: Verify the regressions fail**

Run:

```bash
python -m pytest tests/test_editorial_filter.py -q
```

Expected: FAIL because category text causes false rejection and English academic markers are incomplete.

- [ ] **Step 3: Restrict content matching to actual content**

Before keyword matching, reject `source_role=discovery` unless `discovery_verified=True`; this keeps Product Hunt and unresolved Reddit posts out of the LLM candidate pool. Build the content filter text from only `raw.title` and `raw.excerpt`. Extend academic markers with bounded English forms for `paper`, `preprint`, `randomized controlled`, `study`, `research evaluation`, and `alignment behavior`. Reject explicit sponsored/partner markers. Keep product availability, price, production deployment, policy, security, and concrete adoption exceptions explicit.

- [ ] **Step 4: Verify the complete editorial matrix**

Run:

```bash
python -m pytest tests/test_editorial_filter.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the filter correction**

```bash
git add src/ai_news/pipeline/editorial.py tests/test_editorial_filter.py
git commit -m "fix: separate content quality from source categories"
```

### Task 4: Make ranking and LLM selection quality-first

**Files:**
- Create: `src/ai_news/editorial_policy.py`
- Modify: `src/ai_news/models.py`
- Modify: `src/ai_news/pipeline/dedupe.py`
- Modify: `src/ai_news/pipeline/rank.py`
- Modify: `src/ai_news/pipeline/run.py`
- Modify: `src/ai_news/llm/prompt.py`
- Modify: `src/ai_news/llm/base.py`
- Test: `tests/test_rank.py`
- Test: `tests/test_dedupe.py`
- Test: `tests/test_llm_prompt.py`
- Test: `tests/test_openai_chat_client.py`
- Test: `tests/test_anthropic_client.py`
- Test: `tests/test_pipeline_run.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_e2e_demo.py`

- [ ] **Step 1: Write failing quality and selection-contract tests**

Cover these invariants:

```text
primary complete recent item > trusted-media complete recent item > discovery marketing snippet
72–96h item > 96–120h item > older item
the prompt says target 3–7, maximum 7, contains no mandatory minimum of 5, and permits an empty selection when every candidate is unsuitable
one final source contributes at most 2 items
unverified discovery never becomes a NewsItem
any non-primary item with marketing_risk=high never becomes a NewsItem
NewsItem.is_official equals RawItem.is_official_source even if the model disagrees
cross-source duplicates prefer primary, then trusted media, then verified discovery
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
python -m pytest tests/test_rank.py tests/test_llm_prompt.py tests/test_openai_chat_client.py tests/test_anthropic_client.py tests/test_pipeline_run.py -q
```

Expected: FAIL on the old weight-dominated score, 5–9 contract, three-item source cap, and model-owned official flag.

- [ ] **Step 3: Implement the deterministic score**

Create shared policy constants so prompt, parser, and pipeline validation cannot drift:

```python
MIN_MODEL_SELECTION = 0
MIN_PUBLISHED_ITEMS = 1
MAX_REPORT_ITEMS = 7
MAX_ITEMS_PER_SOURCE = 2
```

Use one documented formula:

```python
role_bonus = {
    SourceRole.PRIMARY: 14,
    SourceRole.TRUSTED_MEDIA: 8,
    SourceRole.DISCOVERY: 0,
}[item.raw.source_role]
completeness = min(12, len(item.raw.excerpt.strip()) // 80)
return (
    item.raw.source_weight * 4
    + _recency_score(item, normalized_now)
    + role_bonus
    + (8 if item.raw.is_official_source else 0)
    + completeness
    + (6 if _has_keyword(item.raw.title) else 0)
)
```

Extend recency scores through 96 and 120 hours with scores 2 and 1. Keep deterministic tie breakers.

- [ ] **Step 4: Change the selection contract**

Set maximum publication count to 7 and final source cap to 2. Prompts must say “目标 3–7 条，质量不足时允许少于 3 条；全部不合格时返回空数组，不得凑数.” Parser validation allows 0–7 analyses and does not impose a positive minimum. The pipeline converts an empty/fully rejected result into `no_eligible_content` instead of a zero-item `DailyReport`. Unverified discovery has already been removed by deterministic eligibility. In `_build_items`, reject every remaining non-primary item whose marketing risk is high. Build `NewsItem.is_official` from the raw source fact, not `analysis.is_official`.

Update both dedupe winner selection and the exact-URL accumulator preference key so source role wins before numeric source weight. Add an event key based on normalized product/entity/version/action terms; tests must prove an official product launch replaces its media/discovery duplicate without merging unrelated products.

- [ ] **Step 5: Verify all ranking, client, and pipeline tests**

Run:

```bash
python -m pytest tests/test_rank.py tests/test_dedupe.py tests/test_llm_prompt.py tests/test_openai_chat_client.py tests/test_anthropic_client.py tests/test_pipeline_run.py tests/test_cli.py tests/test_e2e_demo.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit quality-first selection**

```bash
git add src/ai_news/editorial_policy.py src/ai_news/pipeline src/ai_news/llm src/ai_news/models.py tests/test_rank.py tests/test_dedupe.py tests/test_llm_prompt.py tests/test_openai_chat_client.py tests/test_anthropic_client.py tests/test_pipeline_run.py tests/test_cli.py tests/test_e2e_demo.py
git commit -m "feat: enforce quality-first daily selection"
```

### Task 5: Persist safe run records for published, empty, and failed runs

**Files:**
- Modify: `src/ai_news/models.py`
- Create: `src/ai_news/storage/run_records.py`
- Modify: `src/ai_news/storage/__init__.py`
- Modify: `src/ai_news/pipeline/run.py`
- Modify: `src/ai_news/cli.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_pipeline_run.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing run-record tests**

Define the public contract in tests:

```python
record = RunRecord(
    date=date(2026, 8, 30),
    cutoff_at=datetime(2026, 8, 30, 15, 59, 59, tzinfo=UTC),
    status=RunStatus.NO_ELIGIBLE_CONTENT,
    source_runs=[source_run],
    safe_error_code="no_eligible_candidates",
)
save_run_record(root, record)
assert load_run_records(root) == [record]
```

Assert the path is `data/runs/2026/08/2026-08-30.json`, writes are atomic, symlink escapes are rejected, and unsafe error strings cannot be stored. Pipeline tests must prove published and no-eligible statuses; analysis failure after collection must write `failed_before_publish` without a report.

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_storage.py -k run_record tests/test_pipeline_run.py -k run_record tests/test_cli.py -k historical -q
```

Expected: FAIL because the model and storage API do not exist.

- [ ] **Step 3: Add the safe model and focused storage module**

Use these exact status values:

```python
class RunStatus(StrEnum):
    PUBLISHED = "published"
    NO_ELIGIBLE_CONTENT = "no_eligible_content"
    FAILED_BEFORE_PUBLISH = "failed_before_publish"


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    date: date
    cutoff_at: datetime
    status: RunStatus
    source_runs: list[SourceRun]
    safe_error_code: Literal[
        "none",
        "no_healthy_sources",
        "no_eligible_candidates",
        "configuration_failed",
        "collection_failed",
        "candidate_preparation_failed",
        "analysis_failed",
        "report_validation_failed",
        "persistence_failed",
    ] = "none"
```

`run_records.py` must resolve the repository boundary, reject symlink leaves/parents, write a same-directory mode-0600 temporary file, `fsync`, then `os.replace`. Loading only accepts canonical `data/runs/YYYY/MM/YYYY-MM-DD.json` paths and verifies the path date. Keep this outside the existing three-target report transaction so old recovery journals remain compatible.

- [ ] **Step 4: Wire records into pipeline outcomes**

Add a `RunRecordSaver` dependency defaulting to `save_run_record`. After collection, persist one record for each terminal outcome. Save a `published` record only after `save_report` succeeds. For `PublicationThresholdError`, write `no_eligible_content` first and keep the CLI exit code 0. For safe pipeline failures after collection, write `failed_before_publish` and then raise the existing safe exception.

In `cli._generate`, compute the effective date and cutoff before loading configuration. If source or model configuration fails, write a `failed_before_publish` record with empty `source_runs` and `configuration_failed`, then emit only the existing safe error category. The Daily workflow must call this unified generate path instead of terminating in a separate `check-config` preflight.

Keep the already implemented `_historical_cutoff`: past dates use Shanghai end-of-day converted to UTC; current dates use the actual clock. Add regression assertions for `23:59:59.999999 Asia/Shanghai`.

- [ ] **Step 5: Verify storage, pipeline, and CLI behavior**

Run:

```bash
python -m pytest tests/test_storage.py tests/test_run_records.py tests/test_pipeline_run.py tests/test_cli.py -q
```

Expected: PASS; no existing report transaction test regresses.

- [ ] **Step 6: Commit run records**

```bash
git add src/ai_news/models.py src/ai_news/storage src/ai_news/pipeline/run.py src/ai_news/cli.py tests/test_storage.py tests/test_run_records.py tests/test_pipeline_run.py tests/test_cli.py
git commit -m "feat: persist safe daily run outcomes"
```

### Task 6: Make source health freshness-aware

**Files:**
- Modify: `src/ai_news/source_health.py`
- Modify: `scripts/check_source_health.py`
- Test: `tests/test_source_health.py`

- [ ] **Step 1: Write failing health-state tests**

Cover four states: `fresh`, `stale`, `empty`, `failed`. Use a 90-day threshold for primary sources, 7 days for trusted media, and advisory-only handling for discovery. Assert enabled primary/trusted-media sources are all checked; stale is visible but does not masquerade as fresh; empty/failed required sources fail the gate.

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_source_health.py -q
```

Expected: FAIL because health is currently a single boolean and only a subset is required.

- [ ] **Step 3: Implement explicit health status**

Add:

```python
class SourceHealthStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    EMPTY = "empty"
    FAILED = "failed"
```

Health output must contain only source ID/name/role/status/item count/latest date/safe error type. `healthy` means request and non-empty parse; `fresh` is a separate count. Discovery failure remains non-blocking, but it is never counted as fresh contribution.

- [ ] **Step 4: Verify the safe CLI output and gate**

Run:

```bash
python -m pytest tests/test_source_health.py -q
python scripts/check_source_health.py
```

Expected: tests PASS; live command exits 0 only when required sources are reachable and non-empty, while stale sources are reported distinctly.

- [ ] **Step 5: Commit source health logic**

```bash
git add src/ai_news/source_health.py scripts/check_source_health.py tests/test_source_health.py
git commit -m "feat: distinguish fresh and reachable sources"
```

### Task 7: Run the pipeline quality gate

**Files:**
- No new files.

- [ ] **Step 1: Run formatting and static checks**

```bash
python -m ruff format .
python -m ruff check .
```

Expected: PASS.

- [ ] **Step 2: Run the full Python suite with coverage**

```bash
python -m pytest --cov=ai_news --cov-report=term-missing --cov-fail-under=85
```

Expected: PASS with coverage at least 85%.

- [ ] **Step 3: Run the secret scan**

```bash
python scripts/check_no_secrets.py
```

Expected: PASS and no credential value printed.

- [ ] **Step 4: Commit only formatter changes, if any**

```bash
git add src tests scripts sources
git diff --cached --quiet || git commit -m "style: format pipeline V2 changes"
```
