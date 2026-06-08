from __future__ import annotations

import logging

from jarvis.config import AppConfig
from jarvis.pipelines.passive.context import PassiveContextAssembler
from jarvis.pipelines.passive.finalize import PassiveTurnFinalizer
from jarvis.pipelines.passive.tool_loop import PassiveToolLoop
from jarvis.runtime.event_bus import EventBus
from jarvis.services.llm import LlmClient
from jarvis.services.sessions import SessionStore
from jarvis.tools.runtime import ToolRuntime

logger = logging.getLogger(__name__)


class PassivePipeline:
    def __init__(
        self,
        config: AppConfig,
        llm: LlmClient,
        sessions: SessionStore,
        tools: ToolRuntime,
        event_bus: EventBus | None = None,
    ) -> None:
        self._config = config
        self._context = PassiveContextAssembler(sessions)
        self._tool_loop = PassiveToolLoop(config.runtime, llm, tools)
        self._finalizer = PassiveTurnFinalizer(sessions)
        self._event_bus = event_bus or EventBus()

    async def run_turn(self, chat_id: str, user_text: str) -> str:
        messages = self._context.start_turn(chat_id, user_text)
        payload = await self._event_bus.emit(
            "passive.context_built",
            {
                "chat_id": chat_id,
                "user_text": user_text,
                "messages": messages,
            },
        )
        messages = payload["messages"]
        outcome = await self._tool_loop.run(chat_id, messages)
        tool_payload = await self._event_bus.emit(
            "passive.tool_loop_completed",
            {
                "chat_id": chat_id,
                "user_text": user_text,
                "messages": outcome.messages,
                "assistant_message": outcome.assistant_message,
                "tool_rounds": outcome.tool_rounds,
            },
        )
        final_text = self._finalizer.finalize_reply(chat_id, tool_payload["assistant_message"]["content"])
        reply_payload = await self._event_bus.emit(
            "passive.reply_finalized",
            {
                "chat_id": chat_id,
                "user_text": user_text,
                "reply": final_text,
                "tool_rounds": tool_payload["tool_rounds"],
            },
        )
        final_text = str(reply_payload["reply"])
        logger.info("chat_id=%s tool_rounds=%s reply_chars=%s", chat_id, outcome.tool_rounds, len(final_text))
        return final_text
