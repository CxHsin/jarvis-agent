import logging

from app.consolidation import Consolidator
from app.conversation_store import ConversationStore, ConversationTurn
from app.llm_client import ChatMessage, OpenAICompatibleClient
from app.memory_policy import MemoryPolicy, MemoryWritePlan
from app.memory_store import MemorySnapshot, MemoryStore, MemoryStoreError
from app.plugins import (
    MemoryWriteContext,
    ModelCallContext,
    ModelCallResult,
    PluginHost,
    TurnContext,
    TurnResult,
)
from app.self_model import default_self_model, format_self_model, parse_self_model
from app.tools.loop import ToolLoop

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
        tool_loop: ToolLoop | None = None,
        plugin_host: PluginHost | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._system_prompt = system_prompt
        self._conversation_store = conversation_store
        self._memory_store = memory_store
        self._memory_policy = memory_policy or MemoryPolicy()
        self._consolidator = consolidator or Consolidator()
        self._tool_loop = tool_loop
        self._plugin_host = plugin_host

    def generate_reply(self, *, chat_id: int, user_text: str) -> str:
        normalized_text = user_text.strip()
        if not normalized_text:
            raise ValueError("user_text must not be empty")

        with self._conversation_store.lock_chat(chat_id):
            memory_snapshot = self._ensure_self_model(self._load_memory_snapshot())
            history = self._conversation_store.get_history(chat_id)
            turn_context = TurnContext(
                chat_id=chat_id,
                user_text=normalized_text,
                history=tuple(history),
                memory_snapshot=memory_snapshot,
                available_tools=(
                    self._plugin_host.available_tools
                    if self._plugin_host is not None
                    else tuple()
                ),
            )
            extra_context_sections = (
                self._plugin_host.build_context(turn_context) if self._plugin_host is not None else []
            )
            messages = self._memory_policy.build_messages(
                system_prompt=self._system_prompt,
                memory_snapshot=memory_snapshot,
                history=history,
                user_text=normalized_text,
                extra_system_sections=extra_context_sections,
            )
            if self._plugin_host is not None:
                before_call_sections = self._plugin_host.before_model_call(
                    ModelCallContext(
                        chat_id=chat_id,
                        user_text=normalized_text,
                        messages=tuple(messages),
                        available_tools=self._plugin_host.available_tools,
                        memory_snapshot=memory_snapshot,
                    )
                )
                messages.extend(
                    ChatMessage(role="system", content=section) for section in before_call_sections
                )

            if self._tool_loop is None:
                reply_text = self._llm_client.chat(messages)
            else:
                reply_text = self._tool_loop.run(
                    llm_client=self._llm_client,
                    messages=messages,
                )
            turn_notes = (
                self._plugin_host.after_model_call(
                    ModelCallResult(
                        chat_id=chat_id,
                        user_text=normalized_text,
                        reply_text=reply_text,
                        memory_snapshot=memory_snapshot,
                    )
                )
                if self._plugin_host is not None
                else []
            )
            self._conversation_store.append_turn(
                chat_id,
                ConversationTurn(user_text=normalized_text, assistant_text=reply_text),
            )
            plan = self._apply_memory_write_plan(
                chat_id=chat_id,
                memory_snapshot=memory_snapshot,
                user_text=normalized_text,
                assistant_text=reply_text,
                turn_notes=turn_notes,
            )
            if self._plugin_host is not None:
                self._plugin_host.after_turn(
                    TurnResult(
                        chat_id=chat_id,
                        user_text=normalized_text,
                        assistant_text=reply_text,
                        memory_write_plan=plan,
                        turn_notes=tuple(turn_notes),
                    )
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
        chat_id: int,
        memory_snapshot: MemorySnapshot | None,
        user_text: str,
        assistant_text: str,
        turn_notes: list,
    ) -> MemoryWritePlan:
        try:
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
            plan = self._memory_policy.build_memory_write_plan(
                memory_snapshot=memory_snapshot,
                user_text=user_text,
                memory_candidates=memory_candidates,
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
                return plan
            consolidation = self._consolidator.consolidate(
                history_text=refreshed_snapshot.history_text,
                previous_recent_context_text=refreshed_snapshot.recent_context_text,
                state=refreshed_snapshot.consolidation_state,
            )
            if consolidation.recent_context_text is not None:
                self._memory_store.write_recent_context(consolidation.recent_context_text)
            self._memory_store.write_consolidation_state(consolidation.state)
            return plan
        except MemoryStoreError:
            logger.exception("Failed to update long-term memory; continuing without it")
            return MemoryWritePlan(
                self_text=None,
                memory_text=None,
                pending_text=None,
                history_entry_text=None,
            )
