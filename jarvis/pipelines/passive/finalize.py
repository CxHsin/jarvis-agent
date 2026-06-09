from __future__ import annotations

from jarvis.memory.context import ContextStore
from jarvis.memory.models import ContextTurn


class PassiveTurnFinalizer:
    def __init__(self, context_store: ContextStore | object) -> None:
        self._context_store = getattr(context_store, "context_store", context_store)

    def normalize_reply(self, content: str) -> str:
        return content.strip() or "I finished the turn but the model returned an empty reply."

    def finalize_reply(self, chat_id: str, content: str) -> str:
        final_text = self.normalize_reply(content)
        self.commit_reply(chat_id, final_text)
        return final_text

    def commit_reply(self, chat_id: str, content: str) -> ContextTurn | None:
        try:
            return self._context_store.append_turn(chat_id, "assistant", self.normalize_reply(content))
        except Exception:
            return None
