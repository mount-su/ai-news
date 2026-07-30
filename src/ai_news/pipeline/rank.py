from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from ai_news.models import Candidate
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
    return (
        item.raw.source_weight * 10
        + _recency_score(item, normalized_now)
        + (10 if item.raw.is_official_source else 0)
        + (10 if _has_keyword(item.raw.title) else 0)
    )


def _ranked_candidates(items: list[Candidate], now: datetime) -> list[Candidate]:
    normalized_now = ensure_utc(now)
    return sorted(
        items,
        key=lambda item: (
            -candidate_score(item, normalized_now),
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
) -> list[Candidate]:
    if limit < 0:
        raise ValueError("limit must not be negative")
    if per_source <= 0:
        raise ValueError("per-source limit must be positive")
    if limit == 0:
        return []

    counts: Counter[str] = Counter()
    selected: list[Candidate] = []
    for item in _ranked_candidates(items, now):
        source_id = item.raw.source_id
        if counts[source_id] >= per_source:
            continue
        counts[source_id] += 1
        selected.append(item)
        if len(selected) == limit:
            break
    return selected
