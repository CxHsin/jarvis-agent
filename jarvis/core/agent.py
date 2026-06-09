from __future__ import annotations

from jarvis.config import AppConfig
from jarvis.memory import build_memory_engine
from jarvis.pipelines.passive.runner import PassivePipeline
from jarvis.runtime.event_bus import EventBus
from jarvis.services.llm import LlmClient
from jarvis.services.sessions import SessionStore
from jarvis.tools.runtime import ToolRuntime


class Agent:
    """Compatibility wrapper around the passive pipeline runner."""

    def __init__(
        self,
        config: AppConfig,
        llm: LlmClient,
        sessions: SessionStore,
        tools: ToolRuntime,
    ) -> None:
        memory = build_memory_engine(config, context_store=sessions.context_store)
        self._pipeline = PassivePipeline(
            config=config,
            llm=llm,
            sessions=sessions,
            tools=tools,
            memory=memory,
            event_bus=EventBus(),
        )

    async def run_turn(self, chat_id: str, user_text: str) -> str:
        return await self._pipeline.run_turn(chat_id=chat_id, user_text=user_text)
