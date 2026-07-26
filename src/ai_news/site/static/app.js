(() => {
  "use strict";

  const feed = document.querySelector("[data-feed]");
  if (!feed) {
    return;
  }

  const search = feed.querySelector("[data-filter-search]");
  const buttons = Array.from(feed.querySelectorAll("button[data-category]"));
  const cards = Array.from(
    feed.querySelectorAll("[data-news-card][data-search][data-category]"),
  );
  const resultCount = feed.querySelector("[data-result-count]");
  const emptyState = feed.querySelector("[data-empty-state]");

  if (!search || buttons.length === 0 || !resultCount || !emptyState) {
    return;
  }

  resultCount.setAttribute("aria-live", "polite");
  let selectedCategory = "all";

  const update = () => {
    const query = search.value.trim().toLowerCase();
    let visibleCount = 0;

    cards.forEach((card) => {
      const haystack = (card.dataset.search || "").toLowerCase();
      const category = card.dataset.category || "";
      const categoryMatches = selectedCategory === "all" || category === selectedCategory;
      const searchMatches = query === "" || haystack.includes(query);
      const visible = categoryMatches && searchMatches;
      card.hidden = !visible;
      if (visible) {
        visibleCount += 1;
      }
    });

    resultCount.textContent = `${visibleCount} 条结果`;
    emptyState.hidden = visibleCount !== 0;
  };

  search.addEventListener("input", update);
  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      selectedCategory = button.dataset.category || "all";
      buttons.forEach((candidate) => {
        candidate.setAttribute("aria-pressed", String(candidate === button));
      });
      update();
    });
  });
})();
