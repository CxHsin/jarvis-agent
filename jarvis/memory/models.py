from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


Role = Literal["user", "assistant", "tool"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SessionMessage(BaseModel):
    role: Role
    content: str


class ContextTurn(BaseModel):
    message_id: str
    session_key: str
    sequence: int
    role: Role
    content: str
    created_at: datetime = Field(default_factory=utc_now)


class HistoryEntry(BaseModel):
    source_ref: str
    message_ids: list[str] = Field(default_factory=list)
    text: str
    created_at: datetime = Field(default_factory=utc_now)


class PendingMemoryItem(BaseModel):
    item_id: str
    source_ref: str
    tag: str
    text: str
    created_at: datetime = Field(default_factory=utc_now)


class VectorMemoryRecord(BaseModel):
    record_id: str
    source_ref: str
    session_key: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class MemoryRecallHit(BaseModel):
    text: str
    source_ref: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievedMemory(BaseModel):
    self_text: str = ""
    memory_text: str = ""
    recent_context_text: str = ""
    vector_hits: list[MemoryRecallHit] = Field(default_factory=list)
    recent_turns: list[ContextTurn] = Field(default_factory=list)


class TurnCommitted(BaseModel):
    session_key: str
    user_turn: ContextTurn
    assistant_turn: ContextTurn
