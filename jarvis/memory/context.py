from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from jarvis.memory.models import ContextTurn, Role, SessionMessage, utc_now


class ContextStore(Protocol):
    def append_turn(
        self,
        session_key: str,
        role: Role,
        content: str,
        *,
        created_at: datetime | None = None,
    ) -> ContextTurn: ...

    def get_recent_turns(self, session_key: str, limit: int | None = None) -> list[ContextTurn]: ...

    def get_turns_since(self, session_key: str, after_sequence: int) -> list[ContextTurn]: ...

    def trim_session(self, session_key: str) -> None: ...

    def clear(self, session_key: str) -> None: ...


class JsonContextStore:
    def __init__(self, path: Path, keep_count: int) -> None:
        self._path = path
        self._keep_count = max(1, keep_count)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write({})

    def append_turn(
        self,
        session_key: str,
        role: Role,
        content: str,
        *,
        created_at: datetime | None = None,
    ) -> ContextTurn:
        data = self._read()
        turns = self._load_turns(data.get(session_key, []), session_key)
        sequence = turns[-1].sequence + 1 if turns else 1
        turn = ContextTurn(
            message_id=f"{session_key}:{sequence}",
            session_key=session_key,
            sequence=sequence,
            role=role,
            content=content,
            created_at=created_at or utc_now(),
        )
        turns.append(turn)
        data[session_key] = [item.model_dump(mode="json") for item in turns[-self._keep_count :]]
        self._write(data)
        return turn

    def get_recent_turns(self, session_key: str, limit: int | None = None) -> list[ContextTurn]:
        data = self._read()
        turns = self._load_turns(data.get(session_key, []), session_key)
        if limit is None:
            return turns
        return turns[-limit:]

    def get_turns_since(self, session_key: str, after_sequence: int) -> list[ContextTurn]:
        return [item for item in self.get_recent_turns(session_key) if item.sequence > after_sequence]

    def trim_session(self, session_key: str) -> None:
        data = self._read()
        turns = self._load_turns(data.get(session_key, []), session_key)
        data[session_key] = [item.model_dump(mode="json") for item in turns[-self._keep_count :]]
        self._write(data)

    def clear(self, session_key: str) -> None:
        data = self._read()
        data.pop(session_key, None)
        self._write(data)

    def _read(self) -> dict[str, list[dict[str, object]]]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, list[dict[str, object]]]) -> None:
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_turns(self, raw_turns: list[dict[str, object]], session_key: str) -> list[ContextTurn]:
        turns: list[ContextTurn] = []
        for index, raw in enumerate(raw_turns, start=1):
            turns.append(self._coerce_turn(raw, session_key, index))
        return turns

    def _coerce_turn(self, raw: dict[str, object], session_key: str, fallback_sequence: int) -> ContextTurn:
        if {"message_id", "sequence", "created_at"} <= raw.keys():
            return ContextTurn.model_validate(raw)
        return ContextTurn(
            message_id=str(raw.get("message_id") or f"{session_key}:{fallback_sequence}"),
            session_key=session_key,
            sequence=int(raw.get("sequence") or fallback_sequence),
            role=str(raw.get("role") or "user"),
            content=str(raw.get("content") or ""),
            created_at=utc_now(),
        )


class SessionStore:
    """Compatibility wrapper around the new ContextStore boundary."""

    def __init__(
        self,
        path: Path,
        history_limit: int,
        *,
        context_store: JsonContextStore | None = None,
    ) -> None:
        self._context_store = context_store or JsonContextStore(path, keep_count=history_limit)

    @property
    def context_store(self) -> JsonContextStore:
        return self._context_store

    def get_messages(self, chat_id: str) -> list[SessionMessage]:
        return [SessionMessage(role=item.role, content=item.content) for item in self._context_store.get_recent_turns(chat_id)]

    def append(self, chat_id: str, message: SessionMessage) -> None:
        self._context_store.append_turn(chat_id, message.role, message.content)

    def clear(self, chat_id: str) -> None:
        self._context_store.clear(chat_id)
