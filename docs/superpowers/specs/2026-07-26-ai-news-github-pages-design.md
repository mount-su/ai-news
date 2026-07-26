# AI 情报雷达：GitHub Pages + GitHub Actions 设计

## 1. 目标

在公开仓库 `mount-su/ai-news` 中构建一个无需自有服务器的中文 AI 资讯站：

- GitHub Actions 每天北京时间 08:17 自动采集全球 AI 资讯。
- 系统从官方 RSS、Atom、GitHub Releases 和 arXiv 获取候选内容。
- LLM 对候选内容进行中文摘要、分类、重要性评分和价值判断。
- 每天发布约 10–20 条资讯，并突出最重要的 3 条。
- JSON 和 Markdown 保存在 Git 仓库中，形成可追溯的历史。
- Jinja 生成纯静态网站，由 GitHub Pages 托管。
- 整个流程不需要自建服务器或外部数据库。

预期线上地址为：

```text
https://mount-su.github.io/ai-news/
```

## 2. 范围

### 2.1 本期包含

- 官方 AI 新闻源、精选 GitHub 项目和 arXiv 论文的每日采集。
- 跨来源、跨日期去重。
- Anthropic 协议兼容的 LLM 适配器。
- 中文摘要、分类、重要性、价值判断、标签、官方标记、营销风险和跟踪信号。
- 首页、分类筛选、客户端搜索、日期归档和日报页面。
- GitHub Actions 定时生成、数据提交、静态构建和 Pages 部署。
- 离线测试样本、本地完整验证、真实工作流和线上页面验收。

### 2.2 本期不包含

- 用户登录、收藏、评论或个性化推荐。
- 动态后端、数据库、全文搜索服务或消息推送。
- 媒体文章全文保存。
- 对任意网页的通用深度爬虫。
- 分钟级实时更新。
- Cloudflare Workers、D1 或 R2。

## 3. 总体架构

系统使用单一 Python 技术栈完成采集、处理、LLM 调用和静态站点生成。

```text
GitHub Actions 定时触发
        ↓
资讯采集器
RSS / Atom / GitHub Releases / arXiv
        ↓
标准化与去重
统一字段 / 规范 URL / 内容指纹
        ↓
筛选与 LLM 分析
分类 / 摘要 / 评分 / 价值判断
        ↓
持久化
data/*.json + content/*.md
        ↓
Jinja 静态站点生成器
        ↓
dist/
        ↓
GitHub Pages
```

### 3.1 模块边界

- `sources/feeds.yaml`：维护来源 URL、类型、分类、可信度、启停状态和项目仓库名。
- `src/ai_news/collectors/`：RSS、Atom、GitHub Releases 和 arXiv 采集器。
- `src/ai_news/pipeline/`：标准化、去重、预筛选、排序和每日条数控制。
- `src/ai_news/llm/`：Anthropic 协议兼容的模型适配器和结构化响应校验。
- `src/ai_news/storage/`：历史索引、JSON、Markdown 和原子写入。
- `src/ai_news/site/`：Jinja 模板、站点构建逻辑、CSS 和客户端 JavaScript。
- `src/ai_news/cli.py`：采集、构建、离线演示和指定日期补跑的命令入口。
- `tests/`：单元测试、固定网络样本和离线端到端测试。
- `.github/workflows/ci.yml`：代码质量、测试、工作流与站点结构校验。
- `.github/workflows/daily-news.yml`：每日生成、数据提交和 Pages 部署。
- `data/`：结构化日报和全局索引。
- `content/`：可阅读的 Markdown 日报。
- `dist/`：临时构建产物，不提交 Git，只上传为 Pages artifact。

每个模块只通过明确的数据模型或函数接口协作。采集器不负责写文件，LLM 适配器不负责排序，模板不执行业务逻辑。

## 4. 数据模型

### 4.1 原始候选

采集器统一输出：

```json
{
  "source_id": "openai-news",
  "source_name": "OpenAI",
  "source_type": "rss",
  "title": "Original title",
  "url": "https://example.com/article",
  "published_at": "2026-07-26T00:00:00Z",
  "excerpt": "Source-provided excerpt",
  "is_official_source": true
}
```

### 4.2 最终资讯

LLM 分析并通过 Schema 校验后形成：

```json
{
  "id": "sha256-prefix",
  "title": "中文标题",
  "original_title": "Original title",
  "url": "https://example.com/article",
  "source": "OpenAI",
  "published_at": "2026-07-26T00:00:00Z",
  "category": "大模型",
  "summary": "中文事实摘要",
  "importance": 8,
  "why_it_matters": "为什么值得关注",
  "tags": ["模型", "官方发布"],
  "is_official": true,
  "marketing_risk": "low",
  "tracking_signal": "后续应关注的信号"
}
```

约束：

- `importance` 为 1–10 的整数。
- `marketing_risk` 只能是 `low`、`medium` 或 `high`。
- 分类只能是 `大模型`、`Agent`、`Coding Agent`、`AI 工具`、`开源项目` 或 `论文研究`。
- URL 必须是允许来源下的 HTTPS 地址。
- 摘要与判断不得包含来源文章未支持的具体事实。

