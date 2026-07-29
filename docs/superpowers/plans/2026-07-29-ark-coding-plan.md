# Ark Coding Plan Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely migrate `mount-su/ai-news` from the rejected standard Ark endpoint to the user's supported OpenAI-compatible Ark Coding Plan endpoint, then generate and deploy the first real report.

**Architecture:** Keep the existing OpenAI Chat analyzer and Secret boundary. Add one exact allowlist exception for the official Ark host plus `/api/coding/v3`, migrate operator-facing configuration contracts, and retain all existing URL hardening, schema validation, timeout, retry, and deployment behavior.

**Tech Stack:** Python 3.12+, httpx, Pydantic, pytest, Ruff, GitHub Actions, GitHub CLI, actionlint, GitHub Pages.

---

## File map

| File | Responsibility |
| --- | --- |
| `src/ai_news/llm/endpoint.py` | Canonicalize Base URLs and construct only supported request endpoints |
| `tests/test_llm_endpoint.py` | Prove the exact Coding Plan path works and bypass variants remain rejected |
| `tests/test_config.py` | Prove runtime settings accept the approved Coding Plan configuration without exposing the Secret |
| `tests/test_openai_chat_client.py` | Prove the analyzer constructs the exact Coding Plan Chat URL and request contract |
| `.env.example` | Provide safe, non-secret local configuration placeholders |
| `README.md` | Document current production provider configuration and Secret/model separation |
| `tests/test_readme_contract.py` | Lock the operator-facing Coding Plan instructions |
| `docs/superpowers/specs/2026-07-29-ark-coding-plan-design.md` | Canonical production-provider design supplement |

The existing workflow continues to read Repository Variables, so
`.github/workflows/daily-news.yml` does not require a provider URL or model-name edit.

### Task 1: Allow only the exact Ark Coding Plan OpenAI Base URL

**Files:**
- Modify: `tests/test_llm_endpoint.py`
- Modify: `src/ai_news/llm/endpoint.py`

- [ ] **Step 1: Replace the blanket Coding Plan rejection test with exact allow/reject contracts**

Add the approved endpoint to `test_build_llm_endpoint_preserves_safe_base_path`:

```python
(
    "https://ark.cn-beijing.volces.com/api/coding/v3",
    "chat/completions",
    "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions",
),
```

Add a canonicalization test:

```python
@pytest.mark.parametrize(
    "base_url",
    [
        "https://ark.cn-beijing.volces.com/api/coding/v3",
        "https://ark.cn-beijing.volces.com/api/coding/v3/",
        "https://ARK.CN-BEIJING.VOLCES.COM/api/coding/v3",
    ],
)
def test_validate_llm_base_url_allows_exact_ark_coding_plan_openai_base(
    base_url: str,
) -> None:
    assert validate_llm_base_url(base_url).casefold().rstrip("/") == (
        "https://ark.cn-beijing.volces.com/api/coding/v3"
    )
```

Keep rejection coverage for all non-exact paths:

```python
@pytest.mark.parametrize(
    "base_url",
    [
        "https://ark.cn-beijing.volces.com/api/coding",
        "https://ark.cn-beijing.volces.com/api/coding/",
        "https://ark.cn-beijing.volces.com/api/coding/v2",
        "https://ark.cn-beijing.volces.com/api/coding/v3/tenant",
        "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions",
        "https://ark.cn-beijing.volces.com/API/CODING/V3",
        "https://ark.cn-beijing.volces.com/api/v3/../coding/v3",
        "https://ark.cn-beijing.volces.com/api%2fcoding%2fv3",
        "https://ark.cn-beijing.volces.com/api%252fcoding%252fv3",
        "https://ark.cn-beijing.volces.com/api%5ccoding%5cv3",
    ],
)
def test_validate_llm_base_url_rejects_non_exact_ark_coding_plan_paths(
    base_url: str,
) -> None:
    with pytest.raises(InvalidLLMEndpoint):
        validate_llm_base_url(base_url)
```

- [ ] **Step 2: Run the endpoint tests and verify RED**

Run:

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  tests/test_llm_endpoint.py -q
```

Expected: the exact `/api/coding/v3` allow tests fail because the current validator rejects every
Ark Coding Plan path; all existing unsafe-URL tests remain green.

- [ ] **Step 3: Implement the minimal exact-path allowlist**

In `src/ai_news/llm/endpoint.py`, define:

```python
_ARK_CODING_OPENAI_BASE_PATH = "/api/coding/v3"
```

Replace the blanket Ark Coding Plan branch with:

```python
if canonical_host == _ARK_HOST and (
    comparable_path == "/api/coding"
    or comparable_path.startswith("/api/coding/")
):
    if normalized_path != _ARK_CODING_OPENAI_BASE_PATH:
        raise _invalid("Unsupported Ark Coding Plan endpoint.")
