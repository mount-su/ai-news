# Newspaper Style And Agent Category Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将静态站点改造成正式的深色日报版式，并在展示层把 Agent 与 Coding Agent 合并为 Agent。

**Architecture:** 保留现有 Jinja 页面结构和日报数据模型；用 CSS 重塑报头、版心、编号、栏线和响应式阅读节奏。分类合并只发生在站点 builder 的导航、聚合和输出路径，历史 JSON 仍按原枚举读取。

**Tech Stack:** Python/Jinja 静态构建、CSS、pytest、站点可访问性校验。

---

### Task 1: 合并展示层分类

**Files:**
- Modify: `src/ai_news/site/builder.py`
- Modify: `tests/test_site_builder.py`
- Modify: `tests/test_site_accessibility.py`

- [ ] 写测试，验证 Coding Agent 条目进入 `/categories/agent/`，且不生成 `/categories/coding-agent/`。
- [ ] 实现展示分类映射：`Category.CODING_AGENT` 使用 Agent 的 slug 和标题；保留模型枚举以兼容历史数据。
- [ ] 运行站点 builder 测试与可访问性测试。

### Task 2: 重做日报版式

**Files:**
- Modify: `src/ai_news/site/static/styles.css`
- Modify: `src/ai_news/site/templates/base.html`
- Modify: `src/ai_news/site/templates/index.html`
- Modify: `src/ai_news/site/templates/archive.html`
- Modify: `src/ai_news/site/templates/category.html`

- [ ] 保留现有语义结构和跳过链接，改用深色晚报配色、米白衬线标题、窄体元信息和细栏线。
- [ ] 增加正式报头信息层级：刊名、日期/版次、来源状态和导航。
- [ ] 让首页头条拥有更强的字号、编号和导语层级，其余条目呈日报列表而非圆角卡片堆叠。
- [ ] 让归档页采用版次目录和日期行，分类页沿用同一日报网格。
- [ ] 在 768px 以下切换为单列、缩小边距并保证链接/触控目标不少于 44px。

### Task 3: 验证与发布

**Files:**
- No new files.

- [ ] 运行 `pytest`、`ruff check .`、`ruff format --check .`、密钥扫描和离线站点构建校验。
- [ ] 打开构建产物检查首页、归档页、Agent 分类页和移动端 CSS 断点。
- [ ] 创建 PR，等待 CI 通过后合并。
- [ ] 手动触发 Pages 部署并验证线上首页、归档页和 Agent 分类链接。
