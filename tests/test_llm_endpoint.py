from __future__ import annotations

import httpx
import pytest

from ai_news.llm.endpoint import (
    InvalidLLMEndpoint,
    build_llm_endpoint,
    validate_llm_base_url,
)


@pytest.mark.parametrize(
    ("base_url", "suffix", "expected"),
    [
        (
            "https://ark.cn-beijing.volces.com/api/v3/",
            "chat/completions",
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        ),
        (
            "https://api.anthropic.com",
            "v1/messages",
            "https://api.anthropic.com/v1/messages",
        ),
        (
            "https://proxy.example/tenant",
            "/v1/messages",
            "https://proxy.example/tenant/v1/messages",
        ),
    ],
)
def test_build_llm_endpoint_preserves_safe_base_path(
    base_url: str,
    suffix: str,
    expected: str,
) -> None:
    endpoint = build_llm_endpoint(base_url, suffix)

    assert isinstance(endpoint, httpx.URL)
    assert str(endpoint) == expected
    assert endpoint.query == b""
    assert endpoint.fragment == ""


@pytest.mark.parametrize(
    "base_url",
    [
        "https://ark.cn-beijing.volces.com/api/coding",
        "https://ark.cn-beijing.volces.com/api/coding/",
        "https://ark.cn-beijing.volces.com/api/coding/v3",
        "https://ARK.CN-BEIJING.VOLCES.COM/api/v3/../coding",
        "https://ark.cn-beijing.volces.com/api%2fcoding",
        "https://ark.cn-beijing.volces.com/api%252fcoding",
        "https://ark.cn-beijing.volces.com/api%5ccoding",
    ],
)
def test_validate_llm_base_url_rejects_ark_coding_plan(base_url: str) -> None:
    with pytest.raises(InvalidLLMEndpoint, match="not allowed"):
        validate_llm_base_url(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.com/v1",
        "https://user:password@api.example.com/v1",
        "https://api.example.com/v1?token=value",
        "https://api.example.com/v1#fragment",
        "https://api.example.com/a\\b",
        "https://api.example.com/%00",
        "https:///missing-host",
    ],
)
def test_validate_llm_base_url_rejects_unsafe_components(base_url: str) -> None:
    with pytest.raises(InvalidLLMEndpoint):
        validate_llm_base_url(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.com/v1?",
        "https://api.example.com/v1#",
    ],
)
def test_validate_llm_base_url_rejects_empty_query_or_fragment_delimiter(
    base_url: str,
) -> None:
    with pytest.raises(InvalidLLMEndpoint):
        validate_llm_base_url(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.com:invalid/v1",
        "https://api.example.com:99999/v1",
        "https://api.example.com:/v1",
    ],
)
def test_validate_llm_base_url_rejects_malformed_ports(base_url: str) -> None:
    with pytest.raises(InvalidLLMEndpoint):
        validate_llm_base_url(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.com/tenant%2fadmin",
        "https://api.example.com/tenant%255cadmin",
    ],
)
def test_validate_llm_base_url_rejects_iteratively_encoded_separators(
    base_url: str,
) -> None:
    with pytest.raises(InvalidLLMEndpoint):
        validate_llm_base_url(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.com/a/../../v1",
        "https://api.example.com/%2e%2e/v1",
        "https://api.example.com/tenant//v1",
    ],
)
def test_validate_llm_base_url_rejects_unsafe_normalized_paths(base_url: str) -> None:
    with pytest.raises(InvalidLLMEndpoint):
        validate_llm_base_url(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.com//",
        "https://api.example.com/tenant//",
    ],
)
def test_validate_llm_base_url_rejects_repeated_trailing_slashes(base_url: str) -> None:
    with pytest.raises(InvalidLLMEndpoint):
        validate_llm_base_url(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.com/",
        "https://api.example.com/tenant/",
    ],
)
def test_validate_llm_base_url_allows_one_trailing_slash(base_url: str) -> None:
    validate_llm_base_url(base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api%00.example.com/v1",
        "https://api%2500.example.com/v1",
        "https://api%2f.example.com/v1",
        "https://api%252f.example.com/v1",
        "https://api%5c.example.com/v1",
        "https://api%255c.example.com/v1",
    ],
)
def test_validate_llm_base_url_rejects_encoded_authority_controls_and_separators(
    base_url: str,
) -> None:
    with pytest.raises(InvalidLLMEndpoint) as captured:
        validate_llm_base_url(base_url)

    assert base_url not in str(captured.value)


def test_validate_llm_base_url_wraps_invalid_ipv4_parser_error() -> None:
    base_url = "https://127.0.0.999/v1"

    with pytest.raises(InvalidLLMEndpoint) as captured:
        validate_llm_base_url(base_url)

    assert base_url not in str(captured.value)


def test_build_llm_endpoint_preserves_ipv6_authority() -> None:
    endpoint = build_llm_endpoint("https://[2001:db8::1]:8443/tenant/", "v1/messages")

    assert str(endpoint) == "https://[2001:db8::1]:8443/tenant/v1/messages"


def test_build_llm_endpoint_canonicalizes_host_case() -> None:
    endpoint = build_llm_endpoint("https://API.Example.COM/tenant/", "chat/completions")

    assert str(endpoint) == "https://api.example.com/tenant/chat/completions"


def test_invalid_error_does_not_expose_input_or_credentials() -> None:
    unsafe_url = "https://sensitive-user:sensitive-password@api.example.com/v1"

    with pytest.raises(InvalidLLMEndpoint) as captured:
        validate_llm_base_url(unsafe_url)

    message = str(captured.value)
    assert unsafe_url not in message
    assert "sensitive-user" not in message
    assert "sensitive-password" not in message