```

Do not weaken the later encoded-separator, dot-segment, full-endpoint, userinfo, query, fragment,
port, hostname, or control-character checks.

- [ ] **Step 4: Run endpoint tests and verify GREEN**

Run the command from Step 2.

Expected: all `tests/test_llm_endpoint.py` tests pass.

- [ ] **Step 5: Commit the endpoint boundary**

```bash
git add src/ai_news/llm/endpoint.py tests/test_llm_endpoint.py
git commit -m "feat: allow Ark Coding Plan OpenAI endpoint"
```

### Task 2: Lock the approved runtime and request contract

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_openai_chat_client.py`

- [ ] **Step 1: Write Coding Plan runtime tests**

Add to `tests/test_config.py`:

```python
def test_load_settings_accepts_approved_ark_coding_plan_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROTOCOL", "openai-chat")
    monkeypatch.setenv(
        "LLM_BASE_URL",
        "https://ark.cn-beijing.volces.com/api/coding/v3",
    )
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("LLM_AUTH_SCHEME", "bearer")

    settings = load_settings()

    assert settings.llm_base_url == "https://ark.cn-beijing.volces.com/api/coding/v3"
    assert settings.llm_model == "deepseek-v4-pro"
    assert settings.llm_token.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(settings)
```

Add to `tests/test_openai_chat_client.py` a request-construction test using
`llm_base_url="https://ark.cn-beijing.volces.com/api/coding/v3"` and assert:

```python
assert str(request.url) == (
    "https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions"
)
assert request.headers["authorization"] == "Bearer top-secret-token"
body = json.loads(request.content)
assert body["model"] == "deepseek-v4-pro"
assert body["thinking"] == {"type": "disabled"}
assert request.extensions["timeout"] == {
    "connect": 120,
    "read": 120,
    "write": 120,
    "pool": 120,
}
```

- [ ] **Step 2: Run the focused integration contracts**

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  tests/test_config.py::test_load_settings_accepts_approved_ark_coding_plan_configuration \
  tests/test_openai_chat_client.py -q
```

Expected: the tests pass after Task 1, demonstrating the endpoint allowlist composes correctly
through Pydantic settings and the real OpenAI request builder without a second production-code
change. The behavior itself was proven RED before implementation in Task 1.

- [ ] **Step 3: Verify the entire protocol and configuration layer**

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  tests/test_config.py tests/test_llm_endpoint.py tests/test_llm_factory.py \
  tests/test_openai_chat_client.py tests/test_anthropic_client.py -q
```

Expected: all tests pass; Anthropic request-body behavior is unchanged.

- [ ] **Step 4: Commit the runtime contracts**

```bash
git add tests/test_config.py tests/test_openai_chat_client.py
git commit -m "test: lock Ark Coding Plan runtime contract"
```

### Task 3: Migrate operator documentation and safe examples

**Files:**
- Modify: `tests/test_readme_contract.py`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Replace standard-Ark documentation assertions with Coding Plan assertions**

Update the environment example contract to require:

```python
assert "LLM_PROTOCOL=openai-chat" in env_example
assert (
    "LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3"
    in env_example
)
assert "LLM_API_" + "KEY=replace-with-a-repository-secret" in env_example
assert "LLM_MODEL=deepseek-v4-pro" in env_example
assert "LLM_AUTH_SCHEME=bearer" in env_example
```

Replace `test_readme_documents_standard_ark_operator_boundaries` with a Coding Plan contract that
requires these fragments:

```python
required_fragments = [
    "https://ark.cn-beijing.volces.com/api/coding/v3",
    "`deepseek-v4-pro` 是模型名",
    "不是 `LLM_API_KEY`",
    "直接写入 GitHub Secret",
    "不能发送到聊天",
    "不能写入文件",
    "至少一份成功生成的真实日报",
    "双协议模型适配与严格响应校验",
]
```

