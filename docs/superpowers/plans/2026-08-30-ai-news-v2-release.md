# AI News V2 Workflow, Release, and 14-Day Backfill Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship V2 safely, make scheduled and push deployments reliable, preserve safe failure diagnostics, regenerate the most recent 14 dates under the new quality rules, and prove the live Pages site and content provenance.

**Architecture:** Keep GitHub Actions and GitHub Pages as the only deployment system. Make workflow outcome conditions explicit, persist safe run records before propagating a generation failure, and run source-health on a separate read-only schedule. Release through one reviewed PR, then dispatch historical dates strictly one at a time so workflow concurrency and historical dedupe remain correct.

**Tech Stack:** GitHub Actions, GitHub Pages, Git, GitHub CLI, Python 3.12, pytest, Ruff, actionlint, curl/browser verification.

---

### Task 1: Correct workflow outcome semantics

**Files:**
- Modify: `.github/workflows/daily-news.yml`
- Modify: `.github/workflows/source-health.yml`
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_workflows.py`

- [ ] **Step 1: Write failing workflow-contract tests**

Add precise assertions for:

```text
push to main: generate is skipped, build runs, successful build allows deploy
failed/skipped build never deploys
manual/scheduled generation captures the CLI exit code
run-record staging and secret scan happen before the exit code is propagated
generation failure prevents build/deploy but leaves the prior Pages deployment intact
source-health runs daily at 07:17 Asia/Shanghai, retains PR/manual triggers, and receives no secret
CI runs the dependency-free Node search tests
manual Daily runs have a date-bearing run-name so a dispatch can be identified unambiguously
the build job injects its checked-out revision and workflow run ID into build-info.json
```

The deploy job must contain this exact condition:

```yaml
if: ${{ always() && needs.build.result == 'success' }}
```

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_workflows.py -q
```

Expected: FAIL because push deploy is currently skipped and source-health is not scheduled.

- [ ] **Step 3: Capture generation failure without losing the run record**

Remove the separate `check-config` preflight from this workflow; the unified `generate` command now records safe configuration failures itself. Replace immediate `set -e` termination around `python -m ai_news generate` with a bounded step that captures its numeric exit code as a step output without printing environment values. Always stage only the existing allowlisted paths `data` and `content`, run `scripts/check_no_secrets.py`, commit/push any safe report or run-record change, then use a separate step to exit with the captured code.

Do not add `[skip ci]`: commits made by the workflow's default `GITHUB_TOKEN` do not recursively start a new push workflow. Preserve the existing retry and concurrency safety.

- [ ] **Step 4: Add the source-health schedule and push-deploy condition**

Use:

```yaml
schedule:
  - cron: "17 7 * * *"
    timezone: "Asia/Shanghai"
```

The source-health job remains `contents: read`, receives no LLM or Reddit secret, and publishes no data. Add the explicit deploy condition above.

Give Daily an explicit `run-name` containing the requested date (or `today`). Immediately after the build job checks out current `main`, write the exact `git rev-parse HEAD` to `AI_NEWS_BUILD_SHA` and `GITHUB_RUN_ID` to `AI_NEWS_BUILD_RUN_ID` through `GITHUB_ENV`; the static builder publishes those values in `build-info.json`. Also write the revision to the job summary without printing any secret.

- [ ] **Step 5: Verify workflow tests and actionlint**

Run:

```bash
python -m pytest tests/test_workflows.py -q
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.7@sha256:887a259a5a534f3c4f36cb02dca341673c6089431057242cdc931e9f133147e9 -ignore 'element of "schedule" section must be mapping and must contain one key "cron"' .github/workflows/ci.yml .github/workflows/daily-news.yml .github/workflows/source-health.yml
```

Expected: PASS.

- [ ] **Step 6: Commit workflow fixes**

```bash
git add .github/workflows tests/test_workflows.py
git commit -m "fix: make news workflows deploy and report reliably"
```

### Task 2: Make V2 backfill semantics explicit and recoverable

**Files:**
- Modify: `src/ai_news/storage/repository.py`
- Modify: `src/ai_news/site/presentation.py`
- Modify: `src/ai_news/site/builder.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_site_presentation.py`
- Test: `tests/test_site_builder.py`

- [ ] **Step 1: Write failing stale-report shadow tests**

Create a historical report and then a same-date `no_eligible_content` run record. Assert:

```text
the old report remains recoverable in Git/on disk
the report no longer contributes URLs to later historical dedupe
the report no longer appears as a published day in current aggregation/search/category pages
archive shows that date as “无合格内容”
homepage falls back to the newest earlier published report
failed_before_publish does not hide a previously published report
published run record keeps the corresponding report active
```

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_storage.py tests/test_site_presentation.py tests/test_site_builder.py -k "shadow or no_eligible or active_report" -q
```

Expected: FAIL because run records do not yet affect active historical views.

- [ ] **Step 3: Implement one active-report policy**

Add a shared storage/presentation helper that treats a same-date `no_eligible_content` run record as authoritative over an older report. A failed retry is diagnostic only and leaves a previously published report active. Use this policy both in `load_history_urls()` and site aggregation so dedupe, archive, search, categories, and homepage cannot disagree.

Do not physically delete historical files as part of generation; Git history and the old JSON remain available for recovery. Do not let an empty historical regeneration overwrite the latest successful homepage.

- [ ] **Step 4: Verify backfill semantics**

Run:

```bash
python -m pytest tests/test_storage.py tests/test_site_presentation.py tests/test_site_builder.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit active-report policy**

