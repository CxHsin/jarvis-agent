from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from jarvis.config import LlmConfig


class LlmClient:
    def __init__(self, config: LlmConfig) -> None:
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=float(config.timeout_seconds),
        )
        self._model = config.model

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
        )
        message = response.choices[0].message
        tool_calls = []
        for call in message.tool_calls or []:
            tool_calls.append(
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": json.loads(call.function.arguments or "{}"),
                }
            )
        return {
            "content": message.content or "",
            "tool_calls": tool_calls,
        }
