# DeepSeek V4 Production Timeout Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the standard Ark OpenAI Chat integration complete strict daily-report generation by disabling DeepSeek reasoning, allowing 120 seconds per request, and bounding the Actions generate job to 30 minutes.

**Architecture:** Keep protocol behavior isolated in `OpenAIChatAnalyzer`, keep the shared request timeout in `BaseAnalyzer`, and put the whole-job ceiling in the existing GitHub Actions job. Preserve the strict response validator, four-attempt retry policy, Secret boundaries, persistence behavior, and Anthropic request schema.

**Tech Stack:** Python 3.12+, httpx, pytest, PyYAML, Ruff, GitHub Actions, actionlint.

---

## File Map

| File | Responsibility |
| --- | --- |
| `src/ai_news/llm/openai_chat.py` | Add the fixed Ark Chat `thinking.type=disabled` request field. |
| `src/ai_news/llm/base.py` | Define and apply the shared 120-second per-request timeout. |
| `tests/test_openai_chat_client.py` | Lock the OpenAI request body and initial/repair timeout extensions. |
| `tests/test_anthropic_client.py` | Lock the shared timeout while proving Anthropic body shape is unchanged. |
| `.github/workflows/daily-news.yml` | Add the 30-minute generate-job ceiling. |
| `tests/test_workflows.py` | Lock the timeout ceiling and existing Secret scope. |

## Task 1: Disable reasoning in OpenAI Chat requests

**Files:**

- Modify: `tests/test_openai_chat_client.py`
- Modify: `src/ai_news/llm/openai_chat.py`

- [ ] **Step 1: Make the exact request-body test require disabled thinking**

In `test_request_uses_exact_ark_endpoint_headers_body_and_isolates_client_defaults`,
add the exact field to the expected body:

```python
assert body == {
    "model": "ark-standard-model",
    "temperature": 0.2,
    "max_tokens": 6000,
    "n": 1,
    "thinking": {"type": "disabled"},
    "messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": body["messages"][1]["content"]},
    ],
}
```

- [ ] **Step 2: Run the exact test and verify RED**

Run:

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  tests/test_openai_chat_client.py::test_request_uses_exact_ark_endpoint_headers_body_and_isolates_client_defaults \
  -q
```

Expected: FAIL because the actual body has no `thinking` field.

- [ ] **Step 3: Add the minimal OpenAI-only request field**

Update `OpenAIChatAnalyzer._body()`:

```python
return {
    "model": self._model,
    "temperature": 0.2,
    "max_tokens": 6000,
    "n": 1,
    "thinking": {"type": "disabled"},
    "messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ],
}
```

Do not change `AnthropicAnalyzer._body()`.

- [ ] **Step 4: Run OpenAI and Anthropic body tests and verify GREEN**

Run:

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  tests/test_openai_chat_client.py::test_request_uses_exact_ark_endpoint_headers_body_and_isolates_client_defaults \
  tests/test_anthropic_client.py::test_request_endpoint_bearer_headers_and_body_preserve_base_path \
  -q
```

Expected: 2 passed. The exact Anthropic body has no `thinking`.

- [ ] **Step 5: Commit the protocol change**

```bash
git add src/ai_news/llm/openai_chat.py tests/test_openai_chat_client.py
git commit -m "fix: disable Ark reasoning for daily analysis"
```

## Task 2: Raise the shared per-request timeout to 120 seconds

**Files:**

- Modify: `tests/test_openai_chat_client.py`
- Modify: `tests/test_anthropic_client.py`
- Modify: `src/ai_news/llm/base.py`

- [ ] **Step 1: Lock timeout on initial and repair OpenAI requests**

In the exact OpenAI request test, add:

```python
assert request.extensions["timeout"] == {
    "connect": 120,
    "read": 120,
    "write": 120,
    "pool": 120,
}
```

In `test_strict_analysis_failure_repairs_once_and_redacts_token`, after
`assert len(requests) == 2`, add:

```python
assert all(
    request.extensions["timeout"]
    == {
        "connect": 120,
        "read": 120,
        "write": 120,
        "pool": 120,
    }
    for request in requests
)
```

Update the existing Anthropic timeout assertion to expect 120 for all four phases.

- [ ] **Step 2: Run the three focused tests and verify RED**

Run:

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  tests/test_openai_chat_client.py::test_request_uses_exact_ark_endpoint_headers_body_and_isolates_client_defaults \
  tests/test_openai_chat_client.py::test_strict_analysis_failure_repairs_once_and_redacts_token \
  tests/test_anthropic_client.py::test_request_does_not_inherit_client_default_params_cookies_or_unrelated_headers \
  -q
