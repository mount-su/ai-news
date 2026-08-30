# AI 情报雷达

一个由 Python 生成的中文 AI 资讯静态日报。它从受控的一手来源、可信媒体和受限
Reddit RSS 获取候选内容，经过去重、质量排序和结构化模型选编后，将可追溯的 JSON、
Markdown 日报与运行记录写入仓库，再用 Jinja 构建 GitHub Pages。

预期 Pages 地址（完成部署后）：<https://mount-su.github.io/ai-news/>

实现约束与决策见[设计规格](docs/superpowers/specs/2026-07-26-ai-news-github-pages-design.md)，
当前模型接入边界见
[Coding Plan 补充设计](docs/superpowers/specs/2026-07-29-ark-coding-plan-design.md)，
模型接入与部署的当前步骤见
[当前实施计划](docs/superpowers/plans/2026-07-29-ark-coding-plan.md)。日报的编辑契约见
[精炼日报设计](docs/superpowers/specs/2026-07-31-concise-editorial-design.md)。厂商扩源和
Reddit 边界见[扩源设计](docs/superpowers/specs/2026-08-23-official-model-vendor-sources-design.md)
及[扩源实施计划](docs/superpowers/plans/2026-08-23-official-model-vendor-sources.md)。

## 它如何工作

每日流水线依次执行：

1. 读取 `sources/feeds.yaml` 中启用的受控来源。
2. 采集官方 Feed、允许列表内的官网页面和受限 Reddit RSS，统一为候选数据。
3. 规范 URL、清理文本，并结合最近 30 天索引跨来源、跨日期去重。
4. 按来源可信度、时效和官方属性预筛选，建立最多 36 条、官方单来源最多 6 条、非官方单来源最多 3 条的候选池。
5. 模型一次性选编 0–7 条，目标 3–7 条；质量优先，可只发布 1–2 条，不为凑数降低门槛。全部不合格时返回空数组，并压缩为“变化、影响、行动”；无效时只允许一次定向修复。
6. 校验入选条数、来源分布、字段长度和候选 ID。
7. 达到发布门槛后，原子写入 JSON、Markdown、全局索引和运行记录；没有合格内容时只写安全运行记录，不生成当期日报。
8. Jinja 从已落盘数据生成 `dist/`，静态检查通过后交给 GitHub Pages。

采集器、模型适配器、存储和站点构建器彼此分离。模板不会发起模型请求，push 到
`main` 也只会用仓库中的已有数据重新 build 和部署，不调用 LLM。

## 页面与数据结构

站点提供首页、单日日报、日期归档和五个固定频道：大模型、Agent、AI 产品、开源生态、
行业政策。`/search/` 提供静态全文搜索与日期、频道、来源筛选，`/sources/` 展示来源的
可访问性、新鲜度和各阶段贡献；`/feed.xml`、`/sitemap.xml`、`/robots.txt` 与
`/404.html` 分别用于订阅、索引和错误回退。页面及静态资源均以固定项目子路径
`SITE_BASE_PATH=/ai-news/` 生成，并支持桌面和移动端响应式查看。

一份成功日报会写入：

```text
data/YYYY/MM/YYYY-MM-DD.json
data/runs/YYYY/MM/YYYY-MM-DD.json
content/YYYY-MM-DD.md
data/index.json
```

日报 JSON 是站点构建和程序读取的规范数据；运行记录区分已发布、无合格内容和发布前失败，
并保存逐来源诊断；Markdown 用于人工审阅；`data/index.json` 保存跨日期去重和历史索引。
`dist/` 是可重新生成的 Pages artifact，不提交到 Git。

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

离线演示不访问网络、不调用真实模型，也不需要任何凭证；它会生成固定的 7 条样本，
适合验证从日报落盘到静态页面的完整链路：

```bash
python -m ai_news demo --root .demo --output .preview/ai-news
python scripts/validate_site.py .preview/ai-news --base-path /ai-news/
python -m http.server 8000 --directory .preview
```

演示数据位于 `.demo/`，站点位于 `.preview/ai-news/`。这两个目录均为本地产物。

## 使用真实模型生成

本项目支持 OpenAI Chat 和 Anthropic 两种协议适配器，运行时统一使用 `LLM_*`
环境变量。当前生产配置使用火山方舟 Coding Plan 的 OpenAI 兼容入口
`https://ark.cn-beijing.volces.com/api/coding/v3` 和模型 `deepseek-v4-pro`。
`deepseek-v4-pro` 是模型名，不是 `LLM_API_KEY`；后者必须是真实 API Key。

真实密钥不能发送到聊天，也不能写入文件、命令历史、README、日志或仓库；应直接写入 GitHub Secret，
或通过隐藏终端输入注入本地进程。下面的命令不会回显密钥：

