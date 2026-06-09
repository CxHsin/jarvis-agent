from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jarvis.memory import MemoryEngine, PromptContextAssembler
from jarvis.memory.context import SessionStore
from jarvis.memory.models import ContextTurn, SessionMessage


SYSTEM_PROMPT = """You are Jarvis, a pragmatic personal assistant.
Reply concisely and accurately.
Use tools when needed for local commands, file access, or web access.
When a tool returns an error, explain it plainly instead of hiding it."""


@dataclass(frozen=True)
class PreparedPassiveTurn:
    user_turn: ContextTurn | None
    messages: list[dict[str, Any]]


class PassiveContextAssembler:
    def __init__(self, memory: MemoryEngine | SessionStore) -> None:
        self._memory = memory if isinstance(memory, MemoryEngine) else None
        self._sessions = memory if isinstance(memory, SessionStore) else None
        self._prompt_context = (
            PromptContextAssembler(memory=self._memory, system_prompt=SYSTEM_PROMPT) if self._memory is not None else None
        )

    def start_turn(self, chat_id: str, user_text: str) -> list[dict[str, Any]]:
        return self.begin_turn(chat_id, user_text).messages

    def begin_turn(self, chat_id: str, user_text: str) -> PreparedPassiveTurn:
        if self._sessions is not None:
            self._sessions.append(chat_id, SessionMessage(role="user", content=user_text))
            return PreparedPassiveTurn(
                user_turn=None,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}]
                + [{"role": item.role, "content": item.content} for item in self._sessions.get_messages(chat_id)],
            )
        user_turn: ContextTurn | None = None
        try:
            user_turn = self._memory.append_turn(chat_id, "user", user_text)
            messages = self._prompt_context.build_messages(chat_id, user_text)
        except Exception:
            # Context failures should not block the core reply path.
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_text}]
        return PreparedPassiveTurn(user_turn=user_turn, messages=messages)
