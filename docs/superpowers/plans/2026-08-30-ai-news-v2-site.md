# AI News V2 Site, Search, and Editorial UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the generated site into a mature, responsive Chinese business-news daily with compact reading, explainable source status, useful discovery tools, and complete static-site metadata.

**Architecture:** Keep the existing atomic Python/Jinja static build. Add a pure presentation layer for channel mapping, issue metadata, pagination, links, and run-record views; keep `builder.py` focused on orchestration. Generate a bounded search index and use dependency-free JavaScript only on the search page. Generate RSS, sitemap, robots, and metadata at build time.

**Tech Stack:** Python 3.12, Jinja2, HTML5, modern CSS, dependency-free JavaScript, Node built-in test runner, pytest, GitHub Pages.

---

### Task 1: Define stable channels and pure presentation helpers

**Files:**
- Create: `src/ai_news/site/presentation.py`
- Modify: `src/ai_news/site/builder.py`
- Test: `tests/test_site_presentation.py`
- Test: `tests/test_site_builder.py`

- [ ] **Step 1: Write failing channel, fragment, pagination, and issue tests**

Lock these five channels in this order, even when a channel is empty:

```python
CHANNELS = (
    ChannelSpec("model", "大模型"),
    ChannelSpec("agent", "Agent"),
    ChannelSpec("ai-tools", "AI 产品"),
    ChannelSpec("open-source", "开源生态"),
    ChannelSpec("industry-policy", "行业政策"),
)
```

Test the mapping contract:

```text
Research -> excluded from aggregation and default search
PLATFORM_POLICY lane -> industry-policy before category mapping
MODEL -> model
AGENT and CODING_AGENT -> agent
TOOL -> ai-tools
OPEN_SOURCE -> open-source
```

Add tests for deterministic article fragments, continuous `01…N` issue numbering, 20-item pagination for 0/20/21/41 entries, and previous/next edition boundaries.

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_site_presentation.py tests/test_site_builder.py -q
```

Expected: FAIL because the presentation module and fixed channel contract do not exist.

- [ ] **Step 3: Implement pure presentation helpers**

Create frozen `ChannelSpec` values and pure helpers:

```python
def channel_for_item(item: NewsItem) -> ChannelSpec | None: ...
def item_fragment(item: NewsItem, report_date: date) -> str: ...
def item_day_url(item: NewsItem, report_date: date, base_path: str) -> str: ...
def issue_numbers(reports: Sequence[DailyReport]) -> dict[date, int]: ...
def paginate[T](values: Sequence[T], page_size: int = 20) -> list[list[T]]: ...
def edition_neighbors(reports, report_date): ...
def archive_entries(reports, run_records): ...
def source_statuses(source_specs, run_records): ...
```

Do not expand the historical `Category` enum just to create the policy channel. Preserve `/categories/ai-tools/` as the canonical URL for “AI 产品.” Old reports without V2 diagnostics must display “历史诊断不可用”; never infer contribution from `success`.

`source_statuses` receives the full enabled source configuration loaded through `load_source_config(root / "sources/feeds.yaml")`. For each source it sums `recent/new/eligible/candidate/selected` over the latest seven Shanghai calendar dates represented by run records, takes the maximum `latest_published_at`, and keeps configured-but-missing sources visible as “暂无运行数据.”

- [ ] **Step 4: Verify GREEN**

Run:

```bash
python -m pytest tests/test_site_presentation.py tests/test_site_builder.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the presentation contract**

```bash
git add src/ai_news/site/presentation.py src/ai_news/site/builder.py tests/test_site_presentation.py tests/test_site_builder.py
git commit -m "feat: define stable editorial presentation channels"
```

### Task 2: Build bounded pages, source views, and deep links

**Files:**
- Modify: `src/ai_news/site/builder.py`
- Modify: `src/ai_news/site/__init__.py`
- Test: `tests/test_site_builder.py`

- [ ] **Step 1: Write failing build-output tests**

Cover all of these outputs and invariants:

