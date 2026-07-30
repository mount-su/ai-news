# Concise Editorial Daily Brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 20-item per-candidate digest with a validated nine-item LLM-edited daily brief and a compact, non-duplicative site.

**Architecture:** Keep collection, normalization, history exclusion, and deterministic deduplication unchanged. Add a source-balanced 36-item candidate pool, ask the LLM to select and rewrite at most nine items in one editorial request, and validate exact count, editorial-lane distribution, source caps, IDs, lengths, and secrets before publication. Add backward-compatible report schema 1.1 support and render one compact nine-card list without duplicate priority or tracking sections.

**Tech Stack:** Python 3.12, Pydantic 2, httpx, Jinja2, vanilla CSS/JavaScript, pytest, Ruff, GitHub Actions, GitHub Pages.

---

## File map

- `src/ai_news/models.py`: define editorial lanes, compact analysis limits, schema 1.1, and legacy 1.0 lane inference.
- `src/ai_news/pipeline/rank.py`: build a deterministic source-balanced candidate pool.
- `src/ai_news/llm/prompt.py`: ask one LLM editor to select and compress the final items.
- `src/ai_news/llm/base.py`: parse a selected subset, validate editorial constraints, and perform one directed repair.
- `src/ai_news/pipeline/run.py`: require nine eligible candidates and nine valid selected results before publication.
- `src/ai_news/cli.py`: make the offline demonstration a nine-item schema 1.1 report.
- `src/ai_news/site/builder.py`: expose compact health and ranking context without duplicate lists.
- `src/ai_news/site/templates/base.html`: replace the four-cell collection dashboard with one compact health line.
- `src/ai_news/site/templates/index.html`: render a single nine-item daily list.
- `src/ai_news/site/templates/day.html`: use the same concise daily presentation for archived dates.
- `src/ai_news/site/templates/_card.html`: render only metadata plus change, impact, action, and source link.
- `src/ai_news/site/static/styles.css`: reduce the hero height and style one responsive ranked list.
- `src/ai_news/site/static/app.js`: remove homepage search/filter behavior that no longer has controls.
- `tests/`: lock every new selection, compatibility, rendering, and security invariant before implementation.
- `tests/fixtures/demo-report-input.json`: define nine deterministic demo titles.
- `README.md`: document the nine-item editorial contract and failure behavior.

### Task 1: Add schema 1.1 and editorial lanes

**Files:**
- Modify: `src/ai_news/models.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_pipeline_run.py`

- [ ] **Step 1: Write failing model and storage compatibility tests**

Add tests that construct a new item with an explicit lane, parse an existing schema 1.0 payload without the field, and verify that new reports serialize as 1.1:

```python
def test_news_item_infers_legacy_editorial_lane_from_category() -> None:
    payload = _news_item_payload()
    payload.pop("editorial_lane", None)
    payload["category"] = "论文研究"
    item = NewsItem.model_validate(payload)
    assert item.editorial_lane is EditorialLane.RESEARCH


def test_new_daily_report_serializes_schema_1_1() -> None:
    report = _report()
    payload = report.model_dump(mode="json")
    assert payload["schema_version"] == "1.1"
    assert payload["items"][0]["editorial_lane"] == "产品工程"


def test_schema_1_0_report_remains_loadable() -> None:
    payload = _report().model_dump(mode="json")
    payload["schema_version"] = "1.0"
    for item in payload["items"]:
        item.pop("editorial_lane")
    loaded = DailyReport.model_validate(payload)
    assert loaded.schema_version == "1.0"
    assert loaded.items[0].editorial_lane is EditorialLane.PRODUCT_ENGINEERING
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_storage.py tests/test_pipeline_run.py -q
```

Expected: failures because `EditorialLane`, `editorial_lane`, and schema 1.1 do not exist.

- [ ] **Step 3: Implement editorial lanes and compatibility**

Add:

```python
class EditorialLane(StrEnum):
    PRODUCT_ENGINEERING = "产品工程"
    BUSINESS = "商业趋势"
    RESEARCH = "前沿研究"


def editorial_lane_for_category(category: Category) -> EditorialLane:
    if category is Category.RESEARCH:
        return EditorialLane.RESEARCH
    return EditorialLane.PRODUCT_ENGINEERING
```

