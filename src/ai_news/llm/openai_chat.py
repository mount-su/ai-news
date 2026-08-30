from __future__ import annotations

import json
from typing import Any

from ai_news.llm.base import AnalysisError, BaseAnalyzer
from ai_news.llm.prompt import SYSTEM_PROMPT

__all__ = ["AnalysisError", "OpenAIChatAnalyzer"]


class OpenAIChatAnalyzer(BaseAnalyzer):
    endpoint_suffix = "chat/completions"
    normalize_provider_drift = True

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _body(self, prompt: str) -> dict[str, Any]:
        return {
            "model": self._model,
            "temperature": 0.2,
            "max_tokens": 6000,
            "n": 1,
            "thinking": {"type": "disabled"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }

    def _extract_text(self, response_body: bytes) -> str | None:
        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return None
        message = first_choice.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        return content
