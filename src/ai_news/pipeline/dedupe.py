from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from ai_news.models import Candidate, SourceRole
from ai_news.pipeline.normalize import ensure_utc, normalize_title

_FUZZY_DUPLICATE_THRESHOLD = 0.92
_FUZZY_TITLE_MAX_CHARS = 256
_MAX_DEDUPLICATION_ITEMS = 500
_ROLE_PRIORITY = {
    SourceRole.PRIMARY: 0,
    SourceRole.TRUSTED_MEDIA: 1,
    SourceRole.DISCOVERY: 2,
}
_EVENT_ACTIONS = (
    (
        "launch",
        re.compile(
            r"(?:\blaunch(?:es|ed|ing)?\b|\breleas(?:e|es|ed|ing)\b|"
            r"\bintroduc(?:e|es|ed|ing)\b|\bannounc(?:e|es|ed|ing)\b|"
            r"\bnow available\b|正式发布|发布|推出|上线|开放)",
            re.IGNORECASE,
        ),
    ),
    (
        "price",
        re.compile(r"(?:\bpric(?:e|es|ed|ing)\b|\bcost\b|价格|降价|费用)", re.IGNORECASE),
    ),
    (
        "update",
        re.compile(r"(?:\bupdat(?:e|es|ed|ing)\b|\bupgrad(?:e|es|ed|ing)\b|更新|升级)"),
    ),
)
_VERSIONED_PRODUCT = re.compile(
    r"(?<![a-z0-9])([a-z][a-z0-9]*[\s_-]*\d+(?:\.\d+)*)(?![a-z0-9])",
    re.IGNORECASE,
)
_LATIN_TOKEN = re.compile(r"\b[a-z][a-z0-9-]{2,}\b", re.IGNORECASE)
_EVENT_STOPWORDS = frozenset(
    {
        "agent",
        "ai",
        "announce",
        "announced",
        "announces",
        "anthropic",
        "app",
        "assistant",
        "available",
        "enterprise",
        "google",
        "introduce",
        "introduced",
        "introduces",
        "launch",
        "launched",
        "launches",
        "meta",
        "microsoft",
        "model",
        "new",
        "official",
        "officially",
        "openai",
        "platform",
        "product",
        "release",
        "released",
        "releases",
        "service",
        "system",
        "teams",
        "tool",
        "users",
    }
)


@dataclass(frozen=True)
class _FuzzyTitle:
    value: str
    character_counts: Counter[str]


def _normalized_title(item: Candidate) -> str:
    return normalize_title(item.raw.title).casefold()


def _fuzzy_title(item: Candidate) -> _FuzzyTitle:
    value = _normalized_title(item)[:_FUZZY_TITLE_MAX_CHARS]
    return _FuzzyTitle(value=value, character_counts=Counter(value))


def _can_reach_fuzzy_threshold(first: _FuzzyTitle, second: _FuzzyTitle) -> bool:
    total_length = len(first.value) + len(second.value)
    maximum_length_match = 2 * min(len(first.value), len(second.value)) / total_length
    if maximum_length_match < _FUZZY_DUPLICATE_THRESHOLD:
        return False

    shared_characters = sum(
        min(count, second.character_counts.get(character, 0))
        for character, count in first.character_counts.items()
    )
    maximum_character_match = 2 * shared_characters / total_length
    return maximum_character_match >= _FUZZY_DUPLICATE_THRESHOLD


def _fuzzy_duplicate(first: _FuzzyTitle, second: _FuzzyTitle) -> bool:
    if first.value == second.value:
        return True
    if not _can_reach_fuzzy_threshold(first, second):
        return False
    return (
        min(
            SequenceMatcher(None, first.value, second.value).ratio(),
            SequenceMatcher(None, second.value, first.value).ratio(),
        )
        >= _FUZZY_DUPLICATE_THRESHOLD
    )


def duplicate_preference_key(item: Candidate) -> tuple[object, ...]:
    return (
        _ROLE_PRIORITY[item.raw.source_role],
        not item.raw.is_official_source,
        not item.raw.discovery_verified,
        -item.raw.source_weight,
        -len(item.raw.excerpt.strip()),
        -ensure_utc(item.raw.published_at).timestamp(),
        item.id,
        item.raw.source_id,
        item.raw.source_name,
        item.raw.title,
        item.raw.excerpt,
        item.raw.category_hint.value,
        item.raw.is_official_source,
        str(item.raw.url),
    )


def _event_key(item: Candidate) -> tuple[str, tuple[str, ...]] | None:
    title = _normalized_title(item)
    action = next(
        (action_name for action_name, pattern in _EVENT_ACTIONS if pattern.search(title)),
        None,
    )
    if action is None:
        return None

    versioned_products = {
        re.sub(r"[\s_]+", "-", match.group(1)).strip("-")
        for match in _VERSIONED_PRODUCT.finditer(title)
    }
    versioned_products = {
        product for product in versioned_products if not re.fullmatch(r"v\d+(?:\.\d+)*", product)
    }
    if versioned_products:
        return action, tuple(sorted(versioned_products))

    product_terms = [
        token for token in _LATIN_TOKEN.findall(title) if token not in _EVENT_STOPWORDS
    ]
    if not product_terms:
        return None
    return action, tuple(product_terms[:2])


def _collapse_exact_duplicates(items: list[Candidate]) -> list[Candidate]:
    parents = list(range(len(items)))
    canonical_roots: dict[str, int] = {}
    title_roots: dict[tuple[str | None, str], int] = {}
    event_roots: dict[tuple[str, tuple[str, ...]], int] = {}

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

    for index, item in enumerate(items):
        canonical_key = str(item.canonical_url)
        title_key = (
            urlsplit(canonical_key).hostname,
            _normalized_title(item),
        )
        event_key = _event_key(item)
        if canonical_key in canonical_roots:
            union(index, canonical_roots[canonical_key])
        else:
            canonical_roots[canonical_key] = index
        if title_key in title_roots:
            union(index, title_roots[title_key])
        else:
            title_roots[title_key] = index
        if event_key is not None:
            if event_key in event_roots:
                union(index, event_roots[event_key])
            else:
                event_roots[event_key] = index

    groups: dict[int, list[Candidate]] = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)
    return [min(group, key=duplicate_preference_key) for group in groups.values()]


def deduplicate(
    items: list[Candidate],
    historical_urls: set[str],
) -> list[Candidate]:
    if len(items) > _MAX_DEDUPLICATION_ITEMS:
        raise ValueError(f"deduplicate accepts at most {_MAX_DEDUPLICATION_ITEMS} candidates")

    historical = {str(url) for url in historical_urls}
    current = [item for item in items if str(item.canonical_url) not in historical]
    exact_winners = sorted(_collapse_exact_duplicates(current), key=duplicate_preference_key)
    prepared = [(item, _fuzzy_title(item)) for item in exact_winners]
    groups: list[list[tuple[Candidate, _FuzzyTitle]]] = []
    for item, title in prepared:
        for group in groups:
            if all(_fuzzy_duplicate(title, grouped_title) for _, grouped_title in group):
                group.append((item, title))
                break
        else:
            groups.append([(item, title)])

    winners = [min((item for item, _ in group), key=duplicate_preference_key) for group in groups]
    return sorted(winners, key=lambda item: item.id)