Add `editorial_lane` to `Analysis` and `NewsItem`. Use a `model_validator(mode="before")` on both models to infer the field only when old input omits it. Change the `DailyReport.schema_version` declaration to:

```python
schema_version: Literal["1.0", "1.1"] = "1.1"
```

Keep the broad historical `NewsItem` maximum lengths so old reports remain readable; strict new-output limits belong in the LLM response model.

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/pytest tests/test_storage.py tests/test_pipeline_run.py -q
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ai_news/models.py tests/test_storage.py tests/test_pipeline_run.py
git commit -m "feat: add editorial lanes and report schema 1.1"
```

### Task 2: Build a source-balanced candidate pool

**Files:**
- Modify: `src/ai_news/pipeline/rank.py`
- Modify: `src/ai_news/pipeline/run.py`
- Test: `tests/test_rank.py`
- Test: `tests/test_pipeline_run.py`

- [ ] **Step 1: Write failing balanced-selection tests**

Add:

```python
def test_select_balanced_candidates_caps_each_source_and_total() -> None:
    dominant = [_candidate(f"a-{index}", "Launch", source_id="source-a") for index in range(20)]
    diverse = [
        _candidate(f"{source}-{index}", "Release", source_id=f"source-{source}")
        for source in ("b", "c", "d", "e", "f")
        for index in range(8)
    ]
    selected = select_balanced_candidates(dominant + diverse, NOW, limit=36, per_source=6)
    assert len(selected) == 36
    assert Counter(item.raw.source_id for item in selected).most_common(1)[0][1] == 6
    assert selected == select_balanced_candidates(
        list(reversed(dominant + diverse)), NOW, limit=36, per_source=6
    )


def test_pipeline_sends_at_most_thirty_six_candidates_and_six_per_source() -> None:
    sources = [_source(source_index) for source_index in range(6)]
    items_by_source = {
        source.id: [
            _raw(source_index * 100 + item_index, source=source)
            for item_index in range(10)
        ]
        for source_index, source in enumerate(sources)
    }
    analyzer = _Analyzer()
    _run(
        source_config=SourceConfig(sources=sources),
        collector=_collector(items_by_source),
        analyzer=analyzer,
    )
    assert len(analyzer.calls[0]) == 36
    assert max(Counter(item.raw.source_id for item in analyzer.calls[0]).values()) == 6
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_rank.py tests/test_pipeline_run.py -q
```

Expected: failures because `select_balanced_candidates` is missing and the pipeline still sends 30 globally ranked candidates.

- [ ] **Step 3: Implement deterministic balanced selection**

In `rank.py`, sort once with the existing score/time/ID key, then accept candidates while the source count is below six:

```python
def select_balanced_candidates(
    items: list[Candidate],
    now: datetime,
    *,
    limit: int = 36,
    per_source: int = 6,
) -> list[Candidate]:
    if limit < 0 or per_source <= 0:
        raise ValueError("selection limits must be positive")
    counts: Counter[str] = Counter()
    selected: list[Candidate] = []
    for item in _ranked(items, now):
        source_id = item.raw.source_id
        if counts[source_id] >= per_source:
            continue
        counts[source_id] += 1
        selected.append(item)
        if len(selected) == limit:
            break
    return selected
```

Extract the existing deterministic sort into `_ranked` so `select_candidates` and the balanced selector share exactly one ordering definition. In `run.py`, replace `_ANALYSIS_LIMIT = 30` with `_EDITORIAL_CANDIDATE_LIMIT = 36`, `_EDITORIAL_SOURCE_LIMIT = 6`, and call the balanced selector after semantic deduplication.

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/pytest tests/test_rank.py tests/test_pipeline_run.py -q
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ai_news/pipeline/rank.py src/ai_news/pipeline/run.py tests/test_rank.py tests/test_pipeline_run.py
git commit -m "feat: balance editorial candidates by source"
```

### Task 3: Replace per-candidate analysis with one editorial selection