```

Expected: FAIL because request extensions still contain 30-second values.

- [ ] **Step 3: Introduce one shared timeout constant**

Near the other limits in `base.py`, add:

```python
_LLM_REQUEST_TIMEOUT_SECONDS = 120
```

Change `_build_request()` to:

```python
extensions={
    "timeout": httpx.Timeout(_LLM_REQUEST_TIMEOUT_SECONDS).as_dict(),
},
```

Do not change retry count, retry statuses, delays, response bound, or client ownership.

- [ ] **Step 4: Run shared-core regression tests and verify GREEN**

Run:

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  tests/test_openai_chat_client.py tests/test_anthropic_client.py tests/test_pipeline_run.py \
  -q
```

Expected: all selected tests pass, including four attempts and 1/2/4 delays.

- [ ] **Step 5: Commit the timeout change**

```bash
git add src/ai_news/llm/base.py \
  tests/test_openai_chat_client.py tests/test_anthropic_client.py
git commit -m "fix: allow slower Ark analysis requests"
```

## Task 3: Bound the GitHub Actions generate job

**Files:**

- Modify: `tests/test_workflows.py`
- Modify: `.github/workflows/daily-news.yml`

- [ ] **Step 1: Add a workflow contract assertion**

In `test_daily_generate_is_write_scoped_conditional_and_maps_exact_environment`,
after checking the generate job mapping, add:

```python
assert generate["timeout-minutes"] == 30
```

Keep the existing exact environment assertion unchanged so the timeout addition cannot
broaden Secret scope.

- [ ] **Step 2: Run the workflow test and verify RED**

Run:

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  tests/test_workflows.py::test_daily_generate_is_write_scoped_conditional_and_maps_exact_environment \
  -q
```

Expected: FAIL with missing key `timeout-minutes`.

- [ ] **Step 3: Add the job ceiling**

Update the generate job:

```yaml
generate:
  if: >-
    ${{ github.event_name == 'schedule' ||
    github.event_name == 'workflow_dispatch' }}
  runs-on: ubuntu-latest
  timeout-minutes: 30
  permissions:
    contents: write
```

- [ ] **Step 4: Run workflow and Secret-contract tests**

Run:

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  tests/test_workflows.py tests/test_secret_scan.py -q
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/python \
  scripts/check_no_secrets.py
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7 \
  -ignore 'element of "schedule" section must be mapping and must contain one key "cron"' \
  .github/workflows/ci.yml .github/workflows/daily-news.yml
```

Expected: tests, repository scan, and actionlint all exit 0.

- [ ] **Step 5: Commit the workflow ceiling**

```bash
git add .github/workflows/daily-news.yml tests/test_workflows.py
git commit -m "ci: bound daily generation runtime"
```

## Task 4: Complete local and production verification

**Files:**

- Modify only files required by verified review findings

- [ ] **Step 1: Run the complete local gate**

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/ruff check .
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/ruff format --check .
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  --cov=ai_news --cov-report=term-missing --cov-fail-under=85
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/python \
  scripts/check_no_secrets.py
git diff --check main...HEAD
git status --short --branch
```

Expected: 0 failures, coverage at least 85%, clean scanner/diff, and no uncommitted files.

- [ ] **Step 2: Verify a fresh offline site**

```bash
verify_root="$(mktemp -d /tmp/ai-news-deepseek-verify.XXXXXX)"
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/python \
  -m ai_news demo --root "$verify_root/demo" --output "$verify_root/dist"
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/python \
  scripts/validate_site.py "$verify_root/dist" --base-path /ai-news/
```

Expected: both commands exit 0.

- [ ] **Step 3: Review the exact branch scope**

```bash
git log --oneline main..HEAD
git diff --stat main...HEAD
git merge-base --is-ancestor main HEAD
```

Expected: only the design, plan, protocol, timeout, workflow, and matching test changes.

- [ ] **Step 4: Fast-forward main, retest, push, and wait for CI**

From `/Users/suhuashan/Documents/AI资讯`:

```bash
git fetch origin
git merge --ff-only codex/deepseek-timeout
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest -q
git push origin main
```

Then watch the CI run whose `headSha` equals local `HEAD` and require `success`.

- [ ] **Step 5: Trigger and verify the real daily workflow**

```bash
gh workflow run daily-news.yml --repo mount-su/ai-news
gh run list --repo mount-su/ai-news --workflow daily-news.yml \
  --event workflow_dispatch --limit 1 \
  --json databaseId,status,conclusion,headSha,url
```

Watch that exact run ID and require generate, build, and deploy to succeed.

- [ ] **Step 6: Audit the generated commit**

After `git fetch origin`, require the bot commit to contain only `data/` and `content/`,
then fast-forward local main. Run the repository Secret scanner again.

- [ ] **Step 7: Verify Pages and browser behavior**

Require Pages metadata to use workflow deployment with HTTPS, HTTP 200 at
`https://mount-su.github.io/ai-news/`, at least five current report cards, working
search, category filter, details disclosure, archive/category routes, visible keyboard
focus, no horizontal overflow at 1440 px and 390 px, and no console/network errors.

- [ ] **Step 8: Complete the active goal**

Only after every local, GitHub Actions, generated-commit, Secret, HTTP, and browser
check has fresh evidence, update the persistent goal to `complete`.
