from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from ai_news.models import Candidate, Category, SourceRole
from ai_news.pipeline.normalize import ensure_utc, normalize_title

_ENGLISH_KEYWORD = re.compile(r"\b(?:release|launch|introducing)\b")
_CHINESE_KEYWORDS = ("发布", "推出", "开源")


def _recency_score(item: Candidate, now: datetime) -> int:
    published_at = ensure_utc(item.raw.published_at)
    age_hours = max(0.0, (now - published_at).total_seconds() / 3600)
    for boundary, score in (
        (6, 20),
        (12, 16),
        (24, 12),
        (36, 8),
        (72, 4),
        (96, 2),
        (120, 1),
    ):
        if age_hours <= boundary:
            return score
    return 0


def _has_keyword(title: str) -> bool:
    normalized = normalize_title(title).casefold()
    return bool(_ENGLISH_KEYWORD.search(normalized)) or any(
        keyword in normalized for keyword in _CHINESE_KEYWORDS
    )


def candidate_score(item: Candidate, now: datetime) -> int:
    normalized_now = ensure_utc(now)
    role_bonus = {
        SourceRole.PRIMARY: 14,
        SourceRole.TRUSTED_MEDIA: 8,
        SourceRole.DISCOVERY: 0,
    }[item.raw.source_role]
    completeness = min(12, len(item.raw.excerpt.strip()) // 80)
    return (
        item.raw.source_weight * 4
        + _recency_score(item, normalized_now)
        + role_bonus
        + (8 if item.raw.is_official_source else 0)
        + completeness
        + (6 if _has_keyword(item.raw.title) else 0)
    )


def _channel_key(category: Category) -> Category | None:
    if category == Category.CODING_AGENT:
        return Category.AGENT
    if category == Category.RESEARCH:
        return None
    return category


def _ranked_candidates(items: list[Candidate], now: datetime) -> list[Candidate]:
    normalized_now = ensure_utc(now)
    channel_counts = Counter(
        channel for item in items if (channel := _channel_key(item.raw.category_hint)) is not None
    )

    def scarcity_bonus(item: Candidate) -> int:
        channel = _channel_key(item.raw.category_hint)
        if channel is None:
            return 0
        return max(0, 6 - min(channel_counts[channel], 6))

    return sorted(
        items,
        key=lambda item: (
            -(candidate_score(item, normalized_now) + scarcity_bonus(item)),
            -ensure_utc(item.raw.published_at).timestamp(),
            item.id,
        ),
    )


def select_candidates(
    items: list[Candidate],
    now: datetime,
    limit: int = 30,
) -> list[Candidate]:
    if limit < 0:
        raise ValueError("limit must not be negative")
    if limit == 0:
        return []

    return _ranked_candidates(items, now)[:limit]


def select_balanced_candidates(
    items: list[Candidate],
    now: datetime,
    *,
    limit: int = 36,
    per_source: int = 6,
    unofficial_per_source: int | None = None,
) -> list[Candidate]:
    if limit < 0:
        raise ValueError("limit must not be negative")
    if per_source <= 0:
        raise ValueError("per-source limit must be positive")
    if unofficial_per_source is not None and unofficial_per_source <= 0:
        raise ValueError("unofficial per-source limit must be positive")
    if limit == 0:
        return []

    counts: Counter[str] = Counter()
    selected: list[Candidate] = []
    for item in _ranked_candidates(items, now):
        source_id = item.raw.source_id
        source_limit = (
            unofficial_per_source
            if unofficial_per_source is not None and not item.raw.is_official_source
            else per_source
        )
        if counts[source_id] >= source_limit:
            continue
        counts[source_id] += 1
        selected.append(item)
        if len(selected) == limit:
            break
    return selected


def select_diversity_preserving_candidates(
    items: list[Candidate],
    now: datetime,
    *,
    limit: int,
    source_floor: int,
) -> list[Candidate]:
    """Keep each source's best rows before filling the remaining global budget."""
    if limit < 0:
        raise ValueError("limit must not be negative")
    if source_floor <= 0:
        raise ValueError("source floor must be positive")
    if limit == 0:
        return []

    ranked = _ranked_candidates(items, now)
    retained_ids = {
        item.id
        for item in select_balanced_candidates(
            ranked,
            now,
            limit=limit,
            per_source=source_floor,
        )
    }
    for item in ranked:
        if len(retained_ids) == limit:
            break
        retained_ids.add(item.id)
    return [item for item in ranked if item.id in retained_ids][:limit]
