from __future__ import annotations

from dataclasses import dataclass, field

from app.conversation_store import ConversationTurn
from app.memory_normalizer import MemoryEntry
from app.memory_policy import MemoryWritePlan
from app.memory_store import MemorySnapshot
from app.plugins.types import PluginOutcome, TurnNote
from app.llm_client import ChatMessage


@dataclass
class PassiveTurnContext:
    chat_id: int
    user_text: str
    normalized_user_text: str
    history: list[ConversationTurn] = field(default_factory=list)
    memory_snapshot: MemorySnapshot | None = None
    available_tools: tuple[str, ...] = ()
    extra_context_sections: list[str] = field(default_factory=list)
    prompt_messages: list[ChatMessage] = field(default_factory=list)
    reply_text: str = ""
    turn_notes: list[TurnNote] = field(default_factory=list)
    memory_candidates: list[MemoryEntry] = field(default_factory=list)
    memory_write_plan: MemoryWritePlan | None = None
    plugin_outcomes: list[PluginOutcome] = field(default_factory=list)


@dataclass(frozen=True)
class PassiveTurnCommitResult:
    reply_text: str
    memory_write_plan: MemoryWritePlan
    turn_notes: tuple[TurnNote, ...]
    plugin_outcomes: tuple[PluginOutcome, ...]
