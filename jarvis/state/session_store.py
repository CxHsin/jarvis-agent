from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


Role = Literal["user", "assistant", "tool"]


class SessionMessage(BaseModel):
    role: Role
    content: str


class SessionStore:
    def __init__(self, path: Path, history_limit: int) -> None:
        self._path = path
        self._history_limit = history_limit
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write({})

    def get_messages(self, chat_id: str) -> list[SessionMessage]:
        data = self._read()
        raw = data.get(chat_id, [])
        return [SessionMessage.model_validate(item) for item in raw]

    def append(self, chat_id: str, message: SessionMessage) -> None:
        data = self._read()
        messages = [SessionMessage.model_validate(item) for item in data.get(chat_id, [])]
        messages.append(message)
        data[chat_id] = [item.model_dump() for item in messages[-self._history_limit :]]
        self._write(data)

    def clear(self, chat_id: str) -> None:
        data = self._read()
        data.pop(chat_id, None)
        self._write(data)

    def _read(self) -> dict[str, list[dict[str, str]]]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, list[dict[str, str]]]) -> None:
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
