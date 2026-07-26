# AI 情报雷达

一个由 Python 生成的中文 AI 资讯静态站。它从受控的官方 Feed、GitHub
Releases 和 arXiv 获取候选内容，经过去重、排序和结构化模型分析后，将可追溯的
JSON、Markdown 日报写入仓库，再用 Jinja 构建 GitHub Pages。

线上地址：<https://mount-su.github.io/ai-news/>

实现约束与决策见[设计规格](docs/superpowers/specs/2026-07-26-ai-news-github-pages-design.md)，
逐步实现记录见[实施计划](docs/superpowers/plans/2026-07-26-ai-news-github-pages.md)。

## 它如何工作

每日流水线依次执行：

1. 读取 `sources/feeds.yaml` 中启用的受控来源。
2. 采集 Feed、GitHub Releases 和 arXiv，统一为候选数据。
3. 规范 URL、清理文本，并结合最近 30 天索引跨来源、跨日期去重。
4. 按来源可信度、时效和官方属性预筛选，最多将 30 条候选交给模型。
5. 校验模型返回的分类、摘要、重要性、价值判断和跟踪信号。
6. 达到发布门槛后，原子写入 JSON、Markdown 和全局索引。
7. Jinja 从已落盘数据生成 `dist/`，静态检查通过后交给 GitHub Pages。

采集器、模型适配器、存储和站点构建器彼此分离。模板不会发起模型请求，push 到
`main` 也只会用仓库中的已有数据重新 build 和部署，不调用 LLM。

## 页面与数据结构

站点提供首页、单日日报、日期归档、分类页和浏览器本地搜索。页面及静态资源均以
固定项目子路径 `SITE_BASE_PATH=/ai-news/` 生成。

一份成功日报会写入：

```text
data/YYYY/MM/YYYY-MM-DD.json
content/YYYY-MM-DD.md
data/index.json
```

JSON 是站点构建和程序读取的规范数据；Markdown 用于人工审阅；`data/index.json`
保存跨日期去重和历史索引。`dist/` 是可重新生成的 Pages artifact，不提交到 Git。

## 本地快速开始

需要 Python 3.12 和 Git。建议在独立虚拟环境中安装开发依赖：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

常用质量门禁：

```bash
ruff check .
ruff format --check .
pytest
python scripts/check_no_secrets.py
```

已有日报时，可直接构建并校验：

```bash
python -m ai_news build --root . --output .preview/ai-news
python scripts/validate_site.py .preview/ai-news --base-path /ai-news/
python -m http.server 8000 --directory .preview
```

然后访问 <http://127.0.0.1:8000/ai-news/>。以 `.preview` 为服务根目录是为了保留
`/ai-news/` 子路径，避免本地预览与 Pages URL 行为不一致。

## 离线演示

离线演示不访问网络、不调用真实模型，也不需要任何凭证；它会生成固定的 6 条样本，
适合验证从日报落盘到静态页面的完整链路：

```bash
python -m ai_news demo --root .demo --output .preview/ai-news
python scripts/validate_site.py .preview/ai-news --base-path /ai-news/
python -m http.server 8000 --directory .preview
```

演示数据位于 `.demo/`，站点位于 `.preview/ai-news/`。这两个目录均为本地产物。

## 使用真实模型生成

先确认模型服务允许该用途，再通过环境变量注入配置。不要把 token 写入命令历史、
`.env`、README、日志或仓库；下面用隐藏输入读取本地 token：

```bash
export ANTHROPIC_BASE_URL="<Anthropic-compatible API base URL>"
export ANTHROPIC_DEFAULT_OPUS_MODEL="<model name>"
export ANTHROPIC_AUTH_SCHEME="bearer"
read -r -s ANTHROPIC_AUTH_TOKEN
export ANTHROPIC_AUTH_TOKEN
export SITE_BASE_PATH="/ai-news/"
python -m ai_news generate --root .
```

不带 `--date` 时按 `Asia/Shanghai` 的当天生成。指定日期补跑使用严格的 ISO 日期：