```text
all five channel first pages always exist
21/41 channel entries create page/2 and page/3 without loss or duplication
home contains one lead, at most three briefs, then compact remaining stories
home and day pages use explicit continuous 01…N labels, never CSS-generated 00
each article has a stable id and its title is a usable deep link
day pages have older/newer edition links and an archive link
sources page reads load_run_records(root), not a second JSON scanner
sources page includes every enabled configured source and aggregates the latest 7 calendar days
archive distinguishes published, no_eligible_content, failed, and never-run dates
mobile-visible archive count fields remain in the HTML
```

The public search entry must contain only:

```text
id, title, summary, why_it_matters, tags, display_category, source,
published_at, report_date, is_official, marketing_risk, page_url
```

Index only the 180 days preceding the newest published report, exclude Research, sort by publication time descending, and include a real `#item-...` fragment.

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_site_builder.py -q
```

Expected: FAIL on missing routes, pagination, fragments, and run-record views.

- [ ] **Step 3: Refactor build orchestration**

Keep the existing staging/install transaction intact. Load both `load_source_config(root / "sources/feeds.yaml")` and `load_run_records(root)` through their public APIs, build page contexts through presentation helpers, and generate:

```text
index.html
days/YYYY-MM-DD/index.html
categories/<fixed-channel>/index.html
categories/<fixed-channel>/page/N/index.html
archive/index.html
search/index.html
sources/index.html
search.json
```

Pass explicit page metadata and `active_nav` into every template. The home masthead gets only one concise status summary based on sources with `recent_count > 0`; detailed counts live on `/sources/`.

- [ ] **Step 4: Verify deterministic output**

Run the builder twice into separate temporary directories and assert byte-identical HTML/JSON after normalizing the generated timestamp already covered by existing tests.

```bash
python -m pytest tests/test_site_builder.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the builder slice**

```bash
git add src/ai_news/site tests/test_site_builder.py
git commit -m "feat: build compact navigable news editions"
```

### Task 3: Replace the demo-like templates with a mature newspaper system

**Files:**
- Modify: `src/ai_news/site/templates/base.html`
- Modify: `src/ai_news/site/templates/index.html`
- Modify: `src/ai_news/site/templates/day.html`
- Modify: `src/ai_news/site/templates/category.html`
- Modify: `src/ai_news/site/templates/archive.html`
- Delete: `src/ai_news/site/templates/_card.html`
- Create: `src/ai_news/site/templates/_story.html`
- Create: `src/ai_news/site/templates/_pagination.html`
- Create: `src/ai_news/site/templates/search.html`
- Create: `src/ai_news/site/templates/sources.html`
- Create: `src/ai_news/site/templates/404.html`
- Test: `tests/test_site_accessibility.py`
- Test: `tests/test_site_builder.py`

- [ ] **Step 1: Rewrite structural and accessibility tests first**

Require:

```text
one main element and one h1 per page
skip link and visible focus styles
fixed channel navigation with aria-current on the active page
semantic article/section/time elements and ordered heading levels
article titles are links, not a tiny source-only click target
source links announce opening a new site when target=_blank
search controls have labels and status is aria-live
day navigation has explicit older/newer labels
mobile archive retains date/status/item-count information
```

Delete assertions that forbid inputs/buttons or lock exact dark-dashboard color variables.

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_site_accessibility.py tests/test_site_builder.py -q
```

Expected: FAIL against the existing dashboard templates.

- [ ] **Step 3: Implement the approved editorial hierarchy**

Use the high-fidelity approved structure:

- `base.html`: compact utility strip, centered masthead, date/issue on the left, freshness cutoff on the right, fixed channel nav, one status line, restrained footer.
- `index.html`: one lead story with “发生了什么 / 为什么重要”, up to three one-paragraph briefs, then compact remaining stories.
- `day.html`: complete edition with article anchors, publication and collection timestamps, evidence/source type, and previous/next edition navigation.
- `category.html`: reverse-chronological compact timeline with a two-line summary and pagination, not fully expanded cards.
- `archive.html`: readable edition ledger including empty/failed/missing explanations.
- `sources.html`: source, role, request state, latest content, recent/new/eligible/candidate/selected counts.
- `404.html`: useful static recovery links that work under the `/ai-news/` project path.

- [ ] **Step 4: Verify template behavior**

Run:

```bash
python -m pytest tests/test_site_accessibility.py tests/test_site_builder.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit templates**