- [ ] **Step 2: Run documentation tests and verify RED**

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  tests/test_readme_contract.py -q
```

Expected: assertions fail against the old `/api/v3` examples and blanket Coding Plan prohibition.

- [ ] **Step 3: Update `.env.example`**

Use only non-secret values:

```dotenv
LLM_PROTOCOL=openai-chat
LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3
LLM_API_KEY=replace-with-a-repository-secret
LLM_MODEL=deepseek-v4-pro
LLM_AUTH_SCHEME=bearer
SITE_BASE_PATH=/ai-news/
```

- [ ] **Step 4: Update README production instructions**

Replace standard-Ark-specific prose and commands with:

```bash
export LLM_PROTOCOL="openai-chat"
export LLM_BASE_URL="https://ark.cn-beijing.volces.com/api/coding/v3"
export LLM_MODEL="deepseek-v4-pro"
export LLM_AUTH_SCHEME="bearer"
read -r -s LLM_API_KEY
export LLM_API_KEY
```

State explicitly that `deepseek-v4-pro` is the model name and is not `LLM_API_KEY`; the latter must
remain a real provider API Key stored only in GitHub Secret or hidden local input. Update the
GitHub Variable examples to the same Base URL and model.

Keep the general credential-safety, report threshold, strict schema, schedule, and Pages
documentation unchanged.

- [ ] **Step 5: Run documentation and Secret tests and verify GREEN**

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  tests/test_readme_contract.py tests/test_secret_scan.py -q
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/python \
  scripts/check_no_secrets.py
```

Expected: tests pass and the repository scan exits 0 without output.

- [ ] **Step 6: Commit the operator migration**

```bash
git add README.md .env.example tests/test_readme_contract.py
git commit -m "docs: configure Ark Coding Plan production"
```

### Task 4: Run the complete local quality gate

**Files:**
- Verify all changed files

- [ ] **Step 1: Run Ruff**

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/ruff check .
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/ruff format --check .
```

Expected: both commands exit 0.

- [ ] **Step 2: Run the complete suite with coverage**

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/pytest \
  --cov=ai_news --cov-report=term-missing --cov-fail-under=85 -q
```

Expected: all tests pass and total coverage remains at least 85%. Remove only the generated
worktree-local `.coverage` file afterward if it exists.

- [ ] **Step 3: Run repository, site, and workflow gates**

```bash
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/python \
  scripts/check_no_secrets.py
AI_NEWS_TMP_ROOT=$(mktemp -d)
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/python \
  -m ai_news demo --root "$AI_NEWS_TMP_ROOT" --output "$AI_NEWS_TMP_ROOT/dist"
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/python \
  scripts/validate_site.py "$AI_NEWS_TMP_ROOT/dist" --base-path /ai-news/
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7 \
  -ignore 'element of "schedule" section must be mapping and must contain one key "cron"' \
  .github/workflows/ci.yml .github/workflows/daily-news.yml
git diff --check main...HEAD
```

Expected: every command exits 0.

- [ ] **Step 4: Review scope and history**

```bash
git status --short --branch
git log --oneline main..HEAD
git diff --stat main...HEAD
git diff main...HEAD
```

Expected: only the approved endpoint, runtime tests, current provider documentation, design, and
plan are present; no Secret values or unrelated files appear.

### Task 5: Integrate, push, and verify the exact CI SHA

**Files:**
- Integrate the verified commits into `/Users/suhuashan/Documents/AI资讯`

- [ ] **Step 1: Confirm the base is unchanged and fast-forward main**

```bash
git -C /Users/suhuashan/Documents/AI资讯 fetch origin
git -C /Users/suhuashan/Documents/AI资讯 status --short --branch
git -C /Users/suhuashan/Documents/AI资讯 merge --ff-only codex/deepseek-timeout
```

Expected: clean main fast-forwards; no merge commit is created.

- [ ] **Step 2: Re-run the full pytest and Secret gate on merged main**

Use the commands from Task 4 with `workdir=/Users/suhuashan/Documents/AI资讯`.

Expected: all tests pass, coverage is at least 85%, and the Secret scan exits 0.

- [ ] **Step 3: Push and verify SHA parity**

```bash
git -C /Users/suhuashan/Documents/AI资讯 push origin main
LOCAL_SHA=$(git -C /Users/suhuashan/Documents/AI资讯 rev-parse HEAD)
REMOTE_SHA=$(git -C /Users/suhuashan/Documents/AI资讯 ls-remote origin refs/heads/main | cut -f1)
test "$LOCAL_SHA" = "$REMOTE_SHA"
```

Expected: push succeeds and both SHAs are identical.

- [ ] **Step 4: Watch only the CI run for the pushed SHA**

```bash
gh run list --repo mount-su/ai-news --commit "$LOCAL_SHA" \
  --json databaseId,workflowName,event,status,conclusion,url,headSha
CI_RUN_ID=$(gh run list --repo mount-su/ai-news --commit "$LOCAL_SHA" \
  --json databaseId,workflowName \
  --jq '[.[] | select(.workflowName == "CI")][0].databaseId')
test -n "$CI_RUN_ID"
gh run watch "$CI_RUN_ID" --repo mount-su/ai-news --exit-status
```

