# Balanced Editorial Relaxation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement scheme B by expanding the news window to 72 hours and relaxing technical-content gates only for high-impact AI/product/developer ecosystem items.

**Architecture:** Keep the existing Python pipeline and schema. Change the deterministic time and editorial gates in place, align the LLM prompt with the same contract, and prove the behavior through focused tests plus full project verification.

**Tech Stack:** Python 3.13 local venv, pytest, pytest-cov, Ruff, GitHub Pages static-site pipeline.

---

## File Structure

- Modify `src/ai_news/pipeline/run.py`: change the global collection window from 36 hours to 72 hours.
- Modify `tests/test_pipeline_run.py`: update the boundary test so it proves 72-hour inclusion and future exclusion.
- Modify `src/ai_news/pipeline/editorial.py`: separate academic/low-value technical rejection from high-impact technical acceptance.
- Modify `tests/test_editorial_filter.py`: add explicit acceptance and rejection cases for the new technical-quality policy.
- Modify `src/ai_news/llm/prompt.py`: remove absolute SDK/API/framework/code-tool rejection and replace it with high-quality technical selection language.
- Modify `tests/test_llm_prompt.py`: assert the prompt contract no longer contains the old absolute rejection and still rejects low-quality technical content.

## Task 1: Expand Candidate Window to 72 Hours

**Files:**
- Modify: `src/ai_news/pipeline/run.py`
- Modify: `tests/test_pipeline_run.py`

- [ ] **Step 1: Write the failing boundary test**

Replace `test_time_window_includes_both_boundaries_and_excludes_future` in `tests/test_pipeline_run.py` with:

```python
def test_time_window_includes_seventy_two_hour_boundaries_and_excludes_future() -> None:
    timestamps = [
        NOW - timedelta(hours=72),
        NOW,
        NOW - timedelta(hours=72, microseconds=1),
        NOW + timedelta(microseconds=1),
    ]
    analyzer = _Analyzer()
    items = [_raw(index, published_at=timestamp) for index, timestamp in enumerate(timestamps)]
    source = _source(0)
    source_config, collector = _with_filler_sources(source, items)

    _run(
        source_config=source_config,
        collector=collector,
        analyzer=analyzer,
    )

    assert len(analyzer.calls) == 1
    assert {
        item.raw.published_at for item in analyzer.calls[0] if item.raw.source_id == source.id
    } == set(timestamps[:2])
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
.venv/bin/pytest tests/test_pipeline_run.py::test_time_window_includes_seventy_two_hour_boundaries_and_excludes_future -q
```

Expected: FAIL because `NOW - timedelta(hours=72)` is outside the current 36-hour `_WINDOW`.

- [ ] **Step 3: Write the minimal implementation**

In `src/ai_news/pipeline/run.py`, change:

```python
_WINDOW = timedelta(hours=36)
```

to:

```python
_WINDOW = timedelta(hours=72)
```

- [ ] **Step 4: Run the focused test and verify it passes**

Run:

```bash
.venv/bin/pytest tests/test_pipeline_run.py::test_time_window_includes_seventy_two_hour_boundaries_and_excludes_future -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/ai_news/pipeline/run.py tests/test_pipeline_run.py
git commit -m "feat: expand editorial collection window"
```

## Task 2: Relax Deterministic Editorial Filter for High-Impact Technical Content

**Files:**
- Modify: `src/ai_news/pipeline/editorial.py`
- Modify: `tests/test_editorial_filter.py`

- [ ] **Step 1: Write failing acceptance tests**

Add this test to `tests/test_editorial_filter.py`:

```python
@pytest.mark.parametrize(
    "title,excerpt",
    [
        (
            "LangChain v1.0 introduces breaking agent runtime changes",
            "The release changes production agent deployment paths and migration requirements.",
        ),
        (
            "OpenAI API update reduces enterprise inference latency",
            "The API change improves latency and lowers cost for production deployments.",
        ),
        (
            "Claude Code SDK adds production stability controls",
            "The SDK update targets enterprise rollout reliability and compatibility.",
        ),
        (
            "Transformers release expands model availability for developers",
            "The release changes access to widely used models across production AI applications.",
        ),
    ],
)
def test_editorial_filter_accepts_high_impact_developer_ecosystem_updates(
    title: str,
    excerpt: str,
) -> None:
    assert is_editorially_eligible(_candidate(title, excerpt, source_id="github-langchain")) is True
```

- [ ] **Step 2: Write rejection tests for low-quality technical content**

Add this test to `tests/test_editorial_filter.py`:

```python
@pytest.mark.parametrize(
    "title,excerpt",
    [
        ("How to build a prompt template in five minutes", "A generic tutorial with no new product facts."),
        ("Model benchmark leaderboard update", "Scores changed without product availability or cost impact."),
        ("SDK v2.4.1 fixes connector timeout", "A patch-level changelog item for one connector."),
        ("Framework repository adds example notebooks", "The update is documentation-only and not a major release."),
    ],
)
def test_editorial_filter_rejects_low_value_technical_content(
    title: str,
    excerpt: str,
) -> None:
    assert is_editorially_eligible(_candidate(title, excerpt, source_id="github-langchain")) is False
```

