import logging

from app.conversation_store import ConversationStore, ConversationTurn
from app.llm_client import ChatMessage, OpenAICompatibleClient
from app.memory_store import MemorySnapshot, MemoryStore, MemoryStoreError

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(
        self,
        *,
        llm_client: OpenAICompatibleClient,
        system_prompt: str,
        conversation_store: ConversationStore,
        memory_store: MemoryStore,
    ) -> None:
        self._llm_client = llm_client
        self._system_prompt = system_prompt
        self._conversation_store = conversation_store
        self._memory_store = memory_store

    def generate_reply(self, *, chat_id: int, user_text: str) -> str:
        normalized_text = user_text.strip()
        if not normalized_text:
            raise ValueError("user_text must not be empty")

        with self._conversation_store.lock_chat(chat_id):
            history = self._conversation_store.get_history(chat_id)
            messages = [ChatMessage(role="system", content=self._system_prompt)]
            memory_snapshot = self._load_memory_snapshot()
            memory_block = _format_memory_block(memory_snapshot)
            if memory_block is not None:
                messages.append(ChatMessage(role="system", content=memory_block))
            for turn in history:
                messages.append(ChatMessage(role="user", content=turn.user_text))
                messages.append(ChatMessage(role="assistant", content=turn.assistant_text))
            messages.append(ChatMessage(role="user", content=normalized_text))

            reply_text = self._llm_client.chat(messages)
            self._conversation_store.append_turn(
                chat_id,
                ConversationTurn(user_text=normalized_text, assistant_text=reply_text),
            )
            return reply_text

    def _load_memory_snapshot(self) -> MemorySnapshot | None:
        try:
            return self._memory_store.load_snapshot()
        except MemoryStoreError:
            logger.exception("Failed to load long-term memory; continuing without it")
            return None


def _format_memory_block(snapshot: MemorySnapshot | None) -> str | None:
    if snapshot is None or not snapshot.has_content():
        return None

    return "\n\n".join(
        [
            "Long-term memory:",
            _format_memory_section("MEMORY.md", snapshot.memory_text),
            _format_memory_section("RECENT_CONTEXT.md", snapshot.recent_context_text),
            _format_memory_section("PENDING.md", snapshot.pending_text),
            _format_memory_section("HISTORY.md", snapshot.history_text),
        ]
    )


def _format_memory_section(title: str, content: str) -> str:
    body = content.strip()
    if not body:
        body = "(empty)"
    return f"[{title}]\n{body}"
