from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from app.conversation_store import ConversationTurn
from app.llm_client import ChatMessage
from app.memory_normalizer import MemoryEntry
from app.memory_policy import MemoryWritePlan
from app.memory_store import MemorySnapshot
from app.tools.base import ToolSpec


@dataclass(frozen=True)
class TurnNote:
    source: str
    text: str


@dataclass(frozen=True)
class PluginOutcome:
    summary: str | None = None


@dataclass(frozen=True)
class TurnContext:
    chat_id: int
    user_text: str
    history: tuple[ConversationTurn, ...]
    memory_snapshot: MemorySnapshot | None
    available_tools: tuple[str, ...]


@dataclass(frozen=True)
class ModelCallContext:
    chat_id: int
    user_text: str
    messages: tuple[ChatMessage, ...]
    available_tools: tuple[str, ...]
    memory_snapshot: MemorySnapshot | None


@dataclass(frozen=True)
class ModelCallResult:
    chat_id: int
    user_text: str
    reply_text: str
    memory_snapshot: MemorySnapshot | None


@dataclass(frozen=True)
class MemoryWriteContext:
    chat_id: int
    user_text: str
    assistant_text: str
    memory_snapshot: MemorySnapshot | None
    turn_notes: tuple[TurnNote, ...]


@dataclass(frozen=True)
class TurnResult:
    chat_id: int
    user_text: str
    assistant_text: str
    memory_write_plan: MemoryWritePlan
    turn_notes: tuple[TurnNote, ...]


@dataclass(frozen=True)
class ProactiveContext:
    chat_id: int
    now: datetime
    last_user_message_at: datetime | None
    last_proactive_send_at: datetime | None
    memory_snapshot: MemorySnapshot | None
    available_tools: tuple[str, ...]
    enabled_plugin_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProactiveCandidate:
    candidate_id: str
    plugin_id: str
    kind: str
    summary: str
    priority: int = 0
    not_before: datetime | None = None
    dedupe_key: str | None = None
    suggested_message: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class DriftContext:
    now: datetime
    last_user_message_at: datetime | None
    last_proactive_send_at: datetime | None
    memory_snapshot: MemorySnapshot | None
    available_tools: tuple[str, ...]
    enabled_plugin_ids: tuple[str, ...]


@dataclass(frozen=True)
class DriftOutcome:
    summary: str
    reason: str = "task_executed"


ExecuteDriftTask = Callable[[DriftContext], DriftOutcome]


@dataclass(frozen=True)
class DriftTask:
    task_id: str
    plugin_id: str
    kind: str
    summary: str
    execute: ExecuteDriftTask
    priority: int = 0
    not_before: datetime | None = None
    dedupe_key: str | None = None
    estimated_cost: int = 1
    requires_tools: bool = False


RegisterToolsHook = Callable[[], list[ToolSpec]]
BuildContextHook = Callable[[TurnContext], list[str]]
BeforeModelCallHook = Callable[[ModelCallContext], list[str]]
AfterModelCallHook = Callable[[ModelCallResult], list[TurnNote]]
BeforeMemoryWriteHook = Callable[[MemoryWriteContext], list[MemoryEntry]]
AfterTurnHook = Callable[[TurnResult], PluginOutcome | None]
CollectProactiveCandidatesHook = Callable[[ProactiveContext], list[ProactiveCandidate]]
CollectDriftTasksHook = Callable[[DriftContext], list[DriftTask]]


@dataclass(frozen=True)
class PluginSpec:
    plugin_id: str
    plugin_name: str
    enabled_by_default: bool = False
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    register_tools: RegisterToolsHook | None = None
    build_context: BuildContextHook | None = None
    before_model_call: BeforeModelCallHook | None = None
    after_model_call: AfterModelCallHook | None = None
    before_memory_write: BeforeMemoryWriteHook | None = None
    after_turn: AfterTurnHook | None = None
    collect_proactive_candidates: CollectProactiveCandidatesHook | None = None
    collect_drift_tasks: CollectDriftTasksHook | None = None
    config: dict[str, object] = field(default_factory=dict)
