from __future__ import annotations

from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from itertools import pairwise, permutations

import pytest

from ai_news.models import Candidate, Category, RawItem, SourceRole
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
    url_slug: str | None = None,
    source_id: str | None = None,
    source_name: str | None = None,
    excerpt: str = "Summary",
    official: bool = False,
    source_role: SourceRole | None = None,
    discovery_verified: bool = False,
) -> Candidate:
    return to_candidate(
        RawItem(
            source_id=source_id or f"source-{slug}",
            source_name=source_name or f"Source {slug}",
            source_weight=weight,
            title=title,
            url=f"https://{host}/{url_slug or slug}",
            published_at=published_at,
            excerpt=excerpt,
            category_hint=Category.MODEL,
            is_official_source=official,
            source_role=source_role,
            discovery_verified=discovery_verified,
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


def test_deduplicate_merges_all_pairwise_duplicate_groups_and_is_order_independent() -> None:
    first = _candidate("first", "Model release abcdefghijklmnop", weight=6)
    bridge = _candidate("bridge", "Model release abcdefghijklmnXp", weight=9)
    last = _candidate("last", "Model release abcdefghijklYnXp", weight=7)
    titles = [item.raw.title.casefold() for item in [first, bridge, last]]

    assert SequenceMatcher(None, titles[0], titles[1]).ratio() >= 0.92
    assert SequenceMatcher(None, titles[1], titles[2]).ratio() >= 0.92
    assert SequenceMatcher(None, titles[0], titles[2]).ratio() >= 0.92
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


def test_deduplicate_treats_sequence_similarity_symmetrically() -> None:
    first = _candidate("asymmetric-a", "bddbcbbabcac", host="first.example")
    second = _candidate("asymmetric-b", "bddbcbbabacadc", host="second.example")

    assert SequenceMatcher(None, first.raw.title, second.raw.title).ratio() >= 0.92
    assert SequenceMatcher(None, second.raw.title, first.raw.title).ratio() < 0.92
    expected = sorted([first, second], key=lambda item: item.id)
    assert deduplicate([first, second], set()) == expected
    assert deduplicate([second, first], set()) == expected


def test_deduplicate_uses_full_stable_tie_break_for_every_input_permutation() -> None:
    candidates = [
        _candidate(
            "tie-z",
            "Shared title Z",
            url_slug="shared",
            source_id="z-source",
            source_name="Alpha",
            excerpt="A",
        ),
        _candidate(
            "tie-a",
            "Shared title A",
            url_slug="shared",
            source_id="a-source",
            source_name="Zulu",
            excerpt="Z",
        ),
        _candidate(
            "tie-m",
            "Shared title M",
            url_slug="shared",
            source_id="m-source",
            source_name="Middle",
            excerpt="M",
        ),
    ]
    expected = candidates[1]

    for ordered_candidates in permutations(candidates):
        assert deduplicate(list(ordered_candidates), set()) == [expected]


def test_deduplicate_does_not_collapse_a_long_similarity_bridge_into_one_group() -> None:
    base_title = "abcdefghij klmnopqrst"
    titles: list[str] = []
    for changed_characters in range(11):
        characters = list(base_title)
        characters[:changed_characters] = ["X"] * changed_characters
        titles.append("".join(characters))
    candidates = [
        _candidate(f"bridge-{index}", title, weight=index % 3 + 1)
        for index, title in enumerate(titles)
    ]

    assert all(
        SequenceMatcher(None, first, second).ratio() >= 0.92 for first, second in pairwise(titles)
    )
    assert SequenceMatcher(None, titles[0], titles[-1]).ratio() < 0.60

    forward = deduplicate(candidates, set())
    reverse = deduplicate(list(reversed(candidates)), set())

    assert len(forward) > 1
    assert forward == reverse


def test_deduplicate_bounds_fuzzy_title_comparison_to_256_normalized_characters() -> None:
    prefix = "A" * 256
    first = _candidate(
        "long-a",
        prefix + "B" * 744,
        host="first.example",
        weight=4,
    )
    winner = _candidate(
        "long-b",
        prefix + "C" * 744,
        host="second.example",
        weight=9,
    )

    assert deduplicate([first, winner], set()) == [winner]


def test_deduplicate_uses_cheap_filters_before_expensive_large_title_comparisons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = [
        _candidate(
            f"bulk-{index}",
            chr(0x4E00 + index) * 1000,
            host=f"source-{index}.example",
        )
        for index in range(120)
    ]
    sequence_matcher_calls = 0
    real_sequence_matcher = SequenceMatcher

    def counting_sequence_matcher(*args: object, **kwargs: object) -> SequenceMatcher:
        nonlocal sequence_matcher_calls
        sequence_matcher_calls += 1
        return real_sequence_matcher(*args, **kwargs)

    monkeypatch.setattr(
        "ai_news.pipeline.dedupe.SequenceMatcher",
        counting_sequence_matcher,
    )

    assert len(deduplicate(candidates, set())) == len(candidates)
    assert sequence_matcher_calls <= len(candidates)


def test_deduplicate_rejects_more_than_500_candidates() -> None:
    candidate = _candidate("bounded", "Bounded input")

    with pytest.raises(ValueError, match="500"):
        deduplicate([candidate] * 501, set())


def test_deduplicate_prefers_primary_then_trusted_media_then_discovery() -> None:
    primary = _candidate(
        "primary",
        "Official launch details",
        url_slug="shared-event",
        weight=1,
        published_at=BASE_TIME - timedelta(hours=2),
        official=True,
        source_role=SourceRole.PRIMARY,
    )
    trusted = _candidate(
        "trusted",
        "Media launch details",
        url_slug="shared-event",
        weight=10,
        published_at=BASE_TIME + timedelta(hours=1),
        source_role=SourceRole.TRUSTED_MEDIA,
    )
    discovery = _candidate(
        "discovery",
        "Community launch details",
        url_slug="shared-event",
        weight=10,
        published_at=BASE_TIME + timedelta(hours=2),
        source_role=SourceRole.DISCOVERY,
        discovery_verified=True,
    )

    assert deduplicate([discovery, trusted, primary], set()) == [primary]
    assert deduplicate([discovery, trusted], set()) == [trusted]


def test_deduplicate_collapses_cross_source_product_launch_event_to_primary() -> None:
    primary = _candidate(
        "gpt5-primary",
        "OpenAI launches GPT-5",
        host="openai.example",
        weight=1,
        official=True,
        source_role=SourceRole.PRIMARY,
    )
    trusted = _candidate(
        "gpt5-media",
        "GPT-5 officially released by OpenAI",
        host="media.example",
        weight=10,
        source_role=SourceRole.TRUSTED_MEDIA,
    )
    discovery = _candidate(
        "gpt5-community",
        "GPT-5 正式发布",
        host="community.example",
        weight=10,
        source_role=SourceRole.DISCOVERY,
        discovery_verified=True,
    )

    assert deduplicate([trusted, discovery, primary], set()) == [primary]


def test_deduplicate_event_key_does_not_merge_different_products_or_actions() -> None:
    gpt5_launch = _candidate(
        "gpt5-launch",
        "OpenAI launches GPT-5",
        host="first.example",
        official=True,
    )
    gpt4_launch = _candidate(
        "gpt4-launch",
        "OpenAI launches GPT-4.1",
        host="second.example",
        official=True,
    )
    gpt5_price = _candidate(
        "gpt5-price",
        "OpenAI changes GPT-5 pricing",
        host="third.example",
        official=True,
    )

    assert deduplicate([gpt5_launch, gpt4_launch, gpt5_price], set()) == sorted(
        [gpt5_launch, gpt4_launch, gpt5_price],
        key=lambda item: item.id,
    )
