# Consumer-Focused Editorial Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the daily AI news pipeline publish only concise, ordinary-user/business-relevant news and render it cleanly across desktop, tablet, and mobile.

**Architecture:** Keep the existing collect/dedupe/LLM/static Pages pipeline. Add a deterministic editorial eligibility filter before the existing 36-item candidate pool, change the analysis contract to a compact schema 1.2 with three user-facing lanes, permit 1–9 quality-selected items (normally 5–9), and update templates/CSS without introducing a runtime backend.

**Tech Stack:** Python 3.12, Pydantic, pytest, Jinja2, static CSS/HTML, GitHub Actions, GitHub Pages.

---

### Task 1: Add failing tests for editorial eligibility and relaxed quantity

**Files:**
- Create: `tests/test_editorial_filter.py`
- Modify: `tests/test_pipeline_run.py`

- [ ] **Step 1: Write failing tests**

Add candidate fixtures and assert `is_editorially_eligible` rejects paper/SDK/framework/benchmark/minor-release/pure-financing text and accepts a product launch, price change, user feature, enterprise adoption, and policy change. Add a pipeline test with four valid analyses asserting a report is returned with four items, while an invalid zero-item report still raises `PublicationThresholdError` only when no item can be published.

- [ ] **Step 2: Run the focused tests**

Run `pytest tests/test_editorial_filter.py tests/test_pipeline_run.py -q`; expected new tests fail because the filter and quantity behavior do not exist.

- [ ] **Step 3: Commit the test-only red state**

Run `git add tests/test_editorial_filter.py tests/test_pipeline_run.py && git commit -m "test: define consumer editorial eligibility"`.

### Task 2: Implement deterministic editorial filtering and candidate preparation

**Files:**
- Create: `src/ai_news/pipeline/editorial.py`
- Modify: `src/ai_news/pipeline/run.py`
- Modify: `src/ai_news/collectors/arxiv.py`
- Modify: `sources/feeds.yaml`
- Test: `tests/test_editorial_filter.py`

- [ ] **Step 1: Implement `is_editorially_eligible(candidate)`**

Normalize title, source, excerpt, and category hint into one string. Reject explicit academic/research, SDK/API, framework, code/repository, benchmark, model-training, and patch/minor-version markers. Reject financing/valuation/personnel markers unless the same text contains product, price, access, user, policy, regulation, market, or adoption impact markers. Accept only product/application, business/market, or platform/policy signals. Keep the function deterministic and side-effect free.

- [ ] **Step 2: Apply the filter before the 36-item pool**

In `_prepare_candidates`, call `deduplicate`, filter eligible candidates, then call `select_balanced_candidates`. Preserve source diversity and the existing 36/6 limits.

- [ ] **Step 3: Remove direct academic inputs**

Disable the arXiv source in `sources/feeds.yaml` and retain the collector only for backward-compatible tests. Mark technical release feeds as disabled when their source is exclusively SDK/framework detail; do not remove collector code.

- [ ] **Step 4: Run focused tests**

Run `pytest tests/test_editorial_filter.py tests/test_rank.py tests/test_pipeline_run.py -q`; expected PASS for new eligibility behavior and existing ranking behavior.

- [ ] **Step 5: Commit**

Run `git add src/ai_news/pipeline/editorial.py src/ai_news/pipeline/run.py src/ai_news/collectors/arxiv.py sources/feeds.yaml tests/test_editorial_filter.py tests/test_pipeline_run.py && git commit -m "feat: filter technical and academic news"`.

### Task 3: Change the LLM contract to schema 1.2 and validate 1–9 items

