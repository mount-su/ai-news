from __future__ import annotations

from difflib import SequenceMatcher
from urllib.parse import urlsplit

from ai_news.models import Candidate
from ai_news.pipeline.normalize import ensure_utc, normalize_title

_FUZZY_DUPLICATE_THRESHOLD = 0.92


def _normalized_title(item: Candidate) -> str:
    return normalize_title(item.raw.title).casefold()


def _same_story(
    first: Candidate,
    second: Candidate,
    first_title: str,
    second_title: str,
) -> bool:
    if str(first.canonical_url) == str(second.canonical_url):
        return True
    first_host = urlsplit(str(first.canonical_url)).hostname
    second_host = urlsplit(str(second.canonical_url)).hostname
    if first_host == second_host and first_title == second_title:
        return True
    return SequenceMatcher(None, first_title, second_title).ratio() >= _FUZZY_DUPLICATE_THRESHOLD


def deduplicate(
    items: list[Candidate],
    historical_urls: set[str],
) -> list[Candidate]:
    historical = {str(url) for url in historical_urls}
    current = [item for item in items if str(item.canonical_url) not in historical]
    titles = [_normalized_title(item) for item in current]
    parents = list(range(len(current)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first_index: int, second_index: int) -> None:
        first_root = find(first_index)
        second_root = find(second_index)
        if first_root != second_root:
            parents[second_root] = first_root

    for first_index, first in enumerate(current):
        for second_index in range(first_index + 1, len(current)):
            if _same_story(
                first,
                current[second_index],
                titles[first_index],
                titles[second_index],
            ):
                union(first_index, second_index)

    groups: dict[int, list[Candidate]] = {}
    for index, item in enumerate(current):
        groups.setdefault(find(index), []).append(item)

    winners = [
        min(
            group,
            key=lambda item: (
                -item.raw.source_weight,
                -ensure_utc(item.raw.published_at).timestamp(),
                item.id,
            ),
        )
        for group in groups.values()
    ]
    return sorted(winners, key=lambda item: item.id)
