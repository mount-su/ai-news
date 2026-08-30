const test = require("node:test");
const assert = require("node:assert/strict");

const Search = require("../src/ai_news/site/static/search-core.js");

const entries = [
  {
    id: "new-model",
    title: "Gemini 企业版正式开放",
    summary: "面向企业工作流提供可部署能力。",
    why_it_matters: "Enterprise teams can move from pilot to production.",
    tags: ["Gemini", "企业部署"],
    display_category: "大模型",
    source: "Google AI",
    published_at: "2026-08-30T08:00:00+00:00",
    report_date: "2026-08-30",
    page_url: "/ai-news/days/2026-08-30/#item-new-model-2026-08-30",
  },
  {
    id: "agent",
    title: "Coding Agent updates",
    summary: "A safer agent workflow for teams.",
    why_it_matters: "减少人工交接。",
    tags: ["Agent", "开发工具"],
    display_category: "Agent",
    source: "OpenAI",
    published_at: "2026-08-25T09:00:00+00:00",
    report_date: "2026-08-25",
    page_url: "/ai-news/days/2026-08-25/#item-agent-2026-08-25",
  },
  {
    id: "old-tool",
    title: "AI 浏览器旧版",
    summary: "较早发布的产品更新。",
    why_it_matters: "用于验证默认时间范围。",
    tags: ["AI 浏览器"],
    display_category: "AI 产品",
    source: "Example Media",
    published_at: "2026-07-20T08:00:00+00:00",
    report_date: "2026-07-20",
    page_url: "/ai-news/days/2026-07-20/#item-old-tool-2026-07-20",
  },
];

test("matches Chinese substrings and case-insensitive English across editorial fields", () => {
  assert.deepEqual(Search.searchEntries(entries, { query: "企业部署" }).map((item) => item.id), [
    "new-model",
  ]);
  assert.deepEqual(Search.searchEntries(entries, { query: "ENTERPRISE" }).map((item) => item.id), [
    "new-model",
  ]);
  assert.deepEqual(Search.searchEntries(entries, { query: "openai" }).map((item) => item.id), [
    "agent",
  ]);
  assert.deepEqual(Search.searchEntries(entries, { query: "开发工具" }).map((item) => item.id), [
    "agent",
  ]);
});

test("combines channel source and inclusive date filters", () => {
  const results = Search.searchEntries(entries, {
    query: "agent",
    channel: "Agent",
    source: "OpenAI",
    startDate: "2026-08-25",
    endDate: "2026-08-25",
  });
  assert.deepEqual(results.map((item) => item.id), ["agent"]);
  assert.deepEqual(Search.searchEntries(entries, { ...Search.clearFilters(), channel: "大模型" }).map((item) => item.id), ["new-model"]);
});

test("defaults to the newest 30 calendar days and sorts newest first", () => {
  const results = Search.searchEntries([...entries].reverse(), {});
  assert.deepEqual(results.map((item) => item.id), ["new-model", "agent"]);
  assert.deepEqual(
    Search.searchEntries(entries, { useRecentWindow: false }).map((item) => item.id),
    ["new-model", "agent", "old-tool"],
  );
});

test("clear filters returns a stable default state", () => {
  assert.deepEqual(Search.clearFilters(), {
    query: "",
    channel: "",
    source: "",
    startDate: "",
    endDate: "",
    useRecentWindow: true,
  });
});

test("keeps the indexed article fragment as the result URL", () => {
  assert.equal(
    Search.resultUrl(entries[0]),
    "/ai-news/days/2026-08-30/#item-new-model-2026-08-30",
  );
  assert.throws(() => Search.resultUrl({ page_url: "/ai-news/days/2026-08-30/" }), /fragment/);
});

test("reports success no-result fetch-error and invalid-JSON states", () => {
  assert.equal(Search.resultState(entries, { query: "Gemini" }).status, "success");
  assert.equal(Search.resultState(entries, { query: "不存在" }).status, "no-result");
  assert.deepEqual(Search.parseIndex(JSON.stringify(entries)).status, "success");
  assert.equal(Search.parseIndex("{").status, "invalid-json");
  assert.equal(Search.parseIndex("{}").status, "invalid-json");
  assert.equal(Search.fetchErrorState().status, "fetch-error");
});