**Files:**
- Modify: `src/ai_news/llm/prompt.py`
- Modify: `src/ai_news/llm/base.py`
- Modify: `src/ai_news/llm/openai_chat.py`
- Modify: `src/ai_news/llm/anthropic.py`
- Test: `tests/test_llm_prompt.py`
- Test: `tests/test_openai_chat_client.py`
- Test: `tests/test_anthropic_client.py`

- [ ] **Step 1: Write failing prompt and parser tests**

Add prompt assertions for one editorial request, the three lanes, exact selection count, source-aware constraints, compact lengths, and anti-generic guidance:

```python
assert "从全部候选中选择恰好 9 条" in prompt
assert "同一事件只保留一条" in prompt
assert "单一来源最多 3 条" in prompt
assert "具有重要意义" in prompt
for lane in EditorialLane:
    assert lane.value in prompt
```

Add response tests for:

- 9 valid selected rows from 12–36 candidates.
- fewer or more than 9 rows.
- duplicate and unknown IDs.
- a lane with 0 or 5 rows.
- a source with 4 selected rows.
- title over 80, summary over 160, impact over 100, action over 80, and more than 3 tags.
- a first invalid response followed by one valid repair.
- two invalid responses producing `invalid_after_repair`.
- one request rather than batches when 30 candidates are supplied.

Use nine-candidate fixtures spanning at least three source IDs and all three editorial lanes.

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_llm_prompt.py tests/test_openai_chat_client.py tests/test_anthropic_client.py -q
```

Expected: failures because the prompt requires one row per candidate, the parser rejects subsets, and analysis is batched.

- [ ] **Step 3: Implement the compact editorial prompt**

Change the response schema to require:

```python
_ROW_SCHEMA = (
    '{"id":"候选ID","title":"1-80字中文短标题",'
    '"editorial_lane":"产品工程|商业趋势|前沿研究",'
    '"category":"六类之一","summary":"20-160字变化",'
    '"importance":1-10,"why_it_matters":"10-100字影响",'
    '"tags":["1-3个标签"],"is_official":true,'
    '"marketing_risk":"low|medium|high",'
    '"tracking_signal":"5-80字行动"}'
)
```

Pass `selection_count=min(9, len(items))` to `build_analysis_prompt`. Include `source_id` in candidate JSON solely for selection constraints, keep URLs and credentials excluded, and reduce excerpts from 2,000 to 1,200 characters so 36 candidates stay within a conservative request bound.

- [ ] **Step 4: Implement selected-subset parsing and constraint errors**

Extend `_AnalysisRow` with `editorial_lane` and the strict compact limits. Change `_parse_analysis` to receive the candidate list, accept only selected IDs, require `min(9, len(candidates))` unique rows, and return a precise safe error kind from:

```python
json, schema, count, duplicate, unknown, lane_distribution, source_distribution
```

For a nine-row result:

```python
lane_counts = Counter(row.editorial_lane for row in rows)
if set(lane_counts) != set(EditorialLane) or any(count > 4 for count in lane_counts.values()):
    return None, "lane_distribution"
source_counts = Counter(candidate_by_id[row.id].raw.source_id for row in rows)
if any(count > 3 for count in source_counts.values()):
    return None, "source_distribution"
```

Remove `_BATCH_SIZE` and `_analyze_batch`. `analyze()` must make one request over the complete balanced candidate pool, followed by at most one repair request. The repair prompt must state the exact safe error kind and repeat all editorial constraints.

- [ ] **Step 5: Preserve both provider request contracts**

Do not change endpoint construction, authentication isolation, retry policy, 120-second request timeout, redirect rejection, sensitive-output checks, or provider response extraction. Increase `max_tokens` only if the existing 6,000-token cap fails the nine-row contract tests; otherwise leave it unchanged.

- [ ] **Step 6: Run focused tests**

Run:

```bash
.venv/bin/pytest tests/test_llm_prompt.py tests/test_openai_chat_client.py tests/test_anthropic_client.py tests/test_llm_factory.py -q
```

Expected: all focused tests pass and 30 candidates result in one initial HTTP request.

- [ ] **Step 7: Commit**

```bash
git add src/ai_news/llm tests/test_llm_prompt.py tests/test_openai_chat_client.py tests/test_anthropic_client.py
git commit -m "feat: make the LLM select a concise editorial brief"
```

### Task 4: Enforce exactly nine publication items in the pipeline

**Files:**
- Modify: `src/ai_news/pipeline/run.py`
- Test: `tests/test_pipeline_run.py`

- [ ] **Step 1: Write failing pipeline contract tests**

Add tests proving:

```python
assert len(report.items) == 9
assert 1 <= Counter(item.editorial_lane for item in report.items)["产品工程"] <= 4
assert max(Counter(item.source for item in report.items).values()) <= 3
```

Also add explicit failure tests for:

- fewer than nine eligible candidates before LLM invocation.
- analyzer output containing 8 or 10 entries.
- any missing lane or a lane with 5 entries.
- four selected candidates from one source.
- malformed analysis rows being skipped until fewer than nine remain.
- source failure being publishable only when all final constraints still pass.

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_pipeline_run.py -q
```