- [ ] **Step 3: Run the focused tests and verify the new acceptance test fails**

Run:

```bash
.venv/bin/pytest tests/test_editorial_filter.py::test_editorial_filter_accepts_high_impact_developer_ecosystem_updates tests/test_editorial_filter.py::test_editorial_filter_rejects_low_value_technical_content -q
```

Expected: FAIL on at least the high-impact SDK/API/framework cases because the current filter rejects those technical markers before considering the new quality contract.

- [ ] **Step 4: Write the minimal filter implementation**

Update `src/ai_news/pipeline/editorial.py` so the marker groups are:

```python
_ACADEMIC_MARKERS = re.compile(
    r"(?:\barxiv\b|论文|学术|研究论文|科研|模型原理|模型训练|alignment behavior|对齐行为)",
    re.IGNORECASE,
)
_LOW_VALUE_TECHNICAL_MARKERS = re.compile(
    r"(?:benchmark|基准测试|评测集|小版本|补丁|修复连接器|超时配置|泛教程|教程|"
    r"template|notebook|documentation-only|example notebooks|"
    r"\b(?:v|version)\s*\d+\.\d+\.\d+|\b\d+\.\d+\.\d+\b)",
    re.IGNORECASE,
)
_DEVELOPER_ECOSYSTEM_MARKERS = re.compile(
    r"(?:\bsdk\b|\bapi\b|接口协议|开发框架|framework|repository|仓库|代码工具|"
    r"agent runtime|runtime|开源项目)",
    re.IGNORECASE,
)
_CORPORATE_ONLY_MARKERS = re.compile(
    r"(?:融资|估值|募资|轮融资|高管|任命|离职|收购|投资|funding|valuation|executive|"
    r"appointed|resigns|acquisition|investment)",
    re.IGNORECASE,
)
_CONCRETE_IMPACT_MARKERS = re.compile(
    r"(?:产品|功能|套餐|订阅|价格|免费|free|tier|开放|上线|发布|推出|用户|客户|消费者|"
    r"企业|部署|采用|购物|平台|政策|监管|规则|合规|市场|可用|访问|服务|开发者|"
    r"production|enterprise|availability|access|cost|latency|compatibility|migration)",
    re.IGNORECASE,
)
_GITHUB_RELEASE_MARKERS = re.compile(
    r"(?:\brelease\b|\bchangelog\b|patch|修复|版本|\bversion\b|\bv\d+(?:\.\d+){1,3}\b|"
    r"\b\d+\.\d+\.\d+\b)",
    re.IGNORECASE,
)
_HIGH_IMPACT_TECHNICAL_MARKERS = re.compile(
    r"(?:安全漏洞|漏洞|security|vulnerability|重大故障|中断|outage|大规模落地|大规模采用|"
    r"生产部署|成本降低|成本|性能提升|performance|latency|adoption|breaking change|"
    r"重大版本|架构变化|migration|compatibility|enterprise|production|availability|"
    r"可用范围|访问门槛|主流产品|稳定性|reliability|major release)",
    re.IGNORECASE,
)
```

Then update `is_editorially_eligible` to:

```python
    if _ACADEMIC_MARKERS.search(text):
        return False
    if _LOW_VALUE_TECHNICAL_MARKERS.search(text) and not _HIGH_IMPACT_TECHNICAL_MARKERS.search(text):
        return False
    if _DEVELOPER_ECOSYSTEM_MARKERS.search(text) and not (
        _HIGH_IMPACT_TECHNICAL_MARKERS.search(text) and _CONCRETE_IMPACT_MARKERS.search(text)
    ):
        return False
    if (
        raw.source_id.startswith("github-")
        and _GITHUB_RELEASE_MARKERS.search(text)
        and not _HIGH_IMPACT_TECHNICAL_MARKERS.search(text)
    ):
        return False
    return not (_CORPORATE_ONLY_MARKERS.search(text) and not _CONCRETE_IMPACT_MARKERS.search(text))
```

- [ ] **Step 5: Run focused editorial tests and verify they pass**

Run:

```bash
.venv/bin/pytest tests/test_editorial_filter.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/ai_news/pipeline/editorial.py tests/test_editorial_filter.py
git commit -m "feat: relax high-impact technical filtering"
```

## Task 3: Align LLM Prompt With the New Editorial Contract

**Files:**
- Modify: `src/ai_news/llm/prompt.py`
- Modify: `tests/test_llm_prompt.py`

- [ ] **Step 1: Write the failing prompt contract test**

Add this test to `tests/test_llm_prompt.py`:

