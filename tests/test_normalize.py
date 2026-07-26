from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest
from pydantic import HttpUrl, TypeAdapter, ValidationError

from ai_news.models import Candidate, Category, RawItem
from ai_news.pipeline.normalize import (
    TRACKING_PARAMS,
    canonical_url,
    ensure_utc,
    normalize_title,
    to_candidate,
)

TIMEZONE_PLUS_EIGHT = timezone(timedelta(hours=8))
HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)


def _raw_item(
    *,
    url: str = "https://Example.com/post/?utm_source=x&ref=home&id=7#section",
    title: str = "  Model\tLaunch  ",
    published_at: datetime = datetime(2026, 7, 25, 18, 30, tzinfo=TIMEZONE_PLUS_EIGHT),
) -> RawItem:
    return RawItem(
        source_id="example",
        source_name="Example",
        source_weight=8,
        title=title,
        url=url,
        published_at=published_at,
        excerpt="Summary",
        category_hint=Category.MODEL,
        is_official_source=True,
    )


def _direct_candidate_data(
    canonical: str,
    *,
    raw_url: str | None = None,
) -> dict[str, object]:
    return {
        "id": hashlib.sha256(canonical.encode()).hexdigest()[:16],
        "canonical_url": canonical,
        "raw": _raw_item(url=raw_url or canonical),
    }


def test_tracking_params_are_the_exact_supported_set() -> None:
    assert {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "ref",
        "source",
    } == TRACKING_PARAMS


def test_canonical_url_removes_tracking_fragment_and_normalizes_host_and_path() -> None:
    assert (
        canonical_url(
            "https://Example.com/post/?utm_source=x&ref=home&id=7#section",
        )
        == "https://example.com/post?id=7"
    )


def test_canonical_url_retains_duplicate_query_values_in_stable_encoded_order() -> None:
    assert (
        canonical_url("https://EXAMPLE.com/items/?b=two words&a=2&a=1&A=0&empty=")
        == "https://example.com/items?A=0&a=1&a=2&b=two+words&empty="
    )


@pytest.mark.parametrize(
    ("unicode_host", "ascii_host"),
    [
        ("faß.de", "xn--fa-hia.de"),
        ("βόλος.com", "xn--nxasmm1c.com"),
    ],
)
def test_canonical_url_uses_pydantic_idna_for_strings_and_http_urls(
    unicode_host: str,
    ascii_host: str,
) -> None:
    value = f"https://{unicode_host}/news"
    parsed_value = HTTP_URL_ADAPTER.validate_python(value)

    assert canonical_url(value) == f"https://{ascii_host}/news"
    assert canonical_url(parsed_value) == f"https://{ascii_host}/news"


def test_canonical_url_strips_a_dns_root_dot() -> None:
    assert canonical_url("https://example.com./news") == "https://example.com/news"
    assert canonical_url("https://example.com./news") == canonical_url("https://example.com/news")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://EXAMPLE.com:443", "https://example.com/"),
        ("https://EXAMPLE.com:8443/path/", "https://example.com:8443/path"),
        ("https://[2001:DB8::1]:443/", "https://[2001:db8::1]/"),
        (
            "https://example.com/新闻/%e6%b5%8b%e8%af%95/",
            "https://example.com/%E6%96%B0%E9%97%BB/%E6%B5%8B%E8%AF%95",
        ),
    ],
)
def test_canonical_url_normalizes_ports_ipv6_and_path_encoding(
    url: str,
    expected: str,
) -> None:
    assert canonical_url(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a/../b", "https://example.com/b"),
        ("https://example.com/safe/%2e%2e/admin", "https://example.com/admin"),
    ],
)
def test_canonical_path_is_identical_for_string_http_url_and_raw_item(
    url: str,
    expected: str,
) -> None:
    parsed_url = HTTP_URL_ADAPTER.validate_python(url)
    raw = _raw_item(url=url)

    assert canonical_url(url) == expected
    assert canonical_url(parsed_url) == expected
    assert str(to_candidate(raw).canonical_url) == expected


