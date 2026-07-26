from __future__ import annotations

from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from ai_news.models import Candidate, Category, RawItem
from ai_news.pipeline.dedupe import deduplicate
from ai_news.pipeline.normalize import to_candidate

BASE_TIME = datetime(2026, 7, 25, 12, tzinfo=UTC)


def _candidate(
    slug: str,
    title: str,
    *,
    host: str = "example.com",
    weight: int = 5,
    published_at: datetime = BASE_TIME,
) -> Candidate:
    return to_candidate(
        RawItem(
            source_id=f"source-{slug}",
            source_name=f"Source {slug}",
            source_weight=weight,
            title=title,
            url=f"https://{host}/{slug}",
            published_at=published_at,
            excerpt="Summary",
            category_hint=Category.MODEL,
            is_official_source=False,
        )
    )


def test_deduplicate_filters_historical_and_same_canonical_url_without_mutating_input() -> None:
    first = _candidate("same", "First version", weight=4)
    duplicate = _candidate("same", "Updated version", weight=9)
    historical = _candidate("historical", "Old item")
    remaining = _candidate("remaining", "Distinct item")
    items = [first, historical, remaining, duplicate]
    snapshot = [item.model_dump() for item in items]

    result = deduplicate(items, {str(historical.canonical_url)})

    assert result == sorted([duplicate, remaining], key=lambda item: item.id)
    assert [item.model_dump() for item in items] == snapshot


def test_deduplicate_matches_casefolded_normalized_title_on_the_same_host() -> None:
    older = _candidate("one", "  MODEL\tLaunch ", weight=4)
    winner = _candidate("two", "model launch", weight=8)

    assert deduplicate([older, winner], set()) == [winner]


def test_deduplicate_applies_fuzzy_title_threshold_across_sources() -> None:
    close = _candidate("close", "OpenAI launches GPT-Next today!", host="news.example")
    original = _candidate("original", "OpenAI launches GPT-Next today", host="official.example")
    different = _candidate("different", "OpenAI previews a robotics dataset", host="third.example")
    close_ratio = SequenceMatcher(
        None,
        original.raw.title.casefold(),
        close.raw.title.casefold(),
    ).ratio()
    different_ratio = SequenceMatcher(
        None,
        original.raw.title.casefold(),
        different.raw.title.casefold(),
    ).ratio()

    assert close_ratio >= 0.92
    assert different_ratio < 0.92
    assert deduplicate([original, close, different], set()) == sorted(
        [min([original, close], key=lambda item: item.id), different],
        key=lambda item: item.id,
    )


def test_deduplicate_chooses_weight_then_newest_time_then_smallest_id() -> None:
    low_weight = _candidate("low", "Shared announcement", weight=7)
    older = _candidate("older", "Shared announcement!", weight=9)
    newer_a = _candidate(
        "newer-a",
        "Shared announcement!!",
        weight=9,
        published_at=BASE_TIME + timedelta(hours=1),
    )
    newer_b = _candidate(
        "newer-b",
        "Shared announcement!!!",
        weight=9,
        published_at=BASE_TIME + timedelta(hours=1),
    )
    expected = min([newer_a, newer_b], key=lambda item: item.id)

    assert deduplicate([newer_b, low_weight, older, newer_a], set()) == [expected]


def test_deduplicate_merges_transitive_duplicate_groups_and_is_order_independent() -> None:
    first = _candidate("first", "Model release abcdefghij", weight=6)
    bridge = _candidate("bridge", "Model release abXdefghij", weight=9)
    last = _candidate("last", "Model release abXdefgYij", weight=7)
    titles = [item.raw.title.casefold() for item in [first, bridge, last]]

    assert SequenceMatcher(None, titles[0], titles[1]).ratio() >= 0.92
    assert SequenceMatcher(None, titles[1], titles[2]).ratio() >= 0.92
    assert SequenceMatcher(None, titles[0], titles[2]).ratio() < 0.92
    assert deduplicate([first, bridge, last], set()) == [bridge]
    assert deduplicate([last, first, bridge], set()) == [bridge]


def test_deduplicate_keeps_genuinely_different_titles_and_is_repeatable() -> None:
    items = [
        _candidate("research", "New protein folding research"),
        _candidate("product", "Enterprise coding agent launch"),
        _candidate("policy", "AI safety policy consultation"),
    ]

    first_run = deduplicate(items, set())
    second_run = deduplicate(items, set())

    assert first_run == sorted(items, key=lambda item: item.id)
    assert second_run == first_run
