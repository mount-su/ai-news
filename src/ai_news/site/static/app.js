(() => {
  "use strict";

  document.documentElement.classList.add("js");

  const searchPage = document.querySelector("[data-search-page]");
  if (!searchPage) {
    return;
  }

  const Search = globalThis.AINewsSearch;
  const form = document.querySelector("#search-form");
  const queryInput = document.querySelector("#search-query");
  const channelInput = document.querySelector("#search-channel");
  const sourceInput = document.querySelector("#search-source");
  const startInput = document.querySelector("#search-start");
  const endInput = document.querySelector("#search-end");
  const clearButton = document.querySelector("#search-clear");
  const status = document.querySelector("#search-status");
  const results = document.querySelector("#search-results");
  let entries = [];

  if (
    !Search ||
    !form ||
    !queryInput ||
    !channelInput ||
    !sourceInput ||
    !startInput ||
    !endInput ||
    !clearButton ||
    !status ||
    !results
  ) {
    return;
  }

  function filtersFromForm() {
    return {
      query: queryInput.value,
      channel: channelInput.value,
      source: sourceInput.value,
      startDate: startInput.value,
      endDate: endInput.value,
      useRecentWindow: !startInput.value && !endInput.value,
    };
  }

  function appendTextElement(parent, tagName, className, text) {
    const element = document.createElement(tagName);
    element.className = className;
    element.textContent = text;
    parent.append(element);
    return element;
  }

  function renderEntry(entry) {
    const listItem = document.createElement("li");
    const article = document.createElement("article");
    article.className = "search-result";

    const heading = document.createElement("h3");
    const link = document.createElement("a");
    link.className = "story-title-link";
    link.href = Search.resultUrl(entry);
    link.textContent = entry.title;
    heading.append(link);
    article.append(heading);

    appendTextElement(
      article,
      "p",
      "search-result__meta",
      `${entry.display_category} · ${entry.source} · ${entry.report_date}`,
    );
    appendTextElement(article, "p", "search-result__summary", entry.summary);
    appendTextElement(article, "p", "search-result__impact", entry.why_it_matters);
    listItem.append(article);
    return listItem;
  }

  function render() {
    const state = Search.resultState(entries, filtersFromForm());
    results.replaceChildren();
    if (state.status === "no-result") {
      status.textContent = "没有找到符合条件的内容。";
      return;
    }
    status.textContent = `找到 ${state.count} 条内容，按发布时间倒序。`;
    const fragment = document.createDocumentFragment();
    state.results.forEach((entry) => fragment.append(renderEntry(entry)));
    results.append(fragment);
  }

  function populateSources() {
    const sources = [...new Set(entries.map((entry) => entry.source))].sort((left, right) =>
      left.localeCompare(right, "zh-CN"),
    );
    const fragment = document.createDocumentFragment();
    sources.forEach((source) => {
      const option = document.createElement("option");
      option.value = source;
      option.textContent = source;
      fragment.append(option);
    });
    sourceInput.append(fragment);
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    render();
  });

  clearButton.addEventListener("click", () => {
    form.reset();
    render();
    queryInput.focus();
  });

  document.addEventListener("keydown", (event) => {
    if (
      event.key !== "/" ||
      event.defaultPrevented ||
      event.isComposing ||
      !globalThis.matchMedia("(pointer: fine)").matches
    ) {
      return;
    }
    const target = event.target;
    if (
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement ||
      target?.isContentEditable
    ) {
      return;
    }
    event.preventDefault();
    queryInput.focus();
  });

  fetch(searchPage.dataset.indexUrl, { headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) {
        throw new Error("search index request failed");
      }
      return response.text();
    })
    .then((serialized) => {
      const parsed = Search.parseIndex(serialized);
      if (parsed.status !== "success") {
        status.textContent = "搜索索引格式异常，请稍后重试。";
        return;
      }
      entries = parsed.entries;
      populateSources();
      render();
    })
    .catch(() => {
      status.textContent = Search.fetchErrorState().message;
    });
})();
