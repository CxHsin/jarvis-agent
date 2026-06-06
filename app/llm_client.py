from dataclasses import dataclass
from typing import Any

import requests


class LLMClientError(RuntimeError):
    """Raised when the OpenAI-compatible API call fails."""


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._session = session or requests.Session()

    def chat(self, messages: list[ChatMessage]) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._session.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LLMClientError("Failed to call OpenAI-compatible API") from exc

        try:
            data = response.json()
            return _extract_text(data)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMClientError("OpenAI-compatible API returned an invalid response") from exc


def _extract_text(data: dict[str, Any]) -> str:
    content = data["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Empty content")
    return content.strip()