```bash
python -m ai_news generate --root . --date YYYY-MM-DD
python -m ai_news build --root . --output .preview/ai-news
python scripts/validate_site.py .preview/ai-news --base-path /ai-news/
```

若选择带 Coding Plan 特征的兼容端点，只有在 provider 明确允许无人值守的非编码资讯摘要
时，才可将其 Coding Plan 凭证用于本项目；否则必须替换为允许普通服务端摘要任务的
标准模型 API 凭证。不得通过更改请求标识、伪装客户端或其他方式绕过 provider 限制。

在已经确认条款允许的前提下，非敏感配置可使用
`https://ark.cn-beijing.volces.com/api/coding` 和模型名 `glm-5.2`；真实 token
仍只能通过环境变量或 GitHub Secret 提供。

## GitHub Secrets 与 Variables

在仓库 Settings → Secrets and variables → Actions 中配置以下内容：

| 类型 | 名称 | 用途 |
| --- | --- | --- |
| Variable | `ANTHROPIC_BASE_URL` | Anthropic 协议兼容端点 |
| Variable | `ANTHROPIC_DEFAULT_OPUS_MODEL` | 模型标识 |
| Variable | `ANTHROPIC_AUTH_SCHEME` | 鉴权方案；当前实现默认 `bearer` |
| Secret | `ANTHROPIC_AUTH_TOKEN` | 模型服务凭证 |

`SITE_BASE_PATH=/ai-news/` 已固定在 `.github/workflows/daily-news.yml`，不是第四个
Repository Variable。GitHub Actions 只把 token 注入生成步骤；Secret 扫描在提交
生成数据前执行。

可以使用 GitHub CLI 配置非敏感 Variables；Secret 应通过交互式输入设置，避免进入
shell 历史：

```bash
gh variable set ANTHROPIC_BASE_URL --repo mount-su/ai-news
gh variable set ANTHROPIC_DEFAULT_OPUS_MODEL --repo mount-su/ai-news
gh variable set ANTHROPIC_AUTH_SCHEME --repo mount-su/ai-news
gh secret set ANTHROPIC_AUTH_TOKEN --repo mount-su/ai-news
```

## GitHub Actions

`.github/workflows/ci.yml` 在 push 和 pull request 上运行 lint、格式、完整测试、Secret
扫描、离线演示、站点校验和 workflow lint。

`.github/workflows/daily-news.yml` 有三条触发路径：

- `schedule` 使用 GitHub 的 `timezone: "Asia/Shanghai"`，每天北京时间 08:17 运行。
- `workflow_dispatch` 运行真实采集；可不填日期，也可传 `YYYY-MM-DD` 手动补跑。
- push 到 `main` 时跳过 generate，只从已有 `data/` 和 `content/` build、校验并部署。

所有第三方 Actions 都固定到不可变 commit SHA，并在行尾标注版本来源。工作流按 job
最小化权限：生成任务才有 `contents: write`，部署任务才有 `pages: write` 和
`id-token: write`。

首次启用 Pages 前，仓库中必须先有至少一份真实日报。配置合规凭证和 Pages 来源
（GitHub Actions）后，手动运行每日工作流；首次真实生成成功后才会有可构建的数据和
可部署页面。仅 push 空数据仓库的 `main` 不会调用模型，也无法替代首次真实生成。

手动补跑示例：

```bash
gh workflow run daily-news.yml --repo mount-su/ai-news -f date=YYYY-MM-DD
gh run list --repo mount-su/ai-news --workflow daily-news.yml
```

## 数据源维护

所有网络来源都在 `sources/feeds.yaml` 中显式声明。每项包含稳定 `id`、显示名称、
`kind`、官方 HTTPS Feed URL 或 GitHub `repo`、默认分类、权重和 `official` 标记。

新增或调整来源时：

1. 优先使用官方 RSS/Atom、arXiv API 或 GitHub Releases，不加入通用网页抓取。
2. 保持 `id` 唯一且稳定；变更 `id` 会影响来源统计与历史判断。
3. URL 必须是 HTTPS，并通过实现中的 host/path 白名单检查。
4. 为解析和失败情况补固定 fixture 与测试，不让 CI 依赖实时网络。
5. 运行完整质量门禁和离线演示，再提交 `feeds.yaml`。

