# DeepSeek V4 生产超时修复设计

**日期：** 2026-07-29  
**仓库：** `mount-su/ai-news`  
**状态：** 待用户书面复核

## 背景

标准 Ark API 配置已经进入 GitHub Actions：

- `LLM_PROTOCOL=openai-chat`
- `LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3`
- `LLM_MODEL=deepseek-v4-pro-260425`
- `LLM_AUTH_SCHEME=bearer`
- `LLM_API_KEY` 仅存在于 Repository Secret

手动运行 `30379333026` 和 `30379910833` 都通过配置预检，但在
`Generate daily report` 中约 90 秒后失败，未进入暂存、Secret 扫描、提交、构建或
部署。公开来源的无模型诊断成功取得 10 个候选，13 个来源成功、2 个来源失败，
本地耗时约 67.5 秒。

方舟模型列表将 `deepseek-v4-pro-260425` 标为支持 Chat API 的深度思考模型；
Chat API 支持通过 `thinking.type=disabled` 关闭深度思考。当前请求没有设置
`thinking`，单请求超时也只有 30 秒。

## 目标

让 DeepSeek V4 Pro 在定时日报的严格 JSON 场景中：

1. 不执行不必要的长思维链；
2. 在正常网络波动或较慢响应下拥有 120 秒单请求时间；
3. 保留既有重试、安全校验和失败不落盘语义；
4. 即使所有重试都耗尽，也由 GitHub Actions 的整体上限终止。

## 方案

### OpenAI Chat 请求

`OpenAIChatAnalyzer._body()` 增加固定字段：

```json
{
  "thinking": {
    "type": "disabled"
  }
}
```

该字段只加入 `openai-chat` 请求。Anthropic 请求格式与行为保持不变。字段固定，
不增加新的环境变量，也不允许仓库变量覆盖。

### 请求超时与重试

`BaseAnalyzer` 的单请求超时从 30 秒提高到 120 秒，并继续通过每个
`httpx.Request` 的 timeout extension 显式设置，避免被采集客户端的默认 20 秒
覆盖。

重试策略不变：

- HTTP `429` 和 `5xx` 重试；
- 传输错误重试；
- 最多 4 次请求；
- 退避仍为 1、2、4 秒；
- `3xx`、其他 `4xx`、超大响应和非法响应继续立即失败。

首次分析和一次格式修复请求都使用相同的 120 秒超时。

### Actions 整体上限

`daily-news.yml` 的 `generate` job 增加：

```yaml
timeout-minutes: 30
```

这是整个采集与模型生成任务的硬上限。超时后不会执行数据暂存、Secret 扫描或
提交，因此不会产生部分日报。

## 安全与错误语义

- 不打印 API Key、Authorization header、请求体、响应体或动态异常文本；
- 继续使用现有固定错误输出；
- 模型返回内容仍需通过严格 schema、候选 ID 集合和 Secret 递归检查；
- 失败时不写入 `data/` 或 `content/`；
- 生成提交仍只允许包含 `data/` 与 `content/`。

## 测试要求

实施必须遵循 TDD：

1. 先让 OpenAI 请求体测试因缺少 `thinking.type=disabled` 失败；
2. 先让 timeout extension 测试因仍为 30 秒失败；
3. 先让 workflow contract 测试因缺少 `timeout-minutes: 30` 失败；
4. 最小实现后运行 OpenAI、Anthropic、共享核心、流水线和工作流聚焦测试；
5. 运行完整测试与覆盖率门槛、Ruff、格式、Secret 扫描、离线站点验证和
   actionlint。

测试还必须证明：

- repair 请求同样使用 120 秒；
- Anthropic 请求体没有 `thinking`；
- 重试次数与退避不变；
- workflow Secret 仍只注入 generate step。

## 生产验收

1. 合并并推送经审查的提交；
2. 等待对应 SHA 的线上 CI 成功；
3. 手动触发一次 `daily-news.yml`；
4. 验证 generate、build、deploy 全部成功；
5. 验证 bot 提交只包含 `data/` 和 `content/`；
6. 验证 Pages 返回 200，并完成桌面端、移动端、搜索、分类、详情、归档、
   键盘焦点和横向溢出检查；
7. 最后复查仓库、Actions 日志和部署产物中没有凭证。

## 非目标

- 不切换到 Responses API；
- 不改为流式响应；
- 不改变 `max_tokens`、批大小或重试次数；
- 不增加模型专属环境变量；
- 不输出更详细的生产异常或模型响应；
- 不改变采集、排序、发布阈值、存储或站点功能。
