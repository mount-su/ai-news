(function exposeSearchCore(root, factory) {
  "use strict";

  const api = Object.freeze(factory());
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.AINewsSearch = api;
  }
})(typeof globalThis === "object" ? globalThis : undefined, function createSearchCore() {
  "use strict";

  const DAY_MS = 24 * 60 * 60 * 1000;
  const DEFAULT_RECENT_DAYS = 30;

  function normalizedText(value) {
    return String(value ?? "").normalize("NFKC").trim().toLocaleLowerCase();
  }

  function dateTimestamp(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value ?? ""))) {
      return Number.NaN;
    }
    return Date.parse(`${value}T00:00:00Z`);
  }

  function clearFilters() {
    return {
      query: "",
      channel: "",
      source: "",
      startDate: "",
      endDate: "",
      useRecentWindow: true,
    };
  }

  function searchableText(entry) {
    return normalizedText(
      [
        entry.title,
        entry.summary,
        entry.why_it_matters,
        entry.source,
        ...(Array.isArray(entry.tags) ? entry.tags : []),
      ].join("\n"),
    );
  }

  function searchEntries(entries, filters = {}) {
    if (!Array.isArray(entries)) {
      throw new TypeError("entries must be an array");
    }
    const query = normalizedText(filters.query);
    const channel = normalizedText(filters.channel);
    const source = normalizedText(filters.source);
    let startDate = String(filters.startDate ?? "");
    const endDate = String(filters.endDate ?? "");

    if (!startDate && !endDate && filters.useRecentWindow !== false && entries.length) {
      const newestTimestamp = Math.max(
        ...entries.map((entry) => dateTimestamp(entry.report_date)).filter(Number.isFinite),
      );
      if (Number.isFinite(newestTimestamp)) {
        startDate = new Date(newestTimestamp - (DEFAULT_RECENT_DAYS - 1) * DAY_MS)
          .toISOString()
          .slice(0, 10);
      }
    }

    return entries
      .filter((entry) => {
        const reportDate = String(entry.report_date ?? "");
        return (
          (!query || searchableText(entry).includes(query)) &&
          (!channel || normalizedText(entry.display_category) === channel) &&
          (!source || normalizedText(entry.source) === source) &&
          (!startDate || reportDate >= startDate) &&
          (!endDate || reportDate <= endDate)
        );
      })
      .slice()
      .sort((left, right) => {
        const timeDifference = Date.parse(right.published_at) - Date.parse(left.published_at);
        return timeDifference || String(left.id).localeCompare(String(right.id));
      });
  }

  function resultUrl(entry) {
    const value = String(entry?.page_url ?? "");
    if (!value.startsWith("/") || !/#item-[^/]+/.test(value)) {
      throw new TypeError("result URL must contain an article fragment");
    }
    return value;
  }

  function resultState(entries, filters = {}) {
    const results = searchEntries(entries, filters);
    return {
      status: results.length ? "success" : "no-result",
      results,
      count: results.length,
    };
  }

  function parseIndex(serialized) {
    try {
      const entries = JSON.parse(serialized);
      if (
        !Array.isArray(entries) ||
        entries.some(
          (entry) =>
            !entry ||
            typeof entry !== "object" ||
            typeof entry.id !== "string" ||
            typeof entry.page_url !== "string",
        )
      ) {
        throw new TypeError("search index must be an entry array");
      }
      return { status: "success", entries };
    } catch (_error) {
      return { status: "invalid-json", entries: [] };
    }
  }

  function fetchErrorState() {
    return {
      status: "fetch-error",
      entries: [],
      message: "搜索索引暂时无法加载，请稍后重试。",
    };
  }

  return {
    DEFAULT_RECENT_DAYS,
    clearFilters,
    fetchErrorState,
    parseIndex,
    resultState,
    resultUrl,
    searchEntries,
  };
});
