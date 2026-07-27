from __future__ import annotations

import asyncio
from typing import Any, Literal

import httpx
import pytest
from pydantic import SecretStr

from ai_news.llm.anthropic import AnthropicAnalyzer
from ai_news.llm.base import AnalysisError, BaseAnalyzer
from ai_news.llm.factory import create_analyzer
from ai_news.llm.openai_chat import OpenAIChatAnalyzer
from ai_news.models import Settings


def _settings(protocol: Literal["openai-chat", "anthropic"]) -> Settings:
    return Settings(
        llm_protocol=protocol,
        llm_base_url="https://ark.cn-beijing.volces.com/api/v3",
        llm_token=SecretStr("top-secret-token"),
        llm_model="ark-standard-model",
        llm_auth_scheme="bearer",
        site_base_path="/ai-news/",
    )


@pytest.mark.parametrize(
    ("settings", "expected_type"),
    [
        (_settings("openai-chat"), OpenAIChatAnalyzer),
        (_settings("anthropic"), AnthropicAnalyzer),
    ],
)
def test_create_analyzer_selects_protocol_and_passes_dependencies(
    settings: Settings,
    expected_type: type[BaseAnalyzer],
) -> None:
    client = httpx.AsyncClient()

    async def no_sleep(_: float) -> None:
        return None

    try:
        analyzer = create_analyzer(settings, client=client, sleep=no_sleep)
    finally:
        asyncio.run(client.aclose())

    assert isinstance(analyzer, expected_type)
    assert analyzer._client is client
    assert analyzer._sleep is no_sleep


def test_create_analyzer_rejects_protocol_outside_settings_invariant() -> None:
    settings = Settings.model_construct(
        llm_protocol="unsupported",
        llm_base_url="https://ark.cn-beijing.volces.com/api/v3",
        llm_token=SecretStr("top-secret-token"),
        llm_model="ark-standard-model",
        llm_auth_scheme="bearer",
        site_base_path="/ai-news/",
    )

    with pytest.raises(ValueError, match="unsupported LLM protocol"):
        create_analyzer(settings)


def test_base_analyzer_rejects_missing_endpoint_suffix_with_safe_error() -> None:
    class MissingEndpointAnalyzer(BaseAnalyzer):
        endpoint_suffix = ""

        def _headers(self) -> dict[str, str]:
            return {}

        def _body(self, prompt: str) -> dict[str, Any]:
            return {"prompt": prompt}

        def _extract_text(self, response_body: bytes) -> str | None:
            return response_body.decode()

    with pytest.raises(AnalysisError, match="invalid LLM base URL") as exc_info:
        MissingEndpointAnalyzer(_settings("anthropic"))

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert "top-secret-token" not in rendered