Expected: failures because the current threshold is five and output is truncated to twenty.

- [ ] **Step 3: Implement exact publication validation**

Replace publication constants with:

```python
_PUBLICATION_COUNT = 9
_MAX_FINAL_SOURCE_ITEMS = 3
_MAX_LANE_ITEMS = 4
```

Reject fewer than nine selected candidates before analyzer creation. Build only analyses returned by the editor, require exactly nine valid `NewsItem` objects, validate all three lanes and source IDs against the selected `Candidate` map, then sort by importance/time/ID without an additional truncation that could hide invalid output.

Keep safe public failures:

```python
raise PublicationThresholdError("fewer than nine valid items")
raise ReportValidationError("editorial distribution invalid")
```

Do not persist a report after either failure.

- [ ] **Step 4: Run focused tests**

Run:

```bash
.venv/bin/pytest tests/test_pipeline_run.py -q
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ai_news/pipeline/run.py tests/test_pipeline_run.py
git commit -m "feat: enforce the nine-item publication contract"
```

### Task 5: Update the offline demo and fixtures

**Files:**
- Modify: `src/ai_news/cli.py`
- Modify: `tests/fixtures/demo-report-input.json`
- Modify: `tests/test_e2e_demo.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing nine-item demo assertions**

Change the end-to-end assertion from a minimum to an exact count:

```python
assert len(report.items) == 9
assert report.schema_version == "1.1"
assert set(item.editorial_lane for item in report.items) == set(EditorialLane)
assert Counter(item.editorial_lane for item in report.items).most_common(1)[0][1] <= 4
```

Update the fixture to contain nine exact expected titles.

- [ ] **Step 2: Run demo tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_e2e_demo.py tests/test_cli.py -q
```

Expected: failures because the current demo emits too few schema 1.0-style items.

- [ ] **Step 3: Build a deterministic nine-item demo**

Create nine `NewsItem` entries in `_demo_report`, distribute lanes 4/3/2, use three source names with at most three items each, and keep all existing URLs and timestamps deterministic. The fixture titles must exactly match the generated ordering.

- [ ] **Step 4: Run demo tests**

Run:

```bash
.venv/bin/pytest tests/test_e2e_demo.py tests/test_cli.py -q
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/ai_news/cli.py tests/fixtures/demo-report-input.json tests/test_e2e_demo.py tests/test_cli.py
git commit -m "test: make the offline demo a nine-item brief"
```

### Task 6: Replace the duplicate dashboard with one concise ranked list

**Files:**
- Modify: `src/ai_news/site/builder.py`
- Modify: `src/ai_news/site/templates/base.html`
- Modify: `src/ai_news/site/templates/index.html`
- Modify: `src/ai_news/site/templates/day.html`
- Modify: `src/ai_news/site/templates/_card.html`
- Modify: `src/ai_news/site/static/styles.css`
- Modify: `src/ai_news/site/static/app.js`
- Test: `tests/test_site_builder.py`
- Test: `tests/test_site_accessibility.py`
- Test: `tests/test_validate_site.py`
- Test: `tests/test_e2e_demo.py`

- [ ] **Step 1: Write failing page-structure tests**

For a new nine-item report, assert:

```python
assert home.count('data-news-card') == 9
for item in report.items:
    assert home.count(f'id="item-{item.id}"') == 1
assert "今日最重要" not in home
assert "后续观察" not in home
assert 'type="search"' not in home
assert "营销风险" not in home
assert "原始标题" not in home
assert "重要性 " not in home
for label in ("变化", "影响", "行动"):
    assert label in home
```

Assert that the first three cards have a priority class, cards 4–9 do not, archive/category links remain, search JSON still contains nine entries, and old 1.0 fixture reports still render.

- [ ] **Step 2: Run site tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_site_builder.py tests/test_site_accessibility.py tests/test_validate_site.py tests/test_e2e_demo.py -q
```

Expected: failures because the current template duplicates top items and renders verbose fields, filters, and a tracking rail.

- [ ] **Step 3: Simplify builder context**

Remove `top_items` and `signals` from page contexts. Add:

```python
"healthy_source_count": sum(run.success for run in report.source_runs),
"ranked_items": [
    {"item": item, "rank": index, "priority": index <= 3}
    for index, item in enumerate(_sort_items(report.items), start=1)
],
```

Keep category navigation, archive generation, category pages, search JSON, base-path handling, atomic staging, and safety checks unchanged.

- [ ] **Step 4: Implement concise templates**

Render one list:

```jinja2
<ol class="brief-list" aria-label="本期九条情报">
  {% for entry in ranked_items %}
    <li class="brief-list__item{% if entry.priority %} brief-list__item--priority{% endif %}">
      {{ news_card(entry.item, entry.rank, entry.priority) }}
    </li>
  {% endfor %}
</ol>
```

The card macro must display only rank, lane, category, source, time, title, three labeled sentences, and the original link. Do not render the old details block, risk badge, official badge, original title, tag list, search data attributes, or duplicate GitHub link.

- [ ] **Step 5: Compact the visual hierarchy**

In CSS:

- cap the edition heading spacing so the first card is visible in a 1,000px-high desktop viewport.
- use a single-column reading measure of approximately 920px.
- give cards 1–3 a subtle stronger border/background instead of a separate grid.
- place “变化、影响、行动” in three compact rows on desktop and stacked rows at 390px.
- keep all controls and text within the viewport with no body-level horizontal scrolling.
- retain visible `:focus-visible`, reduced-motion, readable contrast, and minimum practical link hit targets.

Reduce `app.js` to harmless progressive enhancement only if another active control still needs it; otherwise leave a small no-op-free module or remove the script reference and update asset validation consistently.

- [ ] **Step 6: Run site and demo tests**

Run:

```bash
.venv/bin/pytest tests/test_site_builder.py tests/test_site_accessibility.py tests/test_validate_site.py tests/test_e2e_demo.py -q
.venv/bin/python -m ai_news demo --root .demo --output .demo/dist
.venv/bin/python scripts/validate_site.py .demo/dist --base-path /ai-news/
```

Expected: tests pass and site validation reports success.

- [ ] **Step 7: Commit**

```bash
git add src/ai_news/site src/ai_news/cli.py tests/test_site_builder.py tests/test_site_accessibility.py tests/test_validate_site.py tests/test_e2e_demo.py
git commit -m "feat: render one concise daily intelligence list"
```

### Task 7: Update documentation and run the complete local gate

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-07-31-concise-editorial.md`
- Test: `tests/test_readme_contract.py`
- Test: `.github/workflows/ci.yml`
- Test: `.github/workflows/daily-news.yml`

- [ ] **Step 1: Add failing README contract tests**

Require documentation to state:

```text
每期 9 条
产品工程
商业趋势
前沿研究
单一来源最多 3 条
生成失败时保留上一期
```