```bash
git add src/ai_news/site/templates tests/test_site_accessibility.py tests/test_site_builder.py
git commit -m "feat: redesign the site as a Chinese business daily"
```

### Task 4: Implement the responsive warm-paper visual system

**Files:**
- Modify: `src/ai_news/site/static/styles.css`
- Create: `src/ai_news/site/static/favicon.svg`
- Modify: `pyproject.toml`
- Test: `tests/test_site_accessibility.py`

- [ ] **Step 1: Add failing visual-contract tests**

Test for semantic variables and accessibility constraints instead of exact screenshots:

```text
warm paper background, dark ink body, restrained editorial red accent
body text at least 15px and metadata at least 12px
interactive target minimum 44px
820px single-column breakpoint
visible channel overflow affordance on narrow screens
focus-visible, reduced-motion, :target highlight, scroll-margin-top
no decorative gradient, heavy box shadow, glowing status, or dashboard card grid
```

Assert `favicon.svg` is packaged and copied.

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_site_accessibility.py -q
```

Expected: FAIL against the current dark tech-panel stylesheet.

- [ ] **Step 3: Rewrite the design tokens and responsive layout**

Use system/local serif stacks for headlines and readable Chinese sans-serif stacks for body copy; do not fetch third-party fonts. Keep page width editorially constrained, use rules/whitespace instead of card shadows, and make the first real story begin above 450px desktop and 500px mobile in the generated layout.

At widths below 820px, stack the masthead, keep the lead readable, expose horizontal channel scrolling with a visible cue, and retain archive/source metrics. At reduced motion, remove smooth scrolling/animated transitions.

- [ ] **Step 4: Verify CSS and packaged assets**

Run:

```bash
python -m pytest tests/test_site_accessibility.py tests/test_site_builder.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit visual system**

```bash
git add src/ai_news/site/static pyproject.toml tests/test_site_accessibility.py tests/test_site_builder.py
git commit -m "style: apply a responsive editorial newspaper system"
```

### Task 5: Add dependency-free full-text search and filters

**Files:**
- Create: `src/ai_news/site/static/search-core.js`
- Modify: `src/ai_news/site/static/app.js`
- Modify: `src/ai_news/site/templates/search.html`
- Create: `tests/site-search.test.cjs`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_workflows.py`

- [ ] **Step 1: Write failing Node tests for pure search logic**

Using `node:test` and `node:assert`, cover:

```text
Chinese substring and case-insensitive English search
title, summary, why-it-matters, source, and tag matching
channel, source, start-date, and end-date filters in combination
default recent-30-day scope and newest-first ordering
clear-filters behavior
stable result URL with article fragment
success, no-result, fetch-error, and invalid-JSON states
```

- [ ] **Step 2: Verify RED**

Run:

```bash
node --test tests/site-search.test.cjs
```

Expected: FAIL because the search module does not exist.

- [ ] **Step 3: Implement a safe dual-export search core**

Expose pure functions through a browser global and CommonJS without dependencies. `app.js` must only fetch `search.json` when `[data-search-page]` exists; other pages must not download the index. Render with `createElement` and `textContent`, never `innerHTML`, `eval`, or `new Function`.

The search page must provide query, channel, source, start date, end date, clear action, result count, loading state, empty state, and error state. The `/` shortcut may focus search only on precise-pointer devices and must ignore form fields and IME composition.

- [ ] **Step 4: Add the Node gate to CI**

Add `node --test tests/site-search.test.cjs` without introducing npm dependencies. Update workflow tests to assert the command exists and runs before the Python coverage gate completes.

- [ ] **Step 5: Verify search and workflow tests**

Run:

```bash
node --test tests/site-search.test.cjs
python -m pytest tests/test_workflows.py tests/test_site_builder.py tests/test_site_accessibility.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit search**

