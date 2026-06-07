import json
import logging

from app.llm_client import ChatMessage
from app.tools.base import ToolCall, ToolResult, parse_tool_call
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolLoop:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        executor: ToolExecutor,
        max_tool_steps: int = 3,
    ) -> None:
        self._registry = registry
        self._executor = executor
        self._max_tool_steps = max_tool_steps

    def run(self, *, llm_client: object, messages: list[ChatMessage]) -> str:
        if not self._registry.list_specs():
            return llm_client.chat(messages)  # type: ignore[attr-defined]

        loop_messages = self._with_tool_instructions(messages)
        tool_steps = 0
        limit_notified = False

        while True:
            reply_text = llm_client.chat(loop_messages)  # type: ignore[attr-defined]
            tool_call = parse_tool_call(reply_text)
            if tool_call is None:
                return reply_text

            logger.info("Tool request received: %s", tool_call.tool_name)
            loop_messages.append(ChatMessage(role="assistant", content=reply_text))

            if tool_steps >= self._max_tool_steps:
                if limit_notified:
                    logger.info("Tool loop terminated after repeated limit hit")
                    return "I'm unable to complete that request with the available tool limit."
                limit_notified = True
                result = ToolResult(
                    status="error",
                    tool_name=tool_call.tool_name,
                    content="Tool step limit reached for this turn.",
                    error_code="tool_limit_reached",
                )
                loop_messages.append(_result_message(result))
                logger.info("Tool loop reached step limit")
                continue

            tool_steps += 1
            result = self._executor.execute(tool_call)
            logger.info(
                "Tool execution finished: tool=%s status=%s error=%s",
                result.tool_name,
                result.status,
                result.error_code,
            )
            loop_messages.append(_result_message(result))

    def _with_tool_instructions(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        tool_lines = []
        for spec in self._registry.list_specs():
            tool_lines.append(
                f"- {spec.name}: {spec.description} args={', '.join(spec.arguments) or '(none)'}"
            )
        instruction = (
            "Tool protocol:\n"
            "- If a tool is needed, reply with a JSON object only.\n"
            '- Use format: {"type":"tool_call","tool_name":"...","arguments":{"key":"value"}}\n'
            "- If no tool is needed, reply with normal assistant text.\n"
            "- Available tools:\n"
            f"{chr(10).join(tool_lines)}"
        )
        return [*messages, ChatMessage(role="system", content=instruction)]


def _result_message(result: ToolResult) -> ChatMessage:
    payload = {
        "type": "tool_result",
        "tool_name": result.tool_name,
        "status": result.status,
        "content": result.content,
    }
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    return ChatMessage(role="system", content=json.dumps(payload, ensure_ascii=True))