```python
def test_prompt_allows_high_quality_technical_items_without_allowing_low_quality_fillers() -> None:
    prompt = build_analysis_prompt([_candidate("technical")])

    assert "SDK/API、开发框架、代码工具必须排除" not in prompt
    assert "SDK/API、开发框架、代码工具和小版本修复" not in SYSTEM_PROMPT
    assert "高质量、可执行、影响明确的技术内容" in prompt
    assert "高质量、可执行、影响明确的技术内容" in SYSTEM_PROMPT
    assert "纯学术" in prompt
    assert "弱 benchmark" in prompt
    assert "小版本修复" in prompt
    assert "泛教程" in prompt
    assert "不合格内容不得凑数" in prompt
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
.venv/bin/pytest tests/test_llm_prompt.py::test_prompt_allows_high_quality_technical_items_without_allowing_low_quality_fillers -q
```

Expected: FAIL because the current prompt contains absolute rejection for SDK/API/framework/code-tool content and does not include the new high-quality technical wording.

- [ ] **Step 3: Write the minimal prompt implementation**

In `src/ai_news/llm/prompt.py`, replace the system prompt sentence:

```python
拒绝论文、科研、模型原理、benchmark、SDK/API、开发框架、代码工具和小版本修复。
```

with:

```python
拒绝纯学术、弱 benchmark、小版本修复、泛教程和缺少事实支撑的营销内容。
只有高质量、可执行、影响明确的技术内容才可保留，例如成本、性能、稳定性、生产部署、广泛采用、破坏性变更、安全、可用范围或主流产品发布。
```

In `build_analysis_prompt`, replace:

```python
论文、科研、模型原理、SDK/API、开发框架、代码工具、小版本修复、没有新事实的观点和营销内容必须排除。
```

with:

```python
纯学术、弱 benchmark、小版本修复、泛教程、没有新事实的观点和营销内容必须排除。
SDK/API、开发框架和代码工具只有在高质量、可执行、影响明确时才可保留，例如成本、性能、稳定性、生产部署、广泛采用、破坏性变更、安全、可用范围或主流产品发布。
```

- [ ] **Step 4: Run focused prompt tests and verify they pass**

Run:

```bash
.venv/bin/pytest tests/test_llm_prompt.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/ai_news/llm/prompt.py tests/test_llm_prompt.py
git commit -m "feat: align prompt with balanced editorial policy"
```

## Task 4: Full Verification and Historical Replay

**Files:**
- Read: `docs/superpowers/specs/2026-08-24-balanced-editorial-relaxation-design.md`
- Read: `content/2026-08-20.md`
- Read: `content/2026-08-21.md`
- Read: `content/2026-08-22.md`

- [ ] **Step 1: Run full automated verification**

Run:

```bash
.venv/bin/pytest --cov=ai_news --cov-report=term-missing --cov-fail-under=85
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python scripts/check_no_secrets.py
```

Expected: pytest passes with coverage >= 85, Ruff passes, format check passes, and secret scan reports no findings.

- [ ] **Step 2: Confirm CLI replay commands**

Run:

```bash
.venv/bin/python -m ai_news --help
.venv/bin/python -m ai_news generate --help
.venv/bin/python -m ai_news build --help
```

Expected: help output lists `generate --root . --date YYYY-MM-DD` and `build --root . --output .preview/ai-news`.

- [ ] **Step 3: Replay 2026-08-20, 2026-08-21, and 2026-08-22 with the existing local configuration**

Run each command separately and read the exit status:

```bash
.venv/bin/python -m ai_news generate --root . --date 2026-08-20
.venv/bin/python -m ai_news generate --root . --date 2026-08-21
.venv/bin/python -m ai_news generate --root . --date 2026-08-22
```

Expected when local `LLM_*` environment values are present: each command exits 0 and generated reports preserve or increase useful candidate counts relative to the old 36-hour evidence.

Expected when local `LLM_API_KEY` is not present because the key only exists in GitHub Secrets: commands fail with `configuration_error: Error`; do not create or paste a local key. In that case, verify the local candidate-window and filter behavior through tests, then use the existing GitHub Actions workflow after the branch is pushed or merged to perform real LLM regeneration.

- [ ] **Step 4: Build and validate the static site if replay produced local output**

Run:

```bash
.venv/bin/python -m ai_news build --root . --output .preview/ai-news
.venv/bin/python scripts/validate_site.py .preview/ai-news --base-path /ai-news/
```

Expected: validation passes against the current repository data. If local generation could not run because `LLM_API_KEY` is only in GitHub Secrets, this build validates existing repository data; real regenerated content must still be validated by Actions after merge.

- [ ] **Step 5: Final implementation review**

Run:

```bash
git diff --check
git status --short
git log --oneline origin/main..HEAD
```

Expected: no whitespace errors, only intended files changed, and commits are limited to the spec, plan, and scheme B implementation.