日报文件还包含 Schema 版本、生成时间、来源状态、候选数、收录数、降级状态和模型标识。

## 5. 首批数据源

### 5.1 RSS / Atom

- OpenAI News
- Google AI Blog
- Google DeepMind Blog
- Hugging Face Blog
- arXiv `cs.AI`、`cs.CL`、`cs.LG`

Anthropic 等没有稳定公开 Feed 的站点不在首批通用网页抓取范围内；可在后续通过稳定官方接口或经过测试的专用采集器补充。

### 5.2 GitHub Releases

- `openai/codex`
- `anthropics/claude-code`
- `google-gemini/gemini-cli`
- `langchain-ai/langchain`
- `crewAIInc/crewAI`
- `run-llama/llama_index`
- `huggingface/transformers`
- `vllm-project/vllm`
- `ollama/ollama`
- `cline/cline`

`microsoft/autogen` 与 `Aider-AI/aider` 可作为低频来源保留，但不提高其来源权重。

## 6. 每日数据流

1. `schedule` 在 `Asia/Shanghai` 时区每天 08:17 触发；`workflow_dispatch` 支持手动和指定日期补跑。
2. 读取最近 30 天历史索引，构建跨日期去重集合。
3. 并发抓取过去约 36 小时的数据。
4. 统一标题、时间、来源、分类和规范 URL。
5. 使用规范 URL、标题指纹和相似标题去重。
6. 根据来源可信度、时效、关键词和官方属性预筛选。
7. 最多选择 30 条候选，分批交给 LLM 分析。
8. 校验结构化结果，最终保留 10–20 条并选择今日最重要的 3 条。
9. 原子写入：

   ```text
   data/YYYY/MM/YYYY-MM-DD.json
   content/YYYY-MM-DD.md
   data/index.json
   ```

10. 生成首页、日报、分类、归档和搜索索引。
11. 提交新增 JSON/Markdown 到默认分支。
12. 在同一个工作流中上传 `dist/` artifact 并部署 Pages。

同一天重复执行时更新同一日报。时间内部使用 UTC，页面显示北京时间。

## 7. GitHub Actions 设计

### 7.1 触发路径

- `schedule` 和手动运行：采集、分析、保存、提交、构建、部署。
- 推送到 `main`：只用已有数据执行 CI、构建和部署，不重复调用 LLM。

定时项显式使用 `cron: "17 8 * * *"` 和 `timezone: "Asia/Shanghai"`，避免用 UTC
换算代替已批准的北京时间语义。手动入口的可选 `date` 使用 `YYYY-MM-DD`，CLI
不带日期时按上海时区当天运行。

每日生成工作流使用 `concurrency` 防止多个任务同时写数据。机器人通过默认
`GITHUB_TOKEN` 提交生成内容；同一工作流直接继续 Pages 部署，不依赖机器人提交触发
第二个工作流。提交前执行敏感信息扫描。若远端 `main` 在运行期间前进，提交步骤先
fetch、rebase，再 push，竞争失败最多重试 3 次；rebase 冲突或重试耗尽时失败退出，
不得强推。

首次 Pages 部署前必须通过手动工作流真实生成至少一份日报。空数据仓库仅 push
`main` 不会调用 LLM，也不能生成可用站点。

### 7.2 权限

- CI 和普通构建：`contents: read`。
- 生成数据提交：`contents: write`。
- Pages 部署：`pages: write` 和 `id-token: write`。

权限按作业声明，不给整个工作流不必要的写权限。

第三方 Actions 必须固定到不可变 commit SHA，并在配置中保留版本来源注释。CI 对
workflow 执行语法检查；如检查器版本尚未识别 GitHub 已支持的 `schedule.timezone`
字段，只允许忽略这一条精确的旧 Schema 诊断，不得整体跳过 workflow lint。

### 7.3 LLM 配置

GitHub Secret：

- `ANTHROPIC_AUTH_TOKEN`

GitHub Repository Variables：

- `ANTHROPIC_BASE_URL`
- `ANTHROPIC_DEFAULT_OPUS_MODEL`
- `ANTHROPIC_AUTH_SCHEME`

站点路径 `SITE_BASE_PATH=/ai-news/` 固定写在每日工作流中，不作为 Repository Variable
交给部署时修改。这样项目子路径、静态校验和 Pages URL 使用同一契约。

适配器不得把任何密钥写入错误信息、日报、构建产物或日志。提供 `.env.example`，其中只有占位符。

所提供的 Base URL 具有 Coding Plan 特征。生产工作流启用前必须确认该凭证允许无人值守
的非编码资讯摘要；若服务条款只允许 AI 编码工具，则必须替换为允许普通服务端调用和
摘要任务的标准模型 API 凭证。不得通过伪装客户端或修改请求标识绕过 provider 限制。
实现保持 Anthropic 协议兼容，不与具体计费套餐耦合。

## 8. 页面与视觉

站点名称为“AI 情报雷达”。

### 8.1 首页

