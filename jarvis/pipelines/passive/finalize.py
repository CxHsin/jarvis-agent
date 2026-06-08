from __future__ import annotations

from jarvis.services.sessions import SessionMessage, SessionStore


class PassiveTurnFinalizer:
    def __init__(self, sessions: SessionStore) -> None:
        self._sessions = sessions

    def finalize_reply(self, chat_id: str, content: str) -> str:
        final_text = content.strip() or "I finished the turn but the model returned an empty reply."
        self._sessions.append(chat_id, SessionMessage(role="assistant", content=final_text))
        return final_text
