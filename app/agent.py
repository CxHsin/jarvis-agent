import logging

from app.consolidation import Consolidator
from app.conversation_store import ConversationStore, ConversationTurn
from app.llm_client import OpenAICompatibleClient
from app.memory_policy import MemoryPolicy
from app.memory_store import MemorySnapshot, MemoryStore, MemoryStoreError
from app.self_model import default_self_model, format_self_model, parse_self_model

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(
        self,
        *,
        llm_client: OpenAICompatibleClient,
        system_prompt: str,
        conversation_store: ConversationStore,
        memory_store: MemoryStore,
        memory_policy: MemoryPolicy | None = None,
        consolidator: Consolidator | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._system_prompt = system_prompt
        self._conversation_store = conversation_store
        self._memory_store = memory_store
        self._memory_policy = memory_policy or MemoryPolicy()
        self._consolidator = consolidator or Consolidator()

    def generate_reply(self, *, chat_id: int, user_text: str) -> str:
        normalized_text = user_text.strip()
        if not normalized_text:
            raise ValueError("user_text must not be empty")

        with self._conversation_store.lock_chat(chat_id):
            memory_snapshot = self._ensure_self_model(self._load_memory_snapshot())
            history = self._conversation_store.get_history(chat_id)
            messages = self._memory_policy.build_messages(
                system_prompt=self._system_prompt,
                memory_snapshot=memory_snapshot,
                history=history,
                user_text=normalized_text,
            )

            reply_text = self._llm_client.chat(messages)
            self._conversation_store.append_turn(
                chat_id,
                ConversationTurn(user_text=normalized_text, assistant_text=reply_text),
            )
            self._apply_memory_write_plan(
                memory_snapshot=memory_snapshot,
                user_text=normalized_text,
                assistant_text=reply_text,
            )
            return reply_text

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

    def _apply_memory_write_plan(
        self,
        *,
        memory_snapshot: MemorySnapshot | None,
        user_text: str,
        assistant_text: str,
    ) -> None:
        try:
            plan = self._memory_policy.build_memory_write_plan(
                memory_snapshot=memory_snapshot,
                user_text=user_text,
            )
            if plan.self_text is not None:
                self._memory_store.write_self(plan.self_text)
            if plan.memory_text is not None:
                self._memory_store.write_memory(plan.memory_text)
            if plan.pending_text is not None:
                self._memory_store.write_pending(plan.pending_text)
            if plan.history_entry_text is not None:
                self._memory_store.append_history(plan.history_entry_text)
                self._memory_store.append_history(f"Assistant: {assistant_text.strip()}")

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
            logger.exception("Failed to update long-term memory; continuing without it")
