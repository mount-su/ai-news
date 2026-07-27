from __future__ import annotations

import json
from typing import Any

from ai_news.llm.base import AnalysisError, BaseAnalyzer
from ai_news.llm.prompt import SYSTEM_PROMPT

__all__ = ["AnalysisError", "AnthropicAnalyzer"]


class AnthropicAnalyzer(BaseAnalyzer):
    endpoint_suffix = "v1/messages"

    def _headers(self) -> dict[str, str]:
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if self._auth_scheme == "bearer":
            headers["Authorization"] = f"Bearer {self._token}"
        else:
            headers["x-api-key"] = self._token
        return headers

    def _body(self, prompt: str) -> dict[str, Any]:
        return {
            "model": self._model,
            "max_tokens": 6000,
            "temperature": 0.2,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }

    def _extract_text(self, response_body: bytes) -> str | None:
        try:
            payload = json.loads(response_body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
            return None
        for block in payload["content"]:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                return text if isinstance(text, str) else None
        return None
