from __future__ import annotations

from typing import Any

from jarvis.services.sessions import SessionMessage, SessionStore


SYSTEM_PROMPT = """You are Jarvis, a pragmatic personal assistant.
Reply concisely and accurately.
Use tools when needed for local commands, file access, or web access.
When a tool returns an error, explain it plainly instead of hiding it."""


class PassiveContextAssembler:
    def __init__(self, sessions: SessionStore) -> None:
        self._sessions = sessions

    def start_turn(self, chat_id: str, user_text: str) -> list[dict[str, Any]]:
        self._sessions.append(chat_id, SessionMessage(role="user", content=user_text))
        return self.build_messages(chat_id)

    def build_messages(self, chat_id: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for item in self._sessions.get_messages(chat_id):
            messages.append({"role": item.role, "content": item.content})
        return messages
