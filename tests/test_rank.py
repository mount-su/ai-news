from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_news.models import Candidate, Category, RawItem
from ai_news.pipeline.normalize import to_candidate
from ai_news.pipeline.rank import candidate_score, select_candidates

NOW = datetime(2026, 7, 26, 12, tzinfo=UTC)


def _candidate(
    slug: str,
    title: str,
    *,
    weight: int = 5,
    age_hours: float = 1,
    official: bool = False,
) -> Candidate:
    return to_candidate(
        RawItem(
            source_id=f"source-{slug}",
            source_name=f"Source {slug}",
            source_weight=weight,
            title=title,
            url=f"https://example.com/{slug}",
            published_at=NOW - timedelta(hours=age_hours),
            excerpt="Summary",
            category_hint=Category.MODEL,
            is_official_source=official,
        )
    )


@pytest.mark.parametrize(
    ("age_hours", "expected_recency"),
    [
        (-5, 20),
        (0, 20),
        (6, 20),
        (6.01, 16),
        (12, 16),
        (12.01, 12),
        (24, 12),
        (24.01, 8),
        (36, 8),
        (36.01, 4),
        (72, 4),
        (72.01, 0),
    ],
)
def test_candidate_score_uses_exact_recency_boundaries(
    age_hours: float,
    expected_recency: int,
) -> None:
    item = _candidate("recency", "Routine update", weight=1, age_hours=age_hours)

    assert candidate_score(item, NOW) == 10 + expected_recency


def test_candidate_score_uses_exact_weight_official_and_single_keyword_formula() -> None:
    item = _candidate(
        "formula",
        "Introducing a release and 开源 launch",
        weight=7,
        age_hours=10,
        official=True,
    )

    assert candidate_score(item, NOW) == 70 + 16 + 10 + 10


@pytest.mark.parametrize(
    "title",
    [
        "Model release",
        "MODEL LAUNCH today",
        "Introducing: an agent",
        "新品发布",
        "正式推出",
        "模型开源",
    ],
)
def test_candidate_score_recognizes_supported_keyword_semantics(title: str) -> None:
    assert candidate_score(_candidate("keyword", title, weight=1), NOW) == 40


@pytest.mark.parametrize(
    "title",
    [
        "Released candidate notes",
        "The launcher was redesigned",
        "An unreleased internal model",
        "Routine model update",
    ],
)
def test_candidate_score_does_not_match_unrelated_or_non_exact_english_words(title: str) -> None:
    assert candidate_score(_candidate("plain", title, weight=1), NOW) == 30


def test_candidate_score_requires_timezone_aware_now() -> None:
    with pytest.raises(ValueError, match="timezone"):
        candidate_score(_candidate("aware", "Routine update"), NOW.replace(tzinfo=None))


def test_select_candidates_sorts_by_score_then_published_time_then_id() -> None:
    highest = _candidate("highest", "Launch", weight=9, age_hours=20)
    newest = _candidate("newest", "Routine update", weight=8, age_hours=1)
    same_score_older = _candidate("older", "Routine update", weight=8, age_hours=2)
    tie_a = _candidate("tie-a", "Routine update", weight=6, age_hours=2)
    tie_b = _candidate("tie-b", "Routine update", weight=6, age_hours=2)
    expected_ties = sorted([tie_a, tie_b], key=lambda item: item.id)
    items = [tie_b, same_score_older, highest, tie_a, newest]
    snapshot = [item.model_dump() for item in items]

    assert select_candidates(items, NOW) == [
        highest,
        newest,
        same_score_older,
        *expected_ties,
    ]
    assert [item.model_dump() for item in items] == snapshot
    assert select_candidates(items, NOW) == select_candidates(items, NOW)


def test_select_candidates_enforces_limit_without_mutating_input() -> None:
    items = [_candidate(f"item-{index}", "Routine update", weight=index + 1) for index in range(4)]

    assert len(select_candidates(items, NOW, limit=2)) == 2
    assert select_candidates(items, NOW, limit=0) == []
    assert len(items) == 4
    with pytest.raises(ValueError, match="limit"):
        select_candidates(items, NOW, limit=-1)
