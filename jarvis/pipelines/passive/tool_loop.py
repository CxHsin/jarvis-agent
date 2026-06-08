from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from jarvis.config import RuntimeConfig
from jarvis.services.llm import LlmClient
from jarvis.tools.runtime import ToolRuntime

logger = logging.getLogger(__name__)
_TOOL_LOOP_REPEAT_LIMIT = 3
_FINAL_ANSWER_PROMPT = (
    "Stop calling tools. Using only the tool results already in this conversation, "
    "answer the user directly. If the available data is incomplete, say what you do know "
    "and note the limitation briefly."
)


@dataclass
class ToolLoopOutcome:
    assistant_message: dict[str, Any]
    messages: list[dict[str, Any]]
    tool_rounds: int


class PassiveToolLoop:
    def __init__(
        self,
        config: RuntimeConfig,
        llm: LlmClient,
        tools: ToolRuntime,
    ) -> None:
        self._config = config
        self._llm = llm
        self._tools = tools

    async def run(self, chat_id: str, messages: list[dict[str, Any]]) -> ToolLoopOutcome:
        unlocked_tools: set[str] = set()
        last_tool_signature = ""
        repeated_tool_signature_count = 0
        assistant_message = await self._llm.complete(messages, self._tools.definitions(unlocked_tools))
        tool_rounds = 0

        while assistant_message["tool_calls"] and tool_rounds < self._config.max_tool_round_trips:
            signature = self._tool_call_signature(assistant_message["tool_calls"])
            if signature:
                if signature == last_tool_signature:
                    repeated_tool_signature_count += 1
                else:
                    last_tool_signature = signature
                    repeated_tool_signature_count = 1
                if repeated_tool_signature_count >= _TOOL_LOOP_REPEAT_LIMIT:
                    logger.warning(
                        "chat_id=%s repeated_tool_signature_detected count=%s signature=%s",
                        chat_id,
                        repeated_tool_signature_count,
                        signature,
                    )
                    messages.append(_assistant_tool_message(assistant_message))
                    for call in assistant_message["tool_calls"]:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": "Skipped because the same tool call pattern repeated too many times.",
                            }
                        )
                    break
            tool_rounds += 1
            messages.append(_assistant_tool_message(assistant_message))
            for call in assistant_message["tool_calls"]:
                result = await self._tools.execute(
                    call["name"],
                    call["arguments"],
                    chat_id=chat_id,
                    unlocked_tools=unlocked_tools,
                )
                if call["name"] == "tool_search" and result.success and result.structured:
                    unlocked_tools.update(result.structured.get("selected_names", []))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": result.content,
                    }
                )
            assistant_message = await self._llm.complete(messages, self._tools.definitions(unlocked_tools))

        if assistant_message["tool_calls"]:
            logger.warning(
                "chat_id=%s tool_round_limit_reached=%s pending_tool_calls=%s",
                chat_id,
                self._config.max_tool_round_trips,
                len(assistant_message["tool_calls"]),
            )
            messages.append({"role": "system", "content": _FINAL_ANSWER_PROMPT})
            assistant_message = await self._llm.complete(messages, [])

        return ToolLoopOutcome(
            assistant_message=assistant_message,
            messages=messages,
            tool_rounds=tool_rounds,
        )

    def _tool_call_signature(self, tool_calls: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for call in tool_calls:
            args = json.dumps(call["arguments"], ensure_ascii=False, sort_keys=True)
            parts.append(f'{call["name"]}:{args}')
        return "|".join(parts)


def _assistant_tool_message(assistant_message: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": assistant_message["content"] or "",
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                },
            }
            for call in assistant_message["tool_calls"]
        ],
    }