```bash
git add src/ai_news/storage/repository.py src/ai_news/site tests/test_storage.py tests/test_site_presentation.py tests/test_site_builder.py
git commit -m "fix: shadow superseded historical reports"
```

### Task 3: Update operator documentation and contract tests

**Files:**
- Modify: `README.md`
- Modify: `tests/test_readme_contract.py`

- [ ] **Step 1: Write failing documentation-contract tests**

Require the README to state:

```text
daily news runs at 08:17 Asia/Shanghai
source health runs at 07:17 Asia/Shanghai
quality target is 3–7 and may publish 1–2 or no edition rather than filler
source roles and freshness/contribution counts are distinct
only LLM_API_KEY remains required; Reddit uses RSS and no Reddit secret
manual historical runs must be oldest to newest and sequential
pushes to main build and deploy Pages even when generation is skipped
public routes include search, sources, RSS, sitemap, robots, and 404
```

- [ ] **Step 2: Verify RED**

Run:

```bash
python -m pytest tests/test_readme_contract.py -q
```

Expected: FAIL on old 5–9 or nine-item language and incomplete routes.

- [ ] **Step 3: Update the README without exposing configuration values**

Document secret names only; never include the current `LLM_API_KEY` value or a copyable token. Explain status semantics in user-facing language and link to the live site, Actions page, RSS, and source-status page.

- [ ] **Step 4: Verify GREEN and commit**

```bash
python -m pytest tests/test_readme_contract.py -q
git add README.md tests/test_readme_contract.py
git commit -m "docs: explain V2 publishing and quality rules"
```

### Task 4: Run the complete pre-release gate and independent review

**Files:**
- Modify only files required to repair discovered defects.

- [ ] **Step 1: Run format, lint, all tests, and coverage**

```bash
python -m ruff format .
python -m ruff check .
node --test tests/site-search.test.cjs
python -m pytest --cov=ai_news --cov-report=term-missing --cov-fail-under=85
```

Expected: all PASS and coverage at least 85%.

- [ ] **Step 2: Run secret and offline-site gates**

```bash
python scripts/check_no_secrets.py
python -m ai_news demo --root .tmp-release-demo --output .tmp-release-site
python scripts/validate_site.py .tmp-release-site --base-path /ai-news/
```

Expected: all PASS, with no secret value printed.

- [ ] **Step 3: Inspect the exact diff and run independent code review**

Review `git diff origin/main...HEAD`, check for unrelated user files, unsafe external HTML, unbounded index growth, workflow permission expansion, credential output, and migrations that break old report loading. Resolve every P0/P1 finding and rerun the relevant gates.

- [ ] **Step 4: Commit final repairs if needed**

```bash
git add src tests scripts sources .github README.md pyproject.toml
git diff --cached --quiet || git commit -m "fix: resolve V2 release review findings"
```

### Task 5: Publish one PR, merge, and prove push deployment

**Files:**
- No new implementation files.

- [ ] **Step 1: Push the feature branch and create the PR**

```bash
git push -u origin codex/site-v2-overhaul
gh pr create --repo mount-su/ai-news --base main --head codex/site-v2-overhaul --title "feat: launch AI News V2" --body "Launches the quality-first pipeline, mature responsive newspaper UI, static search, source diagnostics, workflow fixes, and verified release gates described in the committed V2 specification."
```

Update the PR body from the verified change/test summary before creation if the implementation differs; never include credentials or generated secret values.

- [ ] **Step 2: Wait for required checks and merge**

```bash
V2_PR_NUMBER="$(gh pr view --repo mount-su/ai-news codex/site-v2-overhaul --json number --jq .number)"
gh pr checks --repo mount-su/ai-news --watch "$V2_PR_NUMBER"
gh pr merge --repo mount-su/ai-news --squash --delete-branch "$V2_PR_NUMBER"
```

Expected: PR checks PASS and merge returns success. Record the merge SHA.

- [ ] **Step 3: Verify the push-triggered workflows**

Find runs for the merge SHA. CI must pass. Daily News must show:

```text
generate: skipped
build: success
deploy: success
```

This is the production proof for the previously skipped push deployment bug; a local YAML test is not sufficient.

### Task 6: Run source health and regenerate 14 dates serially

**Files:**
- Generated safe data/run-record/content files on `main` only.

- [ ] **Step 1: Run and inspect source health**

```bash
gh workflow run source-health.yml --repo mount-su/ai-news --ref main
```