```bash
export LLM_PROTOCOL="openai-chat"
export LLM_BASE_URL="https://ark.cn-beijing.volces.com/api/coding/v3"
export LLM_MODEL="deepseek-v4-pro"
export LLM_AUTH_SCHEME="bearer"
read -r -s LLM_API_KEY
export LLM_API_KEY
export SITE_BASE_PATH="/ai-news/"
python -m ai_news check-config
python -m ai_news generate --root .
```

不带 `--date` 时按 `Asia/Shanghai` 的当天生成。指定日期补跑使用严格的 ISO 日期：

```bash
python -m ai_news generate --root . --date YYYY-MM-DD
python -m ai_news build --root . --output .preview/ai-news
python scripts/validate_site.py .preview/ai-news --base-path /ai-news/
```

配置检查只允许方舟官方主机的精确 Coding Plan OpenAI Base URL
`/api/coding/v3`，并继续拒绝其他 Coding Plan 路径、完整请求端点、编码绕过、
userinfo、query 和 fragment。真实 API Key 只通过 GitHub Secret 或隐藏终端输入
提供，不能发送到聊天，也不能写入文件、README、日志或仓库。

## GitHub Secrets 与 Variables

在仓库 Settings → Secrets and variables → Actions 中配置以下内容：

| 类型 | 名称 | 用途 |
| --- | --- | --- |
| Variable | `LLM_PROTOCOL` | Coding Plan 使用 `openai-chat` |
| Variable | `LLM_BASE_URL` | `https://ark.cn-beijing.volces.com/api/coding/v3` |
| Variable | `LLM_MODEL` | `deepseek-v4-pro` |
| Variable | `LLM_AUTH_SCHEME` | Coding Plan 使用 `bearer` |
| Secret | `LLM_API_KEY` | 真实 API Key，不是模型名 |

`SITE_BASE_PATH=/ai-news/` 已固定在 `.github/workflows/daily-news.yml`，不是第四个
Repository Variable。GitHub Actions 只把 token 注入生成步骤；Secret 扫描在提交
生成数据前执行。唯一必需的 Secret 是 `LLM_API_KEY`；Reddit 使用 RSS，不需要任何
Reddit 客户端密钥或登录态。

可以使用 GitHub CLI 配置非敏感 Variables。Secret 必须直接通过
`gh secret set LLM_API_KEY` 的隐藏交互式输入设置，不能先保存到文件或发送到聊天：

```bash
gh variable set LLM_PROTOCOL --body openai-chat --repo mount-su/ai-news
gh variable set LLM_BASE_URL --body https://ark.cn-beijing.volces.com/api/coding/v3 --repo mount-su/ai-news
gh variable set LLM_MODEL --body deepseek-v4-pro --repo mount-su/ai-news
gh variable set LLM_AUTH_SCHEME --body bearer --repo mount-su/ai-news
gh secret set LLM_API_KEY --repo mount-su/ai-news
```

## GitHub Actions

`.github/workflows/ci.yml` 在 push 和 pull request 上运行 lint、格式、完整测试、Secret
扫描、离线演示、站点校验和 workflow lint。

`.github/workflows/source-health.yml` 在数据源或采集器相关 PR 上运行，并在每天
北京时间 07:17 执行一次无 Secret 的实时巡检。巡检分别报告请求是否成功、是否为空、
最新内容是否过期；日报再单独记录近窗、新增、合格、候选贡献和最终入选，不能用
“可访问”替代“有近期内容”。Reddit 若在 GitHub Actions 匿名环境中持续被限流，会记录
为单源失败，不阻止其他来源采集。

`.github/workflows/daily-news.yml` 有三条触发路径：

- `schedule` 使用 GitHub 的 `timezone: "Asia/Shanghai"`，每天北京时间 08:17 运行。
- `workflow_dispatch` 运行真实采集；可不填日期，也可传 `YYYY-MM-DD` 手动补跑。
- push 到 `main` 时跳过 generate，只从已有 `data/` 和 `content/` build、校验并部署。

所有第三方 Actions 都固定到不可变 commit SHA，并在行尾标注版本来源。工作流按 job
最小化权限：生成任务才有 `contents: write`，部署任务才有 `pages: write` 和
`id-token: write`。

首次启用 Pages 前，仓库中必须先有至少一份成功生成的真实日报。配置标准 API 凭证、
provider 控制台确认的模型 ID 和 Pages 来源（GitHub Actions）后，手动运行每日工作流；
首次真实生成成功后才会有可构建的数据和可部署页面。仅 push 空数据仓库的 `main`
不会调用模型，也无法替代首次真实生成。

手动补跑示例：

```bash
gh workflow run daily-news.yml --repo mount-su/ai-news -f date=YYYY-MM-DD
gh run list --repo mount-su/ai-news --workflow daily-news.yml
```

## 数据源维护