- [ ] **Step 2: Run the README tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_readme_contract.py -q
```

Expected: failures because the current README documents the 20-item analyzer flow.

- [ ] **Step 3: Update README**

Describe the balanced 36-candidate pool, one editorial LLM call plus one possible repair, the exact nine-item publication contract, dynamic 1–4 lane distribution, final source cap, compact fields, schema 1.1 compatibility, and fail-closed publication behavior. Do not include secrets or provider tokens.

- [ ] **Step 4: Run the complete local quality gate**

Run:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest --cov=ai_news --cov-report=term-missing --cov-fail-under=85
.venv/bin/python scripts/check_no_secrets.py
.venv/bin/python -m ai_news demo --root .demo --output .demo/dist
.venv/bin/python scripts/validate_site.py .demo/dist --base-path /ai-news/
docker run --rm \
  -v "$PWD:/repo" \
  -w /repo \
  rhysd/actionlint:1.7.7@sha256:887a259a5a534f3c4f36cb02dca341673c6089431057242cdc931e9f133147e9 \
  -ignore 'element of "schedule" section must be mapping and must contain one key "cron"' \
  .github/workflows/ci.yml .github/workflows/daily-news.yml
git diff --check
```

Expected: Ruff passes, every test passes, total coverage is at least 85%, secret scan is clean, demo and validation succeed, actionlint has no non-ignored findings, and `git diff --check` is clean.

- [ ] **Step 5: Commit documentation and plan progress**

Mark all completed plan checkboxes, then:

```bash
git add README.md tests/test_readme_contract.py docs/superpowers/plans/2026-07-31-concise-editorial.md
git commit -m "docs: explain the concise editorial contract"
```

### Task 8: Publish and verify production

**Files:**
- Verify: `.github/workflows/ci.yml`
- Verify: `.github/workflows/daily-news.yml`
- Verify: `data/YYYY/MM/YYYY-MM-DD.json`
- Verify: `content/YYYY-MM-DD.md`
- Verify: `https://mount-su.github.io/ai-news/`

- [ ] **Step 1: Reconcile with current remote main**

Run:

```bash
git fetch origin
git rebase origin/main
```

Expected: clean rebase; generated-data commits may move the branch forward without overlapping source changes.

- [ ] **Step 2: Push the implementation branch and inspect exact-sha CI**

Run:

```bash
git push -u origin codex/concise-editorial
gh run list --repo mount-su/ai-news --branch codex/concise-editorial --limit 10
```

If branch CI is not configured, fast-forward or merge the reviewed branch into `main`, push `main`, and inspect the CI run whose `headSha` exactly equals the implementation commit.

- [ ] **Step 3: Merge to main without overwriting generated data**

After final fetch/rebase and a clean verification:

```bash
git switch main
git pull --ff-only origin main
git merge --ff-only codex/concise-editorial
git push origin main
```

Expected: main points at the exact reviewed implementation ancestry.

- [ ] **Step 4: Trigger one real Daily AI news workflow**

Run:

```bash
gh workflow run daily-news.yml --repo mount-su/ai-news
gh run list --repo mount-su/ai-news --workflow daily-news.yml --event workflow_dispatch --limit 3
```

Watch the exact dispatched run until `generate`, `build`, and `deploy` are all successful.

- [ ] **Step 5: Audit the generated report and bot commit**

Verify:

```python
assert len(report["items"]) == 9
assert 1 <= min(Counter(item["editorial_lane"] for item in report["items"]).values())
assert max(Counter(item["editorial_lane"] for item in report["items"]).values()) <= 4
assert max(Counter(item["source"] for item in report["items"]).values()) <= 3
assert report["schema_version"] == "1.1"
```

Confirm the bot commit changes only the expected `data/` and `content/` files and contains no secret markers.

- [ ] **Step 6: Verify the live site in desktop and mobile browsers**

At `https://mount-su.github.io/ai-news/`, verify:

- exactly nine cards and nine unique item IDs.
- cards 1–3 are visually emphasized but not duplicated.
- every card shows 变化、影响、行动 and one source link.
- old verbose labels, search controls, duplicate priority region, and tracking rail are absent.
- the first card appears within a 1,000px-high desktop viewport.
- 1440px and 390px viewports have no body-level horizontal overflow.
- archive and one historical date page remain accessible.
- no console errors are emitted.

- [ ] **Step 7: Run the final security and state audit**

Confirm HTTPS Pages, clean repository/public artifact secret scans, local/remote main SHA parity, successful exact-sha CI, successful real Daily run, and a clean worktree. Only then mark the goal complete.