- 顶部导航：站点名称、更新时间、首页、分类和归档。
- 今日概览：采集来源数、候选数、收录数和最后成功时间。
- 今日最重要的 3 条：使用大卡片突出结论、重要性和价值。
- 持续跟踪信号：展示值得后续观察的模型、产品和项目。
- 分类筛选与搜索。
- 按重要性和发布时间排序的资讯卡片。
- 数据来源、生成方式和 GitHub 仓库链接。

### 8.2 视觉

- 深墨蓝背景、电光青主色和少量琥珀色高重要性提示。
- 清晰网格、细边框和状态标记，形成情报终端气质。
- 不使用大面积渐变、夸张玻璃效果或干扰阅读的动画。
- 桌面端双栏或三栏，移动端单栏。
- 不只依赖颜色表达重要性。

### 8.3 卡片与交互

卡片显示中文标题、原始标题、来源、时间、原文、摘要、重要性、价值判断、标签、官方标记、营销风险和跟踪信号。

- 默认显示摘要，长分析可展开。
- 搜索和分类筛选完全在浏览器本地执行。
- JavaScript 关闭时仍能阅读静态内容。
- 使用语义化 HTML、键盘可操作控件和可见焦点样式。
- 尊重 `prefers-reduced-motion`。
- 所有资源和内部链接支持 `/ai-news/` 项目子路径。

## 9. 异常处理

### 9.1 数据源

- 数据源独立运行，单源失败不影响其他来源。
- 超时、`429` 和 `5xx` 最多重试 3 次并指数退避。
- 记录每个来源的状态、条数、错误类型和耗时。
- 有效资讯少于 5 条时不发布新日报。
- 至少 5 条时允许降级发布，并记录失败来源。

### 9.2 LLM

- 对限流和服务端错误重试，不重试鉴权失败和无效模型。
- 要求固定 JSON Schema。
- 解析失败时只执行一次结构修复；再次失败则舍弃该批次。
- 模型整体不可用时不提交、不部署空白日报。
- 输入明确标记为不可信外部资料，忽略其中指令。
- 对单条输入和整批输入设置长度上限。

### 9.3 一致性

- 临时文件全部验证通过后才原子替换。
- JSON Schema、日期、URL、评分范围和必填字段通过后才提交。
- 构建或验证失败时不覆盖最后一次成功部署。
- 首页显示最后成功时间，不把失败运行伪装成最新内容。

## 10. 安全边界

- 只访问 `feeds.yaml` 中允许的 HTTPS 来源。
- 不执行或下载来源给出的代码、脚本和二进制文件。
- 对来源 HTML 清理，模板默认转义全部文本。
- 密钥只通过 GitHub Secrets 和进程环境传递。
- Actions 日志、测试快照和错误对象不得包含密钥。
- 聊天、截图、日志或本地环境中的密钥不得进入仓库、测试快照或 Git 历史。
- 生成前后执行敏感信息模式扫描。
- 不保存媒体全文，仅保存必要元数据、短摘录和原创分析。

## 11. 测试

### 11.1 单元测试

- RSS、Atom、GitHub Release 和 arXiv 正常与异常样本。
- 缺失字段、异常时间、预发布和草稿过滤。
- URL 规范化、跨来源和跨日期去重。
- 分类、来源权重、排序和数量限制。
- LLM 请求、超时、限流、鉴权失败、无效 JSON 和 Schema。
- 输入长度和提示注入隔离。
- 原子写入、同日重跑和历史索引。
- `/ai-news/` 下资源和内部链接。
- HTML 转义和客户端搜索数据。

### 11.2 离线端到端

固定 RSS/API 样本和模拟 LLM 响应完成：

```text
采集 → 去重 → 分析 → JSON/Markdown → dist/ → HTML/链接检查
```

该测试不访问网络和真实模型，保证 CI 稳定。

### 11.3 质量门禁

- `ruff check`
- `ruff format --check`
- `pytest`
- JSON/YAML 校验
- GitHub Actions 语法检查
- 静态站点结构与内部链接检查
- 敏感信息模式扫描

## 12. 上线验收

只有以下证据全部成立才算完成：

1. 本地质量门禁与完整测试通过。
2. 固定样本生成的站点通过桌面端和移动端检查。
3. 浏览器控制台无 JavaScript 错误，站点资源无 404。
4. `mount-su/ai-news` 为公开仓库，默认分支是 `main`。
5. Pages 来源配置为 GitHub Actions。
6. Repository Secret 和 Variables 已配置，仓库和日志无真实密钥。
7. 手动每日工作流完成真实采集、真实 LLM 分析、历史数据提交和 Pages 部署。
8. `https://mount-su.github.io/ai-news/` 返回 200。
9. 页面显示本次生成日期、今日重点和有效原文链接。
10. 搜索、分类、归档、展开分析和移动端布局可用。
11. 定时配置为北京时间每天 08:17。
12. 工作流失败时，最后一次成功站点仍保持可访问。

## 13. 后续演进

当 Git 仓库存储、静态搜索或每日任务时长成为瓶颈后，再评估 Cloudflare Pages、Workers、D1 和 R2。该演进不属于本期实现范围。
