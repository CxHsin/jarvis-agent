from __future__ import annotations

import logging

from jarvis.config import AppConfig
from jarvis.memory import MemoryEngine, TurnCommitted, build_memory_engine
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
        sessions: SessionStore | None,
        tools: ToolRuntime,
        memory: MemoryEngine | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._config = config
        self._memory = memory or build_memory_engine(config, context_store=sessions.context_store if sessions is not None else None)
        self._context = PassiveContextAssembler(self._memory)
        self._tool_loop = PassiveToolLoop(config.runtime, llm, tools)
        self._finalizer = PassiveTurnFinalizer(self._memory.context_store)
        self._event_bus = event_bus or EventBus()

    async def run_turn(self, chat_id: str, user_text: str) -> str:
        prepared = self._context.begin_turn(chat_id, user_text)
        messages = prepared.messages
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
        normalized_reply = self._finalizer.normalize_reply(tool_payload["assistant_message"]["content"])
        reply_payload = await self._event_bus.emit(
            "passive.reply_finalized",
            {
                "chat_id": chat_id,
                "user_text": user_text,
                "reply": normalized_reply,
                "tool_rounds": tool_payload["tool_rounds"],
            },
        )
        final_text = self._finalizer.normalize_reply(str(reply_payload["reply"]))
        assistant_turn = self._finalizer.commit_reply(chat_id, final_text)
        if prepared.user_turn is not None and assistant_turn is not None:
            try:
                self._memory.on_turn_committed(
                    TurnCommitted(
                        session_key=chat_id,
                        user_turn=prepared.user_turn,
                        assistant_turn=assistant_turn,
                    )
                )
            except Exception:
                logger.exception("memory maintenance failed for chat_id=%s", chat_id)
        logger.info("chat_id=%s tool_rounds=%s reply_chars=%s", chat_id, outcome.tool_rounds, len(final_text))
        return final_text