所有网络来源都在 `sources/feeds.yaml` 中显式声明。每项包含稳定 `id`、显示名称、
`kind`、默认分类、权重、`official` 标记和来源角色：`primary` 是可直接核验的一手来源，
`trusted_media` 是可信媒体，`discovery` 只负责发现线索。发现源必须解析并验证允许列表内
的外部原文才可能进入候选，不能仅凭社区或产品榜单文案发布事实。Feed 配置受控 HTTPS
URL；官网来源配置代码内允许列表中的官方页面适配器；Reddit 只配置固定社区名，不能
注入请求 URL、选择器或解析规则。

Reddit RSS 只承担社区线索发现，不作为事实来源。它串行请求 `new` 和 `top/day` 两条
合并订阅，间隔 60 秒，并验证最终的可信外部原文、主题和明确发布日期；不保存用户、帖子正文、
评论或投票数据。该方案不需要 `REDDIT_CLIENT_ID`、`REDDIT_CLIENT_SECRET`、Cookie
或 Reddit 登录。该来源默认启用，用于补充大模型、AI 应用、AI 产品和 Agent 相关线索；
不使用密钥、代理或 HTML 抓取绕过限制。

新增或调整来源时：

1. 优先使用官方 RSS/Atom；没有订阅源时只能增加经过审查的官方页面适配器，不使用通用网页抓取。
2. 保持 `id` 唯一且稳定；变更 `id` 会影响来源统计与历史判断。
3. URL 必须是 HTTPS，并通过实现中的 host/path 白名单检查。
4. 为解析和失败情况补固定 fixture 与测试，不让 CI 依赖实时网络。
5. Reddit 外部目标只接受启用厂商的官方域名或代码内可信媒体允许列表。
6. 运行完整质量门禁、实时来源健康检查和离线演示，再提交 `feeds.yaml`。

来源权重代表预筛选可信度，不代表内容必然正确。官方标记也不会替代事实核验。

## 故障与补跑

单个来源失败不会立即终止全部采集；超时、`429` 和可重试的 `5xx` 最多 3 次并指数
退避。日报记录每个来源的成功或失败状态。去重后的合格候选为 0，或模型不能
返回任何合规选编时停止发布，不提交空日报；来源失败但最终仍满足发布门槛时
可以降级发布，并记录 `degraded=true`。
该字段由来源运行是否失败决定。

模型限流和服务端错误采用有界重试；鉴权失败、无效模型和不安全重定向不会盲目重试。
模型响应对完整候选池执行校验；最多返回 7 条，单一来源最多 2 条，质量不足时不得凑数。
程序会容忍常见的供应商格式漂移，例如 JSON 外层解释文字、
`items` 包装对象、可安全转换的数字/布尔字符串和略超字段长度。条数、ID、分类分布、
来源分布或仍无法修复的字段无效时，只执行一次结构修复。修复后仍无效会抛出分析错误，
整次生成失败且不写入新日报。

历史补跑必须按日期从旧到新串行执行，并等待前一天的生成、提交与部署完成后再触发
下一天；这样跨日期去重只读取已经确认的更早内容，也避免同一并发组替换尚未开始的任务。
若新规则把已有日期判为无合格内容，该日期的旧日报会从站点和后续去重历史中失效，Git
历史仍保留可恢复版本。

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

采集只访问 `feeds.yaml` 和代码允许列表限定的 HTTPS 端点，不执行来源中的指令，也不
下载或运行来源代码、脚本和二进制文件。官网页面使用固定适配器，Reddit 只读取 RSS
及受限的可信外部原文；两者都不会降级为通用抓取或登录绕过。外部文本视为不可信输入，
长度受限；模板默认转义，客户端搜索索引只含公开日报字段。

仓库不保存文章全文、付费内容或媒体文件，只保存标题、原文链接、必要的短摘录、来源
元数据和本项目生成的中文分析。版权归原作者或发布方；摘要用于索引与研判，读者应访问
原文核对事实、上下文和许可。若来源要求移除，应从配置和后续数据中删除相应内容。

真实凭证只允许存在于进程环境和 GitHub Secrets。聊天、截图、日志或本地环境中的凭证
与令牌不能进入 Git 历史、测试快照、Actions 日志或 Pages artifact；真实密钥不能发送到
聊天，也不能写入文件。若曾暴露，应立即在 provider 侧撤销并轮换。
`scripts/check_no_secrets.py` 是提交前门禁，但不能替代凭证轮换。

## 项目结构

```text
.
├── .github/workflows/        # CI、每日生成与 Pages 部署
├── sources/feeds.yaml        # 允许的数据源
├── src/ai_news/
│   ├── collectors/           # Feed、官方页面适配器、Reddit RSS
│   ├── llm/                  # 双协议模型适配与严格响应校验
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
