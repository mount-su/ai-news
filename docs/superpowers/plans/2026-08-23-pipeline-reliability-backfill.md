# Pipeline Reliability And Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 允许 LLM 只返回真正合格的部分候选，正确按指定历史日期取数，并回补 2026-08-17 至 2026-08-23 的缺失日报。

**Architecture:** 保留现有确定性编辑过滤和严格字段校验。LLM 层从“必须返回全部指定数量”改为“返回 1–9 条有效子集”，避免因不凑数而被误判为格式失败；CLI 对历史 `--date` 传入该日北京时间结束时作为内容窗口截止点，使补跑读取对应日期而不是当前日期。修复合并后通过 GitHub Actions 按日期顺序回补并部署。

**Tech Stack:** Python 3.12、Pydantic、httpx、pytest、GitHub Actions、GitHub Pages。

---

### Task 1: 允许高质量子集输出

**Files:**
- Modify: `tests/test_openai_chat_client.py`
- Modify: `tests/test_anthropic_client.py`
- Modify: `tests/test_llm_prompt.py`
- Modify: `src/ai_news/llm/base.py`
- Modify: `src/ai_news/llm/prompt.py`

- [x] 新增失败测试：12 个候选时返回 3 个合法条目应直接成功且不触发修复请求。
- [x] 新增提示词测试：文本必须声明选择 1 至 9 条，不得要求恰好 9 条。
- [x] 运行目标测试并确认因现有 `missing` 校验和“恰好”提示失败。
- [x] 删除数量下限校验，保留至少 1 条、最多 9 条、候选 ID、字段、来源上限与敏感信息校验。
- [x] 修改提示词为“选择 1 至 9 条；质量不足可以少选”。
- [x] 运行 OpenAI、Anthropic 与提示词测试并确认通过。

### Task 2: 按历史日期建立补跑窗口

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `src/ai_news/cli.py`

- [x] 新增失败测试：指定过去日期时，CLI 应向 `run_daily` 传入该日 `23:59:59.999999 Asia/Shanghai` 对应的 UTC 时间。
- [x] 新增测试：不指定日期时不覆盖流水线当前时间。
- [x] 运行目标测试并确认现有 CLI 未传 `now` 而失败。
- [x] 实现历史日期截止时间计算，仅在 `--date` 早于北京时间当天时传入 `now`。
- [x] 运行 CLI 与流水线测试并确认通过。

### Task 3: 完整门禁与发布

**Files:**
- Modify: `docs/superpowers/plans/2026-08-23-pipeline-reliability-backfill.md`

- [x] 运行全量 pytest、Ruff、格式校验、密钥扫描、离线构建和站点校验。
- [ ] 提交分支、创建 PR，等待 CI 通过后合并。
- [ ] 依次触发 2026-08-17 至 2026-08-23 的 workflow_dispatch；每个日期等待生成、提交、构建和部署完成后再触发下一日。
- [ ] 核对 `data/index.json`、每日日报 JSON、分类页和 Pages 最后修改时间；无合格内容的日期记录为已运行但不伪造资讯。
