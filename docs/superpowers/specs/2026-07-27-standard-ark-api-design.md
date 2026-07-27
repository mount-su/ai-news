# 标准 Ark API 与双协议 LLM 设计

## 背景

当前实现只支持 Anthropic-compatible `/v1/messages` 协议，并使用
`ANTHROPIC_*` 环境变量。仓库最初提供的
`https://ark.cn-beijing.volces.com/api/coding` 属于火山方舟 Coding Plan
入口。火山引擎公开说明该套餐只能在指定 AI 编程工具中使用，不能作为普通 API
供无人值守任务调用。因此，GitHub Actions 不得使用截图中的 Coding Plan 凭据生成
资讯日报。

火山方舟标准模型 API 使用 `https://ark.cn-beijing.volces.com/api/v3`，提供
OpenAI-compatible Chat Completions 接口。项目需要支持该标准接口，同时保留对合法
Anthropic-compatible 服务的支持。

依据：

- [火山方舟 Chat Completions API](https://api.volcengine.com/api-docs/view?action=ChatCompletions&serviceCode=ark&version=2024-01-01)
- [火山方舟 Coding Plan 使用限制](https://www.volcengine.com/article/37156)

## 决策

采用双协议分析器，并把模型配置迁移到通用 `LLM_*` 命名：

- `openai-chat`：调用标准 OpenAI-compatible `/chat/completions`。
- `anthropic`：调用现有 Anthropic-compatible `/v1/messages`。
- 应用配置层和网络请求层都拒绝火山方舟 `/api/coding` 入口。
- 不读取旧的 `ANTHROPIC_*` 环境变量，也不提供隐式回退。

这是一次干净迁移。仓库目前没有生产日报、模型 Secret 或成功 Pages 部署，因此无需
承担线上兼容负担。移除旧命名也能避免后来只添加 Secret 时意外启用 Coding Plan
配置。

## 目标

1. 使用标准 Ark API Key 和标准模型 ID 完成无人值守中文资讯分析。
2. 保留现有 Anthropic-compatible 服务的可选支持。
3. 两种协议共享同一套批次、严格 Schema 校验、一次修复、重试和安全边界。
4. 在发出任何模型请求前阻止已知不合规的 Coding Plan URL。
5. 让 GitHub Actions 仅通过通用 Repository Variables 和 Secret 注入模型配置。

## 非目标

- 不尝试让 Coding Plan 凭据绕过工具或用途限制。
- 不支持流式模型响应、工具调用、联网搜索或图片输入。
- 不引入 OpenAI 或火山方舟 SDK；继续使用 `httpx`，减少依赖和隐式行为。
- 不改变资讯来源、筛选排序、日报 Schema、静态站点或发布阈值。
- 不假设 Coding Plan 中的 `glm-5.2` 名称可直接用于标准 Ark API。生产模型 ID 必须
  来自标准方舟账户可用模型列表。

## 配置契约

应用读取且只读取以下模型变量：

| 类型 | 名称 | 说明 |
| --- | --- | --- |
| Variable | `LLM_PROTOCOL` | 必须为 `openai-chat` 或 `anthropic` |
| Variable | `LLM_BASE_URL` | HTTPS API 基础 URL，不含请求端点后缀 |
| Variable | `LLM_MODEL` | 标准 API 可调用的模型 ID |
| Variable | `LLM_AUTH_SCHEME` | 可选，默认 `bearer`；`openai-chat` 只允许 `bearer` |
| Secret | `LLM_API_KEY` | 标准模型 API 凭据 |
| Environment | `SITE_BASE_PATH` | 保持 `/ai-news/` |

所有必填值去除首尾空白后不得为空。基础 URL 必须满足：

- 使用 HTTPS；
- 有合法 host；
- 不含 userinfo、query 或 fragment；
- 路径是规范的绝对路径，不接受编码斜杠、反斜杠或控制字符；
- 当 host 为 `ark.cn-beijing.volces.com` 时，规范化路径不得等于
  `/api/coding`，也不得位于 `/api/coding/` 之下。

`openai-chat` 将 `/chat/completions` 追加到基础 URL；
`anthropic` 将 `/v1/messages` 追加到基础 URL。调用方不得把完整请求端点当作
基础 URL。

## 组件边界

### Settings

`Settings` 新增 `llm_protocol`，其余字段继续使用内部通用名称
`llm_base_url`、`llm_token`、`llm_model` 和 `llm_auth_scheme`。配置加载器从
`LLM_*` 环境变量映射到这些字段。

配置校验负责静态错误，包括缺少变量、协议值无效、鉴权方案不兼容、URL 不安全以及
Coding Plan URL。错误不得包含 API Key。

### 分析器工厂

流水线不再直接构造 `AnthropicAnalyzer`，而是调用
`create_analyzer(settings, client=client)`：

- `openai-chat` 返回 `OpenAIChatAnalyzer`；
- `anthropic` 返回 `AnthropicAnalyzer`；
- 未知协议在工厂之前已被 Settings 拒绝。

流水线现有 `Analyzer` Protocol 保持不变，因此采集、去重、排序、报告生成与测试注入
不需要改变。

### 共享分析核心

两种分析器共享以下行为：

- 每批最多 10 条候选；
- 同一系统提示词和候选分析提示词；
- 2 MB 响应上限；
- `429`、`5xx` 和传输错误最多重试 3 次，退避为 1、2、4 秒；
- 鉴权失败、其他 `4xx`、重定向和无效响应不重试；
- 对整批结果执行严格 JSON array 和候选 ID 集合校验；
- 首次响应无效时只执行一次结构修复；
- 修复仍失败时整次生成失败，不写日报；
- 修复提示词对 API Key 脱敏并限制无效响应长度。

协议模块只负责端点、headers、请求 body 和响应文本提取，避免复制安全与校验逻辑。

## 协议请求

### OpenAI-compatible Chat Completions

请求：

```json
{
  "model": "<LLM_MODEL>",
  "temperature": 0.2,
  "max_tokens": 6000,
  "n": 1,
  "messages": [
    {"role": "system", "content": "<system prompt>"},
    {"role": "user", "content": "<analysis or repair prompt>"}
  ]
}
```

使用 `Authorization: Bearer <LLM_API_KEY>` 和
`Content-Type: application/json`。响应必须包含非空
`choices[0].message.content` 字符串，否则视为无效模型响应。

### Anthropic-compatible Messages

保持现有请求格式、`anthropic-version: 2023-06-01` header 和
`content[].type == "text"` 响应提取。`bearer` 与 `x-api-key` 两种鉴权均保留。

## 安全和错误处理

- `/api/coding` 拒绝发生在初始化阶段，不发送探测请求。
- URL 检查使用解析后的 host 和规范化 path，不使用易绕过的原始字符串包含判断。
- GitHub Secret 只注入 `generate` step；build、deploy、CI 和静态产物均不可访问。
- 日志、Pydantic 错误、模型错误和修复提示词不得包含 API Key。
- 模型响应继续视为不可信输入，必须通过现有严格 Pydantic Schema。
- Secret 扫描器覆盖 `LLM_API_KEY` 及旧 `ANTHROPIC_AUTH_TOKEN`，防止迁移期间泄漏。

## GitHub Actions 与仓库配置

`daily-news.yml` 的生成步骤改为：

- Variables：`LLM_PROTOCOL`、`LLM_BASE_URL`、`LLM_MODEL`、
  `LLM_AUTH_SCHEME`；
- Secret：`LLM_API_KEY`；
- 保留 `SITE_BASE_PATH`、`GITHUB_TOKEN` 和可选日期。

生成命令前增加本地配置预检，实际校验仍由应用 Settings 完成。不得在 shell 中打印
变量值。

合并后删除仓库中现有的 `ANTHROPIC_BASE_URL`、
`ANTHROPIC_DEFAULT_OPUS_MODEL` 和 `ANTHROPIC_AUTH_SCHEME` Variables，再配置通用
Variables。只有获得标准 Ark API Key 和经标准账户确认可用的模型 ID 后，才设置
`LLM_API_KEY` Secret 并手动触发首次生成。

## 文档迁移

更新 `.env.example` 和 README：

- 使用通用 `LLM_*` 示例；
- 明确 `/api/coding` 不支持本项目；
- 给出标准 Ark `/api/v3` 配置示例，但模型 ID 使用占位符；
- Secret 通过交互式 `gh secret set LLM_API_KEY` 或 GitHub 设置页写入；
- 不要求用户把 API Key 粘贴到聊天、文件或 shell history。

## 测试

### 配置

- 读取完整 `LLM_*` 配置；
- 拒绝缺失、空白、未知协议和不兼容鉴权；
- 拒绝 Ark `/api/coding` 及其子路径；
- 覆盖大小写 host、尾斜杠、编码斜杠和 query/fragment 绕过；
- 证明错误文本不包含 Secret；
- 证明旧 `ANTHROPIC_*` 不会被隐式读取。

### OpenAI 分析器

- 精确验证端点、Bearer header、请求 body 和响应提取；
- 复用或参数化现有批次、重试、2 MB、重定向、Schema 和一次修复测试；
- 覆盖空 `choices`、非字符串 content、错误状态和安全错误；
- 证明 API Key 不进入修复提示词或异常文本。

### 集成与工作流

- 流水线根据 `llm_protocol` 选择正确分析器；
- workflow 只映射通用变量和 Secret；
- workflow 不再包含旧变量或 Coding Plan URL；
- Secret 扫描器识别 `LLM_API_KEY` 的真实值和常见 header/配置形式；
- README 契约测试验证新配置、合规说明和首次部署步骤。

### 完整门禁

运行：

```bash
ruff check .
ruff format --check .
pytest --cov=ai_news --cov-report=term-missing --cov-fail-under=85
python scripts/check_no_secrets.py
python -m ai_news demo --root .demo --output .demo/dist
python scripts/validate_site.py .demo/dist --base-path /ai-news/
git diff --check
```

## 发布与验收

1. 在功能分支通过完整门禁和规格/质量审查。
2. 快进合并到 `main` 并推送，确认 GitHub CI 通过。
3. 删除旧 Repository Variables，配置标准 API 通用 Variables。
4. 用户通过 GitHub Secret 设置标准 `LLM_API_KEY`。
5. 手动触发 `daily-news.yml`，生成首份真实日报。
6. 验证日报提交只包含 `data/` 与 `content/`。
7. 验证 build、站点校验和 Pages deploy 成功。
8. 在桌面与移动视口打开生产 Pages，检查页面、资源、搜索、筛选和内部链接。
9. 确认后续北京时间每天 08:17 的 schedule 保持启用。

完成标准是：公开仓库 CI 绿色、真实日报已提交、生产 Pages 返回 200 且关键交互正常，
并且任何仓库文件、日志或静态产物中都不存在模型 Secret。
