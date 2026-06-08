from __future__ import annotations

import logging

from app.conversation_store import ConversationStore
from app.memory_store import MemorySnapshot, MemoryStore, MemoryStoreError
from app.plugins import PluginHost
from app.plugins.types import TurnContext
from app.self_model import default_self_model, format_self_model, parse_self_model
from app.turns.context import PassiveTurnContext

logger = logging.getLogger(__name__)


class LoadTurnContextStage:
    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        memory_store: MemoryStore,
        plugin_host: PluginHost | None = None,
    ) -> None:
        self._conversation_store = conversation_store
        self._memory_store = memory_store
        self._plugin_host = plugin_host

    def run(self, context: PassiveTurnContext) -> PassiveTurnContext:
        memory_snapshot = self._ensure_self_model(self._load_memory_snapshot())
        history = self._conversation_store.get_history(context.chat_id)
        available_tools = (
            self._plugin_host.available_tools if self._plugin_host is not None else tuple()
        )
        plugin_turn_context = TurnContext(
            chat_id=context.chat_id,
            user_text=context.normalized_user_text,
            history=tuple(history),
            memory_snapshot=memory_snapshot,
            available_tools=available_tools,
        )
        extra_context_sections = (
            self._plugin_host.build_context(plugin_turn_context) if self._plugin_host is not None else []
        )
        context.memory_snapshot = memory_snapshot
        context.history = list(history)
        context.available_tools = available_tools
        context.extra_context_sections = extra_context_sections
        return context

    def _load_memory_snapshot(self) -> MemorySnapshot | None:
        try:
            return self._memory_store.load_snapshot()
        except MemoryStoreError:
            logger.exception("Failed to load long-term memory; continuing without it")
            return None

    def _ensure_self_model(self, snapshot: MemorySnapshot | None) -> MemorySnapshot | None:
        if snapshot is None:
            return None

        try:
            model = parse_self_model(snapshot.self_text)
        except Exception:
            logger.exception("Failed to parse SELF.md; falling back to default self model")
            model = default_self_model()
        self_text = format_self_model(model)
        if snapshot.self_text != self_text:
            try:
                self._memory_store.write_self(self_text)
            except MemoryStoreError:
                logger.exception("Failed to persist SELF.md; continuing with in-memory default")
        return MemorySnapshot(
            self_text=self_text,
            memory_text=snapshot.memory_text,
            recent_context_text=snapshot.recent_context_text,
            pending_text=snapshot.pending_text,
            history_text=snapshot.history_text,
            consolidation_state=snapshot.consolidation_state,
        )
