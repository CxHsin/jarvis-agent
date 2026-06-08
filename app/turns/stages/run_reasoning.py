from __future__ import annotations

from app.llm_client import ChatMessage, OpenAICompatibleClient
from app.plugins import PluginHost
from app.plugins.types import ModelCallContext
from app.tools.loop import ToolLoop
from app.turns.context import PassiveTurnContext


class RunReasoningStage:
    def __init__(
        self,
        *,
        llm_client: OpenAICompatibleClient,
        tool_loop: ToolLoop | None = None,
        plugin_host: PluginHost | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._tool_loop = tool_loop
        self._plugin_host = plugin_host

    def run(self, context: PassiveTurnContext) -> PassiveTurnContext:
        messages = list(context.prompt_messages)
        if self._plugin_host is not None:
            before_call_sections = self._plugin_host.before_model_call(
                ModelCallContext(
                    chat_id=context.chat_id,
                    user_text=context.normalized_user_text,
                    messages=tuple(messages),
                    available_tools=context.available_tools,
                    memory_snapshot=context.memory_snapshot,
                )
            )
            messages.extend(ChatMessage(role="system", content=section) for section in before_call_sections)

        if self._tool_loop is None:
            context.reply_text = self._llm_client.chat(messages)
        else:
            context.reply_text = self._tool_loop.run(
                llm_client=self._llm_client,
                messages=messages,
            )
        context.prompt_messages = messages
        return context