Resolve the new run ID and watch it. Confirm all enabled primary/trusted-media sources are represented, freshness is distinct from reachability, and discovery failures do not masquerade as fresh contribution.

Identify this run by snapshotting current `databaseId` values before dispatch and selecting a newly appearing `workflow_dispatch` run afterward; never assume the newest repository run belongs to this operation.

- [ ] **Step 2: Dispatch each historical date oldest to newest**

The fixed sequence is:

```text
2026-08-17, 2026-08-18, 2026-08-19, 2026-08-20,
2026-08-21, 2026-08-22, 2026-08-23, 2026-08-24,
2026-08-25, 2026-08-26, 2026-08-27, 2026-08-28,
2026-08-29, 2026-08-30
```

For one date at a time:

```bash
RUN_ID_SNAPSHOT="$(mktemp)"
gh run list --repo mount-su/ai-news --workflow daily-news.yml --event workflow_dispatch --limit 100 --json databaseId --jq '.[].databaseId' > "$RUN_ID_SNAPSHOT"
gh workflow run daily-news.yml --repo mount-su/ai-news --ref main -f date=YYYY-MM-DD
```

Poll `gh run list --workflow daily-news.yml --event workflow_dispatch --json databaseId,displayTitle,createdAt` until a database ID not present in `$RUN_ID_SNAPSHOT` appears whose date-bearing `displayTitle` contains the requested date. Record that exact ID, run `gh run watch --repo mount-su/ai-news <id> --exit-status`, and do not dispatch the next date until the current one finishes and its main-branch data commit is visible. Rapid bulk dispatch is forbidden because the workflow concurrency group retains at most one pending run.

- [ ] **Step 3: Validate every regenerated date before continuing**

For each date, read the deployed `build-info.json`, record `{date, run_id, result, data_sha, status}`, fetch current `origin/main`, and use `git show origin/main:data/runs/YYYY/MM/YYYY-MM-DD.json` (plus the report path when published) for the assertions below. The build-info `workflow_run_id` must equal the watched ID and its `revision` must be an ancestor of current `origin/main`.

```text
data/runs/YYYY/MM/YYYY-MM-DD.json exists
status is published or no_eligible_content
published reports have 1–7 items and at most 2 from one source
no Research category is published
no unverified discovery or high-risk non-primary story is published
historical cutoff matches Shanghai end-of-day
no_eligible dates shadow an older same-date report in current site/history views
```

Do not treat a successful Actions badge alone as content-quality acceptance.

- [ ] **Step 4: Run the current-day edition once more**

After the 14 dates finish, dispatch 2026-08-30 one final time so its history view includes every rebuilt prior date. Wait for generate, build, and deploy success.

### Task 7: Prove production provenance, behavior, and content quality

**Files:**
- No implementation files unless a verified defect requires a new PR.

- [ ] **Step 1: Verify deployment provenance**

Record the V2 merge SHA. Read deployed `/build-info.json` and use its `revision` as the authoritative final data/deployment SHA; confirm its `workflow_run_id` is the final successful build/deploy run. Fetch `origin/main` and require:

```bash
git merge-base --is-ancestor "$V2_MERGE_SHA" "$DEPLOYMENT_SHA"
```

Also verify the GitHub Pages deployment references the same final build run. A reachable URL without SHA ancestry is not enough.

- [ ] **Step 2: Verify public static assets**

Require HTTP 200 and correct content for:

```text
https://mount-su.github.io/ai-news/
/categories/model/
/categories/agent/
/categories/ai-tools/
/categories/open-source/
/categories/industry-policy/
/search/
/archive/
/sources/
/feed.xml
/sitemap.xml
/robots.txt
/search.json
/build-info.json
/404.html
```

Verify a real unknown route receives the Pages 404 experience or useful fallback.

- [ ] **Step 3: Run browser regression at desktop and mobile sizes**

At 1440×900 and 390×844 verify:

```text
no horizontal page overflow
first real story is visible within the first viewport region
no duplicated health status and no 00 item numbers
fixed channel navigation indicates the active page
search query plus channel/source/date filters work and deep-link to the article
category lists are reverse chronological and paginated
day pages navigate to older/newer editions
archive keeps counts on mobile and explains empty/failed/missing dates
```

- [ ] **Step 4: Audit the regenerated 14-day content set**

Generate a deterministic source/category/risk summary from current public reports and require:

```text
arXiv/Research published count is 0
unverified Product Hunt/Reddit count is 0
high marketing-risk non-primary count is 0
no single source dominates the 14-day set
each populated primary channel has at least two independent recent sources when supply exists
publication timestamps and collection dates are both visible and not conflated
```

If a quality or live defect is found, fix it on a new `codex/` branch, repeat PR/CI/deploy gates, and rerun only the affected historical dates in chronological order.

- [ ] **Step 5: Record final evidence and complete the goal**

Report the PR URL, merge SHA, final deployment SHA/run, source-health run, the 14 backfill run IDs/statuses, live URLs checked, test/coverage results, and any genuinely empty dates. Mark the goal complete only after all required work and live verification pass.