def test_canonical_path_does_not_recursively_decode_double_encoded_dot_segments() -> None:
    url = "https://example.com/safe/%252e%252e/admin"
    expected = "https://example.com/safe/%252e%252e/admin"
    parsed_url = HTTP_URL_ADAPTER.validate_python(url)
    raw = _raw_item(url=url)

    assert canonical_url(url) == expected
    assert canonical_url(parsed_url) == expected
    assert str(to_candidate(raw).canonical_url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        " ",
        "http://example.com/post",
        "https:///missing-host",
        "https://user:secret@example.com/post",
        "https://[broken",
        "https://example.com:bad/post",
        "https://example.com/%zz",
        "https://example.com/\ud800",
    ],
)
def test_canonical_url_rejects_invalid_or_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError, match="URL|url|https|host|userinfo|port|encoding"):
        canonical_url(url)


def test_normalize_title_uses_nfkc_and_collapses_whitespace_without_changing_case() -> None:
    assert normalize_title("  \uff21\uff29\t新品\n发布  ") == "AI 新品 发布"


@pytest.mark.parametrize("title", ["", " \t\n ", "\u3000"])
def test_normalize_title_rejects_empty_results(title: str) -> None:
    with pytest.raises(ValueError, match="title"):
        normalize_title(title)


def test_ensure_utc_converts_aware_datetime_and_rejects_naive_datetime() -> None:
    source = datetime(2026, 7, 25, 18, 30, tzinfo=TIMEZONE_PLUS_EIGHT)

    assert ensure_utc(source) == datetime(2026, 7, 25, 10, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone"):
        ensure_utc(datetime(2026, 7, 25, 10, 30))


class _InvalidTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None


def test_ensure_utc_rejects_timezone_with_no_valid_offset() -> None:
    with pytest.raises(ValueError, match="timezone"):
        ensure_utc(datetime(2026, 7, 25, 10, 30, tzinfo=_InvalidTimezone()))


def test_to_candidate_normalizes_a_copy_and_uses_stable_canonical_url_hash() -> None:
    raw = _raw_item()
    original = raw.model_dump()

    candidate = to_candidate(raw)

    assert candidate.id == "8810464c45f302f7"
    assert str(candidate.canonical_url) == "https://example.com/post?id=7"
    assert candidate.raw.title == "Model Launch"
    assert candidate.raw.published_at == datetime(2026, 7, 25, 10, 30, tzinfo=UTC)
    assert candidate.raw is not raw
    assert raw.model_dump() == original


def test_candidate_forbids_unknown_fields() -> None:
    data = to_candidate(_raw_item()).model_dump()

    with pytest.raises(ValidationError):
        Candidate(**data, unexpected=True)


def test_candidate_requires_a_16_character_hex_id() -> None:
    data = to_candidate(_raw_item()).model_dump()
    data["id"] = "not-an-id"

    with pytest.raises(ValidationError):
        Candidate.model_validate(data)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/news",
        "https://user:secret@example.com/news",
        "https://example.com/news/",
        "https://example.com/news?utm_source=tracker",
    ],
)
def test_candidate_rejects_noncanonical_or_unsafe_canonical_url(url: str) -> None:
    with pytest.raises(ValidationError, match="canonical"):
        Candidate.model_validate(_direct_candidate_data(url))


def test_candidate_rejects_input_that_pydantic_would_silently_canonicalize() -> None:
    data = _direct_candidate_data("https://example.com/news")
    data["canonical_url"] = "https://EXAMPLE.com/news"

    with pytest.raises(ValidationError, match="canonical"):
        Candidate.model_validate(data)


def test_candidate_rejects_id_that_does_not_match_canonical_url_hash() -> None:
    data = _direct_candidate_data("https://example.com/news")
    data["id"] = "0000000000000000"

    with pytest.raises(ValidationError, match="id"):
        Candidate.model_validate(data)


def test_candidate_rejects_raw_url_that_canonicalizes_to_another_url() -> None:
    data = _direct_candidate_data(
        "https://example.com/news",
        raw_url="https://example.com/other",
    )

    with pytest.raises(ValidationError, match="raw"):
        Candidate.model_validate(data)


def test_candidate_is_frozen() -> None:
    candidate = to_candidate(_raw_item())

    with pytest.raises(ValidationError, match="frozen"):
        candidate.id = "0000000000000000"


def test_candidate_nested_raw_item_is_frozen() -> None:
    candidate = to_candidate(_raw_item())

    with pytest.raises(ValidationError, match="frozen"):
        candidate.raw.title = "Mutated"
