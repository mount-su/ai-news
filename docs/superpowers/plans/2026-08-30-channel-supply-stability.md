# Channel Supply Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix daily generation failures caused by slightly invalid LLM output, then broaden high-quality Agent, AI tools, and open-source supply without reintroducing low-value technical noise.

**Architecture:** Keep the existing fetch → normalize → editorial filter → LLM → static site flow. Make the LLM parser tolerant of common provider formatting drift while preserving candidate-ID, secret-safety, and distribution validation. Register only direct RSS/GitHub sources that already fit existing collectors, and keep arXiv disabled.

**Tech Stack:** Python 3.12+, Pydantic 2, HTTPX, feedparser, pytest, Ruff, GitHub Actions, GitHub Pages.

---

### Task 1: Harden LLM Analysis Parsing

**Files:**
- Modify: `tests/test_openai_chat_client.py`
- Modify: `src/ai_news/llm/base.py`

- [ ] **Step 1: Write failing parser tests**

Add tests proving the analyzer accepts a JSON array surrounded by provider text, an object wrapper with `items`, numeric and boolean strings, and slightly overlong fields.

- [ ] **Step 2: Verify tests fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_openai_chat_client.py::test_openai_accepts_common_provider_json_formatting_drift -q
```

Expected: FAIL because the current parser requires a bare strict JSON array.

- [ ] **Step 3: Implement minimal parser tolerance**

Extract the first balanced JSON array/object from text, unwrap known list keys, coerce safe primitive strings, clamp long text fields to schema limits, and keep duplicate/unknown/missing candidate checks unchanged.

- [ ] **Step 4: Verify focused tests pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_openai_chat_client.py -q
```

Expected: PASS.

### Task 2: Broaden Quality Sources for Sparse Channels

**Files:**
- Modify: `sources/feeds.yaml`
- Modify: `tests/test_config.py`
- Modify: `README.md`
- Modify: `tests/test_readme_contract.py`

- [ ] **Step 1: Write failing source-manifest expectations**

Update manifest tests to expect enabled Reddit RSS plus vetted direct sources for Agent, AI tools, and open-source coverage.

- [ ] **Step 2: Verify source test fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py::test_repository_source_manifest_contains_only_verified_sources -q
```

Expected: FAIL because sources are not registered yet.

- [ ] **Step 3: Add sources with existing collectors**

Enable `reddit-ai`, add direct RSS sources for Sourcegraph, Cursor, Ollama, Product Hunt AI, and GitHub Blog AI. Do not restore the removed GitHub Release source list; the existing editorial filter keeps patch-level noise out.

- [ ] **Step 4: Verify config and docs**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_readme_contract.py -q
```

Expected: PASS.

### Task 3: Verify End-to-End and Backfill

**Files:**
- Generated after workflow or local real run: `data/YYYY/MM/YYYY-MM-DD.json`, `content/YYYY-MM-DD.md`, `data/index.json`

- [ ] **Step 1: Run full local gates**

Run:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_no_secrets.py
```

Expected: PASS.

- [ ] **Step 2: Push branch and open PR**

Push `codex/channel-supply-stability` and open a pull request.

- [ ] **Step 3: After merge, backfill missing days**

Run `Daily AI news` on `main` for missing dates in order, starting with 2026-08-29 and 2026-08-30.

- [ ] **Step 4: Verify Pages**

Check the deployed GitHub Pages site for latest generated time, category pages, archive pages, and responsive layout.
