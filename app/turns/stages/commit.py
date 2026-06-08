from __future__ import annotations

import logging

from app.consolidation import Consolidator
from app.conversation_store import ConversationStore, ConversationTurn
from app.memory_policy import MemoryPolicy, MemoryWritePlan
from app.memory_store import MemorySnapshot, MemoryStore, MemoryStoreError
from app.plugins import MemoryWriteContext, PluginHost, TurnResult
from app.turns.context import PassiveTurnCommitResult, PassiveTurnContext

logger = logging.getLogger(__name__)


class CommitTurnStage:
    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        memory_store: MemoryStore,
        memory_policy: MemoryPolicy,
        consolidator: Consolidator,
        plugin_host: PluginHost | None = None,
    ) -> None:
        self._conversation_store = conversation_store
        self._memory_store = memory_store
        self._memory_policy = memory_policy
        self._consolidator = consolidator
        self._plugin_host = plugin_host

    def run(self, context: PassiveTurnContext) -> PassiveTurnCommitResult:
        self._append_conversation_turn(
            chat_id=context.chat_id,
            user_text=context.normalized_user_text,
            assistant_text=context.reply_text,
        )
        plan = self._build_memory_write_plan(
            chat_id=context.chat_id,
            memory_snapshot=context.memory_snapshot,
            user_text=context.normalized_user_text,
            assistant_text=context.reply_text,
            turn_notes=context.turn_notes,
        )
        memory_persisted = self._persist_memory_updates(
            plan=plan,
            assistant_text=context.reply_text,
        )
        if memory_persisted:
            self._run_consolidation()
        context.memory_write_plan = plan
        context.plugin_outcomes = (
            self._plugin_host.after_turn(
                TurnResult(
                    chat_id=context.chat_id,
                    user_text=context.normalized_user_text,
                    assistant_text=context.reply_text,
                    memory_write_plan=plan,
                    turn_notes=tuple(context.turn_notes),
                )
            )
            if self._plugin_host is not None
            else []
        )
        return PassiveTurnCommitResult(
            reply_text=context.reply_text,
            memory_write_plan=plan,
            turn_notes=tuple(context.turn_notes),
            plugin_outcomes=tuple(context.plugin_outcomes),
        )

    def _append_conversation_turn(
        self,
        *,
        chat_id: int,
        user_text: str,
        assistant_text: str,
    ) -> None:
        self._conversation_store.append_turn(
            chat_id,
            ConversationTurn(
                user_text=user_text,
                assistant_text=assistant_text,
            ),
        )

    def _build_memory_write_plan(
        self,
        *,
        chat_id: int,
        memory_snapshot: MemorySnapshot | None,
        user_text: str,
        assistant_text: str,
        turn_notes: list,
    ) -> MemoryWritePlan:
        memory_candidates = (
            self._plugin_host.before_memory_write(
                MemoryWriteContext(
                    chat_id=chat_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    memory_snapshot=memory_snapshot,
                    turn_notes=tuple(turn_notes),
                )
            )
            if self._plugin_host is not None
            else []
        )
        return self._memory_policy.build_memory_write_plan(
            memory_snapshot=memory_snapshot,
            user_text=user_text,
            memory_candidates=memory_candidates,
        )

    def _persist_memory_updates(
        self,
        *,
        plan: MemoryWritePlan,
        assistant_text: str,
    ) -> bool:
        try:
            if plan.self_text is not None:
                self._memory_store.write_self(plan.self_text)
            if plan.memory_text is not None:
                self._memory_store.write_memory(plan.memory_text)
            if plan.pending_text is not None:
                self._memory_store.write_pending(plan.pending_text)
            if plan.history_entry_text is not None:
                self._memory_store.append_history(plan.history_entry_text)
                self._memory_store.append_history(f"Assistant: {assistant_text.strip()}")
            return True
        except MemoryStoreError:
            logger.exception("Failed to persist long-term memory updates; continuing with reply")
            return False

    def _run_consolidation(self) -> None:
        try:
            refreshed_snapshot = self._load_memory_snapshot()
            if refreshed_snapshot is None:
                return
            consolidation = self._consolidator.consolidate(
                history_text=refreshed_snapshot.history_text,
                previous_recent_context_text=refreshed_snapshot.recent_context_text,
                state=refreshed_snapshot.consolidation_state,
            )
            if consolidation.recent_context_text is not None:
                self._memory_store.write_recent_context(consolidation.recent_context_text)
            self._memory_store.write_consolidation_state(consolidation.state)
        except MemoryStoreError:
            logger.exception("Failed to run consolidation; continuing with reply")

    def _load_memory_snapshot(self) -> MemorySnapshot | None:
        try:
            return self._memory_store.load_snapshot()
        except MemoryStoreError:
            logger.exception("Failed to load long-term memory for consolidation; continuing without it")
            return None