Expected: the exact `CI` run succeeds.

### Task 6: Generate a real report and audit the bot commit

**Files:**
- External state: Repository Variables, Actions run, generated `data/` and `content/`

- [ ] **Step 1: Confirm configuration names without reading the Secret**

```bash
gh variable list --repo mount-su/ai-news
gh secret list --repo mount-su/ai-news
```

Expected:

```text
LLM_PROTOCOL     openai-chat
LLM_BASE_URL     https://ark.cn-beijing.volces.com/api/coding/v3
LLM_MODEL        deepseek-v4-pro
LLM_AUTH_SCHEME  bearer
LLM_API_KEY      a timestamp, with no Secret value
```

- [ ] **Step 2: Trigger and watch one manual daily run**

```bash
MANUAL_RUN_URL=$(gh workflow run daily-news.yml --repo mount-su/ai-news)
MANUAL_RUN_ID=${MANUAL_RUN_URL##*/}
test -n "$MANUAL_RUN_ID"
gh run view "$MANUAL_RUN_ID" --repo mount-su/ai-news \
  --json databaseId,status,conclusion,createdAt,headSha,url
gh run watch "$MANUAL_RUN_ID" --repo mount-su/ai-news --exit-status
```

Expected: `generate`, `build`, and `deploy` all succeed. If the run fails, inspect only
credential-safe logs and continue systematic debugging; do not blindly dispatch another run.

- [ ] **Step 3: Audit the generated commit**

```bash
git -C /Users/suhuashan/Documents/AI资讯 fetch origin
git -C /Users/suhuashan/Documents/AI资讯 show \
  --name-only --format='%H%n%an%n%s' origin/main
```

Expected: author is `github-actions[bot]`, subject is `data: publish daily AI news`, and every
changed path starts with `data/` or `content/`.

- [ ] **Step 4: Validate the generated report and synchronize local main**

Inspect the new JSON and Markdown without printing environment variables or credentials. Require:

- report date equals the intended Shanghai date;
- at least five items;
- every item has title, summary, why-it-matters, tracking signal, category, importance, and URL;
- model is `deepseek-v4-pro`;
- source-run metadata contains no credential data.

Then:

```bash
git -C /Users/suhuashan/Documents/AI资讯 merge --ff-only origin/main
PYTHONPATH=src /Users/suhuashan/.config/superpowers/worktrees/AI资讯/ai-news/.venv/bin/python \
  /Users/suhuashan/Documents/AI资讯/scripts/check_no_secrets.py
```

Expected: local main fast-forwards and the Secret scan exits 0.

### Task 7: Verify GitHub Pages and complete the goal

**Files:**
- External state: GitHub Pages deployment and rendered site

- [ ] **Step 1: Verify Pages configuration and HTTP routes**

```bash
gh api repos/mount-su/ai-news/pages
curl -fsS -o /dev/null -w '%{http_code}\n' https://mount-su.github.io/ai-news/
curl -fsS -o /dev/null -w '%{http_code}\n' https://mount-su.github.io/ai-news/archive/
curl -fsS -o /dev/null -w '%{http_code}\n' https://mount-su.github.io/ai-news/categories/model/
```

Expected: Pages uses workflow deployment, HTTPS is enforced, and all routes return `200`.

- [ ] **Step 2: Perform desktop browser verification at 1440px**

Using the in-app browser, verify:

- the newest real report date and at least five cards are visible;
- styles and JavaScript load without console or network errors;
- search filters visible cards;
- category buttons update `aria-pressed`;
- details expand and collapse;
- archive and category navigation work;
- keyboard focus is visible.

- [ ] **Step 3: Perform mobile browser verification at 390px**

Verify the same critical interactions and confirm:

```javascript
document.documentElement.scrollWidth <= document.documentElement.clientWidth
```

Expected: true, with no clipped controls or horizontal overflow.

- [ ] **Step 4: Perform final security and schedule audit**

Confirm:

- repository Secret scan still passes;
- deployed HTML, JavaScript, JSON, and search index contain neither credential values nor
  credential configuration;
- schedule remains `17 8 * * *` with `timezone: "Asia/Shanghai"`;
- recent CI and Daily runs are successful;
- local main and `origin/main` are synchronized.

- [ ] **Step 5: Complete the persistent goal**

Only after all prior checks succeed, mark every plan step complete and mark the goal complete.
Report the Pages URL, report date and item count, exact CI and Daily run URLs, final SHA, bot commit
scope, local test count and coverage, and daily schedule.