来源权重代表预筛选可信度，不代表内容必然正确。官方标记也不会替代事实核验。

## 故障与补跑

单个来源失败不会立即终止全部采集；超时、`429` 和可重试的 `5xx` 最多 3 次并指数
退避。日报记录每个来源的成功或失败状态。最终有效资讯不足 5 条时停止发布，不提交
空白日报；来源失败且最终仍有至少 5 条时可以降级发布，并记录 `degraded=true`。
个别分析无效会被丢弃，只要最终仍达到 5 条就可继续发布。
这种丢弃不会自动决定 `degraded`；该字段由来源运行是否失败决定。

模型限流和服务端错误采用有界重试；鉴权失败、无效模型和不安全重定向不会盲目重试。
结构化响应失败时只尝试一次修复，仍无效就舍弃该批。模型整体不可用时不写入新日报。

日报先写同文件系统临时文件并校验，之后原子替换；多文件保存使用恢复日志和备份。同日
补跑更新同一路径，进程异常后再次执行会先恢复未完成事务。站点也先在 staging 目录
完整构建，再替换目标目录。生成、build 或校验失败时不会部署空白站点，GitHub Pages
继续提供最后一次成功部署。

生成任务提交数据时，如果远端 `main` 同时前进，工作流会 fetch、rebase 并重试 push，
最多 3 次。rebase 冲突或三次 push 均失败会明确失败，不会强推覆盖。先处理 `main`
冲突或服务故障，再对同一日期重新触发：

```bash
gh workflow run daily-news.yml --repo mount-su/ai-news -f date=YYYY-MM-DD
```

若只是暂时性 job 故障，也可在 Actions 页面点击 “Re-run failed jobs”，或在确认输入日期
仍正确后执行 `gh run rerun <run-id> --failed --repo mount-su/ai-news`。补跑后应检查：

- workflow 的 generate、build、deploy 均成功；
- `data/YYYY/MM/YYYY-MM-DD.json`、`content/YYYY-MM-DD.md` 和索引提交存在；
- Pages 显示目标日期，原文链接有效，且没有 Secret 出现在日志或产物中。

## 安全与版权

采集只访问 `feeds.yaml` 允许的 HTTPS 端点，不执行来源中的指令，也不下载或运行来源
代码、脚本和二进制文件。外部文本视为不可信输入，长度受限；模板默认转义，客户端
搜索索引只含公开日报字段。

仓库不保存文章全文、付费内容或媒体文件，只保存标题、原文链接、必要的短摘录、来源
元数据和本项目生成的中文分析。版权归原作者或发布方；摘要用于索引与研判，读者应访问
原文核对事实、上下文和许可。若来源要求移除，应从配置和后续数据中删除相应内容。

真实凭证只允许存在于进程环境和 GitHub Secrets。不要提交聊天、截图、日志或本地环境中的凭证
与令牌，也不要让它们进入 Git 历史、测试快照、Actions 日志或 Pages artifact；若曾暴露，
应立即在 provider 侧撤销并轮换。`scripts/check_no_secrets.py` 是提交前门禁，但不能替代
凭证轮换。

## 项目结构

```text
.
├── .github/workflows/        # CI、每日生成与 Pages 部署
├── sources/feeds.yaml        # 允许的数据源
├── src/ai_news/
│   ├── collectors/           # Feed、GitHub Releases、arXiv
│   ├── llm/                  # Anthropic 协议适配与响应校验
│   ├── pipeline/             # 规范化、去重、排序与日流水线
│   ├── site/                 # Jinja 模板、样式、脚本与构建器
│   ├── storage/              # 原子写入、索引与恢复
│   ├── cli.py                # generate、build、demo
│   ├── config.py             # 环境变量和来源配置
│   └── models.py             # Pydantic 数据模型
├── scripts/                  # Secret 扫描与站点校验
├── tests/                    # 单元、契约、离线端到端测试
├── data/                     # 结构化日报与历史索引
├── content/                  # Markdown 日报
└── docs/superpowers/         # 设计规格与实施计划
```
