# Ark Coding Plan 接入设计

**日期：** 2026-07-29
**状态：** 已确认
**范围：** `mount-su/ai-news` 的 LLM 配置、端点安全校验、运维文档与生产验收

## 背景

当前生产配置使用火山方舟标准模型入口
`https://ark.cn-beijing.volces.com/api/v3` 和版本化模型
`deepseek-v4-pro-260425`，真实请求返回 `HTTP 403`。用户提供的方舟控制台使用配置
明确显示其账户使用 Coding Plan：

- OpenAI 兼容 Base URL：
  `https://ark.cn-beijing.volces.com/api/coding/v3`
- 模型名：`deepseek-v4-pro`
- API Key 已保存在 GitHub Actions Secret `LLM_API_KEY`

因此本设计用用户账户实际支持的 Coding Plan 配置替代标准 Ark 生产配置。

本设计仅覆盖接入方式迁移，不改变采集源、候选排序、分析 schema、发布阈值、站点结构、
调度时间或 Secret 注入方式。

## 方案选择

### 采用：OpenAI Chat 兼容 Coding Plan

生产配置为：

| 名称 | 值 |
| --- | --- |
| `LLM_PROTOCOL` | `openai-chat` |
| `LLM_BASE_URL` | `https://ark.cn-beijing.volces.com/api/coding/v3` |
| `LLM_MODEL` | `deepseek-v4-pro` |
| `LLM_AUTH_SCHEME` | `bearer` |
| `LLM_API_KEY` | 沿用 GitHub Secret 中的现有值 |

客户端在 Base URL 后追加 `chat/completions`，最终请求地址为
`https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions`。

继续使用现有 OpenAI Chat 适配器、`thinking.type=disabled`、120 秒单次请求超时和
30 分钟 Actions 生成作业上限。

### 未采用：Anthropic 兼容 Coding Plan

该方案需要切换协议和请求体，并绕开已经验证过的 OpenAI Chat 适配器，改动和回归范围
更大。当前控制台同时提供 OpenAI 兼容入口，因此不采用。

### 未采用：标准 Ark API

标准 `/api/v3` 已由真实运行证明对当前 Secret 和目标模型返回 `HTTP 403`，并与用户
提供的 Coding Plan 使用配置不一致，因此不继续使用。

## 端点安全边界

端点校验器只新增一个窄例外：

- 允许精确主机 `ark.cn-beijing.volces.com`；
- 允许精确规范化路径 `/api/coding/v3`，以及一个无语义差异的末尾 `/`；
- 仅允许 HTTPS、默认或显式合法端口，并继续拒绝 userinfo、query、fragment；
- 继续拒绝 `/api/coding`、`/api/coding/v3/...` 作为 Base URL；
- 继续拒绝大小写混淆、点路径、反斜杠、重复斜杠、多轮百分号编码分隔符和控制字符；
- 继续拒绝把完整 `chat/completions` 或 `v1/messages` 请求地址误填为 Base URL；
- 非方舟第三方 HTTPS Base URL 的既有行为保持不变。

请求构造仍由 `build_llm_endpoint` 追加受限后缀，调用方不能通过配置任意改写最终请求
路径。

## 数据流

1. GitHub Actions 从 Variables 读取协议、Base URL、模型和鉴权方案。
2. `LLM_API_KEY` 仅从 GitHub Secret 注入 `generate` 步骤。
3. `check-config` 在任何网络请求前验证配置和 Coding Plan 精确端点。
4. 采集器获取公开资讯并选择候选。
5. OpenAI Chat 适配器向 Coding Plan 请求分析，响应继续执行严格 JSON schema 校验和
   一次修复请求。
6. 只有至少五条有效资讯时才原子写入 `data/` 与 `content/`。
7. Actions Secret 扫描通过后，机器人只提交上述两个目录。
8. 后续 build、validate 和 deploy 发布到 GitHub Pages。

## 错误处理与安全

- 不读取、显示、记录或重新设置现有 `LLM_API_KEY`。
- GitHub 日志只允许输出现有脱敏失败码，例如 `http_401`、`http_403`、
  `invalid_response` 或 `invalid_after_repair`。
- API 响应正文、Authorization 请求头和 Secret 值不得写入日志、报告、提交或 Pages
  产物。
- 401/403 视为凭证或套餐权限失败；429/5xx 和传输错误保持现有四次尝试与指数退避。
- 配置、生成、提交、构建或部署任一阶段失败时，不宣称生产成功。

## 文档迁移

README、`.env.example` 和对应文档契约改为 Coding Plan 生产配置，并明确：

- `deepseek-v4-pro` 是 `LLM_MODEL`，不是 `LLM_API_KEY`；
- Secret 必须保存真实 API Key；
- 不能把 API Key 发送到聊天或写入仓库；
- 当前生产 Base URL 是 `/api/coding/v3`。

2026-07-27 的标准 Ark 补充设计保留为历史记录；涉及当前生产 provider、Base URL 和
模型名的内容，以本设计为准。

## 测试与生产验收

实现必须通过以下门禁：

1. 端点单元测试先证明 `/api/coding/v3` 当前被拒绝，再放行精确路径。
2. 安全回归测试覆盖伪造主机、子路径、点路径、编码分隔符、query、fragment 和完整
   请求端点。
3. 配置、OpenAI 请求、README、`.env.example` 和 workflow 契约测试通过。
4. Ruff、完整 pytest、覆盖率不低于 85%、Secret 扫描、离线站点验证和 actionlint
   全部通过。
5. 精确 SHA 的线上 CI 成功。
6. 手动真实生成成功，机器人提交仅包含 `data/` 与 `content/`。
7. Pages 返回 HTTP 200；桌面端 1440px 和移动端 390px 下验证首页、归档、分类、搜索、
   详情展开、键盘焦点和无水平溢出。
8. 页面和部署产物中不存在任何凭证值或凭证配置泄露。

只有以上生产验收全部完成，目标才算成功。
