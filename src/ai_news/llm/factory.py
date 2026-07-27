from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from ai_news.llm.anthropic import AnthropicAnalyzer
from ai_news.llm.base import BaseAnalyzer
from ai_news.llm.openai_chat import OpenAIChatAnalyzer
from ai_news.models import Settings

__all__ = ["create_analyzer"]


def create_analyzer(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> BaseAnalyzer:
    if settings.llm_protocol == "openai-chat":
        return OpenAIChatAnalyzer(settings, client=client, sleep=sleep)
    if settings.llm_protocol == "anthropic":
        return AnthropicAnalyzer(settings, client=client, sleep=sleep)
    raise ValueError("unsupported LLM protocol")