**Files:**
- Modify: `src/ai_news/models.py`
- Modify: `src/ai_news/llm/prompt.py`
- Modify: `src/ai_news/pipeline/run.py`
- Modify: `src/ai_news/storage/repository.py`
- Modify: `tests/test_llm_prompt.py`
- Modify: `tests/test_pipeline_run.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Add failing contract assertions**

Assert prompts describe only the three new lane values, compact title/summary/impact limits, and rejection of technical/academic material. Assert `DailyReport` accepts `schema_version="1.2"`, still reads 1.0/1.1, and pipeline validation accepts 1–9 items but rejects empty output and per-source counts above three.

- [ ] **Step 2: Implement model compatibility**

Add `EditorialLane.PRODUCT_APPLICATION`, `BUSINESS_MARKET`, and `PLATFORM_POLICY`; map legacy lanes when reading 1.0/1.1. Change `DailyReport.schema_version` to `Literal["1.0", "1.1", "1.2"]` with default `1.2`; keep old report parsing compatible. Tighten new `Analysis` text limits while accepting legacy fields for historical files.

- [ ] **Step 3: Update prompt**

Replace the three lane values and remove the old research/engineering preference language. Instruct the model to choose `min(9, len(items))`, reject technical/academic candidates, and return only concise `summary` and `why_it_matters`; retain ID binding and JSON-only safeguards.

- [ ] **Step 4: Update pipeline validation**

Set publication target to 9 maximum and minimum to 1. Skip the old “all three lanes and exactly nine” check; validate unique IDs, allowed lanes, compact fields, and max three items per source. If `_build_items` returns no valid item, raise `PublicationThresholdError`; otherwise build the report.

- [ ] **Step 5: Run contract tests**

Run `pytest tests/test_llm_prompt.py tests/test_pipeline_run.py tests/test_storage.py -q`; expected PASS.

- [ ] **Step 6: Commit**

Run `git add src/ai_news/models.py src/ai_news/llm/prompt.py src/ai_news/pipeline/run.py src/ai_news/storage/repository.py tests/test_llm_prompt.py tests/test_pipeline_run.py tests/test_storage.py && git commit -m "feat: adopt consumer editorial schema"`.

### Task 4: Simplify templates and add responsive layout

**Files:**
- Modify: `src/ai_news/site/templates/index.html`
- Modify: `src/ai_news/site/templates/day.html`
- Modify: `src/ai_news/site/templates/_card.html`
- Modify: `src/ai_news/site/templates/base.html`
- Modify: `src/ai_news/site/static/styles.css`
- Modify: `tests/test_site_builder.py`
- Modify: `tests/test_site_accessibility.py`

- [ ] **Step 1: Add failing rendering assertions**

Assert generated homepage has no duplicate priority section, no technical tracking block, contains only two editorial text blocks per card, and CSS includes media queries for 390px and tablet layouts with no horizontal overflow.

- [ ] **Step 2: Implement compact card markup**

Render lane, source/time, title, `summary`, `why_it_matters`, and source link. Remove rank emphasis, tags, `tracking_signal`, and duplicate sections. Render lane groups from actual items only.

- [ ] **Step 3: Implement responsive CSS**

Use a capped reading width, compact spacing, grid-to-single-column breakpoints at tablet and 600px, `overflow-wrap:anywhere` for long text/URLs, minimum 44px touch targets, and `@media (max-width: 600px)` rules for typography and padding.

- [ ] **Step 4: Run site tests**

Run `pytest tests/test_site_builder.py tests/test_site_accessibility.py -q`; expected PASS, including internal links and responsive CSS assertions.

- [ ] **Step 5: Commit**

Run `git add src/ai_news/site/templates src/ai_news/site/static/styles.css tests/test_site_builder.py tests/test_site_accessibility.py && git commit -m "feat: simplify responsive news layout"`.

### Task 5: Full verification, real generation, and Pages acceptance

**Files:**
- Modify only generated `content/` and `data/` files if the real run succeeds.

- [ ] **Step 1: Run the complete local gate**

Run `pytest -q`, `ruff check .`, `python scripts/check_no_secrets.py`, and `python scripts/validate_site.py`; all must exit 0 before publishing.

- [ ] **Step 2: Build static output locally**

Run the repository’s site build command and inspect generated HTML at 390px, 768px, and desktop widths; verify no horizontal scroll and every source link is preserved.

- [ ] **Step 3: Run the real daily workflow**

Dispatch `daily-news.yml` with the existing GitHub Secret `LLM_API_KEY`; monitor the run until completion. Never print or persist the secret.

- [ ] **Step 4: Verify deployed Pages**

Open `https://mount-su.github.io/ai-news/`, inspect the latest date page and archive, and confirm actual item count, consumer-only content, compact cards, source links, and responsive behavior.

- [ ] **Step 5: Commit generated output only after verification**

Stage only generated `content/` and `data/` files, then commit with `chore: publish consumer-focused daily report` if the workflow updates them.