```bash
git add src/ai_news/site/static src/ai_news/site/templates/search.html tests/site-search.test.cjs .github/workflows/ci.yml tests/test_workflows.py
git commit -m "feat: add static full-text news search"
```

### Task 6: Generate SEO, sharing, RSS, sitemap, robots, and custom 404

**Files:**
- Modify: `src/ai_news/site/builder.py`
- Modify: `src/ai_news/cli.py`
- Modify: `src/ai_news/site/templates/base.html`
- Modify: `src/ai_news/site/templates/404.html`
- Test: `tests/test_site_builder.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing metadata and XML tests**

Use an XML parser to assert:

```text
every page has a unique absolute canonical under https://mount-su.github.io/ai-news/
every page has description, Open Graph, Twitter Card, and favicon metadata
feed.xml is valid and contains formally selected stories from the newest 30 days
feed item URLs use real day-page fragments and descriptions retain original-source links
sitemap.xml is valid and lists home, five channels and pages, search, archive, sources, and day pages
404 is excluded from sitemap
robots.txt points at the absolute sitemap URL
404.html links remain inside the GitHub Pages project path
build-info.json contains the exact injected 40-character build revision and optional numeric workflow run ID
```

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_site_builder.py -k "metadata or rss or sitemap or robots or not_found" -q
```

Expected: FAIL because the outputs do not exist.

- [ ] **Step 3: Generate safe static metadata**

Pass `page_title`, `description`, `canonical_url`, and `og_type` explicitly. Generate `feed.xml` and `sitemap.xml` with a standard XML builder, never string-concatenated unescaped XML. Use the fixed public root `https://mount-su.github.io/ai-news/` and keep build output paths relative for Pages.

Add explicit `build_revision` and `workflow_run_id` inputs to `build_site`; the CLI reads `AI_NEWS_BUILD_SHA` and `AI_NEWS_BUILD_RUN_ID`, validates them, and passes deterministic `local`/`offline-demo` values outside production. Emit `build-info.json` with only `schema_version`, `revision`, and `workflow_run_id`. This file is the production provenance authority after the build job checks out the data commit pushed by the generate job.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
python -m pytest tests/test_site_builder.py tests/test_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit discoverability assets**

```bash
git add src/ai_news/site src/ai_news/cli.py tests/test_site_builder.py tests/test_cli.py
git commit -m "feat: add RSS and static-site metadata"
```

### Task 7: Strengthen offline validation and finish the site gate

**Files:**
- Modify: `scripts/validate_site.py`
- Modify: `tests/test_validate_site.py`
- Modify: `tests/test_e2e_demo.py`

- [ ] **Step 1: Write failing validation tests**

Require the validator to reject invalid `search.json`, malformed feed/sitemap XML, malformed or missing `build-info.json`, missing robots/404/favicon, duplicate IDs, broken internal pages, and broken internal fragments. Extend the offline demo expected output set to all V2 routes.

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_validate_site.py tests/test_e2e_demo.py -q
```

Expected: FAIL because the validator only checks old HTML routes.

- [ ] **Step 3: Extend validation without network dependencies**

Parse JSON and XML locally. Validate all relative HTML links and fragments against the staging tree. Require exactly one main element and no duplicate IDs per HTML page. Keep external URLs out of the offline availability check.

- [ ] **Step 4: Run the complete site gate**

```bash
node --test tests/site-search.test.cjs
python -m pytest tests/test_site_presentation.py tests/test_site_builder.py tests/test_site_accessibility.py tests/test_validate_site.py tests/test_e2e_demo.py -q
python -m ai_news demo --root .tmp-demo-v2 --output .tmp-site-v2
python scripts/validate_site.py .tmp-site-v2 --base-path /ai-news/
```

Expected: all commands PASS and the V2 staging tree is complete.

- [ ] **Step 5: Commit validation changes**

```bash
git add scripts/validate_site.py tests/test_validate_site.py tests/test_e2e_demo.py
git commit -m "test: validate complete V2 static output"
```
