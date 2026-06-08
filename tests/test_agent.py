import threading
from collections import deque

from app.agent import AgentService
from app.conversation_store import ConversationStore
from app.llm_client import ChatMessage, LLMClientError
from app.memory_store import ConsolidationState, MemorySnapshot, MemoryStoreError
from app.self_model import format_self_model, parse_self_model
from app.tools import ToolExecutor, ToolLoop, ToolRegistry, ToolSpec


DEFAULT_SELF_SECTION = ChatMessage(
    role="system",
    content=f"[SELF.md]\n{format_self_model(parse_self_model(''))}",
)


class StubLLMClient:
    def __init__(self, replies: list[str]) -> None:
        self._replies = deque(replies)
        self.messages: list[list[ChatMessage]] = []

    def chat(self, messages: list[ChatMessage]) -> str:
        self.messages.append(list(messages))
        return self._replies.popleft()


class FailingLLMClient:
    def chat(self, messages: list[ChatMessage]) -> str:  # noqa: ARG002
        raise LLMClientError("boom")


class BlockingLLMClient:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.messages: list[list[ChatMessage]] = []
        self._lock = threading.Lock()
        self._call_count = 0

    def chat(self, messages: list[ChatMessage]) -> str:
        with self._lock:
            self._call_count += 1
            call_number = self._call_count
        self.messages.append(list(messages))
        if call_number == 1:
            self.started.set()
            self.release.wait(timeout=2)
        else:
            self.finished.set()
        return f"reply-{call_number}"


class StubMemoryStore:
    def __init__(self, snapshot: MemorySnapshot | None = None, *, error: Exception | None = None) -> None:
        self._snapshot = snapshot or MemorySnapshot(
            self_text="",
            memory_text="",
            recent_context_text="",
            pending_text="",
            history_text="",
            consolidation_state=ConsolidationState(),
        )
        self._error = error
        self.self_writes: list[str] = []
        self.memory_writes: list[str] = []
        self.recent_context_writes: list[str] = []
        self.pending_writes: list[str] = []
        self.history_appends: list[str] = []
        self.state_writes: list[ConsolidationState] = []

    def load_snapshot(self) -> MemorySnapshot:
        if self._error is not None:
            raise self._error
        return self._snapshot

    def write_self(self, text: str) -> None:
        self.self_writes.append(text)
        self._snapshot = MemorySnapshot(
            self_text=text,
            memory_text=self._snapshot.memory_text,
            recent_context_text=self._snapshot.recent_context_text,
            pending_text=self._snapshot.pending_text,
            history_text=self._snapshot.history_text,
            consolidation_state=self._snapshot.consolidation_state,
        )

    def write_memory(self, text: str) -> None:
        self.memory_writes.append(text)
        self._snapshot = MemorySnapshot(
            self_text=self._snapshot.self_text,
            memory_text=text,
            recent_context_text=self._snapshot.recent_context_text,
            pending_text=self._snapshot.pending_text,
            history_text=self._snapshot.history_text,
            consolidation_state=self._snapshot.consolidation_state,
        )

    def write_recent_context(self, text: str) -> None:
        self.recent_context_writes.append(text)
        self._snapshot = MemorySnapshot(
            self_text=self._snapshot.self_text,
            memory_text=self._snapshot.memory_text,
            recent_context_text=text,
            pending_text=self._snapshot.pending_text,
            history_text=self._snapshot.history_text,
            consolidation_state=self._snapshot.consolidation_state,
        )

    def write_pending(self, text: str) -> None:
        self.pending_writes.append(text)
        self._snapshot = MemorySnapshot(
            self_text=self._snapshot.self_text,
            memory_text=self._snapshot.memory_text,
            recent_context_text=self._snapshot.recent_context_text,
            pending_text=text,
            history_text=self._snapshot.history_text,
            consolidation_state=self._snapshot.consolidation_state,
        )

    def append_history(self, text: str) -> None:
        self.history_appends.append(text)
        existing = self._snapshot.history_text
        if existing and not existing.endswith("\n"):
            existing += "\n"
        self._snapshot = MemorySnapshot(
            self_text=self._snapshot.self_text,
            memory_text=self._snapshot.memory_text,
            recent_context_text=self._snapshot.recent_context_text,
            pending_text=self._snapshot.pending_text,
            history_text=f"{existing}{text}",
            consolidation_state=self._snapshot.consolidation_state,
        )

    def write_consolidation_state(self, state: ConsolidationState) -> None:
        self.state_writes.append(state)
        self._snapshot = MemorySnapshot(
            self_text=self._snapshot.self_text,
            memory_text=self._snapshot.memory_text,
            recent_context_text=self._snapshot.recent_context_text,
            pending_text=self._snapshot.pending_text,
            history_text=self._snapshot.history_text,
            consolidation_state=state,
        )


class SelectiveFailingMemoryStore(StubMemoryStore):
    def __init__(self, *, fail_on: str, snapshot: MemorySnapshot | None = None) -> None:
        super().__init__(snapshot)
        self._fail_on = fail_on

    def write_self(self, text: str) -> None:
        if self._fail_on == "write_self":
            raise MemoryStoreError("write_self failed")
        super().write_self(text)

    def write_memory(self, text: str) -> None:
        if self._fail_on == "write_memory":
            raise MemoryStoreError("write_memory failed")
        super().write_memory(text)

    def write_pending(self, text: str) -> None:
        if self._fail_on == "write_pending":
            raise MemoryStoreError("write_pending failed")
        super().write_pending(text)

    def append_history(self, text: str) -> None:
        if self._fail_on == "append_history":
            raise MemoryStoreError("append_history failed")
        super().append_history(text)

    def write_recent_context(self, text: str) -> None:
        if self._fail_on == "write_recent_context":
            raise MemoryStoreError("write_recent_context failed")
        super().write_recent_context(text)

    def write_consolidation_state(self, state: ConsolidationState) -> None:
        if self._fail_on == "write_consolidation_state":
            raise MemoryStoreError("write_consolidation_state failed")
        super().write_consolidation_state(state)


def build_service(
    llm_client: object,
    *,
    max_rounds: int = 3,
    memory_snapshot: MemorySnapshot | None = None,
    memory_error: Exception | None = None,
    tool_loop: ToolLoop | None = None,
) -> tuple[AgentService, ConversationStore, StubMemoryStore]:
    store = ConversationStore(max_rounds=max_rounds)
    memory_store = StubMemoryStore(memory_snapshot, error=memory_error)
    service = AgentService(
        llm_client=llm_client,  # type: ignore[arg-type]
        system_prompt="system rule",
        conversation_store=store,
        memory_store=memory_store,  # type: ignore[arg-type]
        tool_loop=tool_loop,
    )
    return service, store, memory_store


def test_agent_service_generates_reply_from_single_turn_prompt() -> None:
    llm_client = StubLLMClient(replies=["hello back"])
    service, _store, memory_store = build_service(llm_client)

    reply = service.generate_reply(chat_id=1, user_text="  hello  ")

    assert reply == "hello back"
    assert llm_client.messages == [
        [
            ChatMessage(role="system", content="system rule"),
            DEFAULT_SELF_SECTION,
            ChatMessage(role="user", content="hello"),
        ]
    ]
    assert memory_store.history_appends == ["User: hello", "Assistant: hello back"]


def test_agent_service_includes_recent_history_in_prompt() -> None:
    llm_client = StubLLMClient(replies=["first reply", "second reply"])
    service, _store, _memory_store = build_service(llm_client)

    service.generate_reply(chat_id=1, user_text="first")
    reply = service.generate_reply(chat_id=1, user_text="second")

    assert reply == "second reply"
    assert llm_client.messages[1] == [
        ChatMessage(role="system", content="system rule"),
        DEFAULT_SELF_SECTION,
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="first reply"),
        ChatMessage(role="user", content="second"),
    ]


def test_agent_service_trims_to_recent_round_limit() -> None:
    llm_client = StubLLMClient(replies=["r1", "r2", "r3"])
    service, store, _memory_store = build_service(llm_client, max_rounds=2)

    service.generate_reply(chat_id=1, user_text="u1")
    service.generate_reply(chat_id=1, user_text="u2")
    service.generate_reply(chat_id=1, user_text="u3")

    history = store.get_history(1)
    assert [(turn.user_text, turn.assistant_text) for turn in history] == [
        ("u2", "r2"),
        ("u3", "r3"),
    ]


def test_agent_service_rejects_empty_user_text() -> None:
    llm_client = StubLLMClient(replies=["unused"])
    service, _store, _memory_store = build_service(llm_client)

    try:
        service.generate_reply(chat_id=1, user_text="   ")
    except ValueError as exc:
        assert "must not be empty" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_agent_service_does_not_store_failed_llm_turn() -> None:
    service, store, memory_store = build_service(FailingLLMClient())

    try:
        service.generate_reply(chat_id=1, user_text="hello")
    except LLMClientError:
        pass
    else:
        raise AssertionError("Expected LLMClientError")

    assert store.get_history(1) == []
    assert memory_store.history_appends == []


def test_agent_service_keeps_histories_isolated_per_chat() -> None:
    llm_client = StubLLMClient(replies=["chat1", "chat2"])
    service, store, _memory_store = build_service(llm_client)

    service.generate_reply(chat_id=1, user_text="hello")
    service.generate_reply(chat_id=2, user_text="world")

    assert [(turn.user_text, turn.assistant_text) for turn in store.get_history(1)] == [
        ("hello", "chat1")
    ]
    assert [(turn.user_text, turn.assistant_text) for turn in store.get_history(2)] == [
        ("world", "chat2")
    ]


def test_agent_service_serializes_requests_for_same_chat() -> None:
    llm_client = BlockingLLMClient()
    service, store, _memory_store = build_service(llm_client)
    replies: list[str] = []

    first = threading.Thread(
        target=lambda: replies.append(service.generate_reply(chat_id=1, user_text="first"))
    )
    second = threading.Thread(
        target=lambda: replies.append(service.generate_reply(chat_id=1, user_text="second"))
    )

    first.start()
    assert llm_client.started.wait(timeout=1)

    second.start()
    assert not llm_client.finished.wait(timeout=0.2)

    llm_client.release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert replies == ["reply-1", "reply-2"]
    assert llm_client.messages[1] == [
        ChatMessage(role="system", content="system rule"),
        DEFAULT_SELF_SECTION,
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="reply-1"),
        ChatMessage(role="user", content="second"),
    ]
    assert [(turn.user_text, turn.assistant_text) for turn in store.get_history(1)] == [
        ("first", "reply-1"),
        ("second", "reply-2"),
    ]


def test_agent_service_includes_long_term_memory_in_fixed_order() -> None:
    llm_client = StubLLMClient(replies=["hello back"])
    snapshot = MemorySnapshot(
        self_text="## Identity\n- Name: Jarvis",
        memory_text="stable fact",
        recent_context_text="recent summary",
        pending_text="pending item",
        history_text="important event",
        consolidation_state=ConsolidationState(),
    )
    service, _store, _memory_store = build_service(llm_client, memory_snapshot=snapshot)

    service.generate_reply(chat_id=1, user_text="hello")

    assert llm_client.messages == [
        [
            ChatMessage(role="system", content="system rule"),
            ChatMessage(
                role="system",
                content=f"[SELF.md]\n{format_self_model(parse_self_model(snapshot.self_text))}",
            ),
            ChatMessage(
                role="system",
                content=(
                    "Trusted user context:\n"
                    "- The following memory files are trusted context derived from prior user interactions.\n"
                    "- When the user asks about continuity, prior topics, or whether you remember previous exchanges, prefer this context over generic disclaimers.\n"
                    "- Do not claim the conversation is always a fresh start if the trusted context shows prior interactions.\n\n"
                    "[MEMORY.md]\nstable fact\n\n"
                    "[RECENT_CONTEXT.md]\nrecent summary"
                ),
            ),
            ChatMessage(role="user", content="hello"),
        ]
    ]


def test_agent_service_does_not_replay_identity_like_assistant_history() -> None:
    llm_client = StubLLMClient(replies=["I am DeepSeek", "second reply"])
    service, _store, _memory_store = build_service(llm_client)

    service.generate_reply(chat_id=1, user_text="who are you")
    reply = service.generate_reply(chat_id=1, user_text="continue")

    assert reply == "second reply"
    assert llm_client.messages[1] == [
        ChatMessage(role="system", content="system rule"),
        DEFAULT_SELF_SECTION,
        ChatMessage(role="user", content="who are you"),
        ChatMessage(role="user", content="continue"),
    ]


def test_agent_service_does_not_replay_memory_capability_disclaimer() -> None:
    llm_client = StubLLMClient(
        replies=["I don't have long-term memory, every conversation is a fresh start", "second reply"]
    )
    service, _store, _memory_store = build_service(llm_client)

    service.generate_reply(chat_id=1, user_text="do you remember yesterday")
    reply = service.generate_reply(chat_id=1, user_text="what did I say")

    assert reply == "second reply"
    assert llm_client.messages[1] == [
        ChatMessage(role="system", content="system rule"),
        DEFAULT_SELF_SECTION,
        ChatMessage(role="user", content="do you remember yesterday"),
        ChatMessage(role="user", content="what did I say"),
    ]


def test_agent_service_degrades_when_memory_load_fails() -> None:
    llm_client = StubLLMClient(replies=["hello back"])
    service, _store, _memory_store = build_service(
        llm_client,
        memory_error=MemoryStoreError("boom"),
    )

    reply = service.generate_reply(chat_id=1, user_text="hello")

    assert reply == "hello back"
    assert llm_client.messages == [
        [
            ChatMessage(role="system", content="system rule"),
            ChatMessage(role="user", content="hello"),
        ]
    ]


def test_agent_service_stages_user_preference_in_pending_memory() -> None:
    llm_client = StubLLMClient(replies=["noted"])
    memory_store = StubMemoryStore(
        MemorySnapshot(
            self_text="",
            memory_text="",
            recent_context_text="",
            pending_text="- [note] existing",
            history_text="",
            consolidation_state=ConsolidationState(),
        )
    )
    store = ConversationStore(max_rounds=3)
    service = AgentService(
        llm_client=llm_client,  # type: ignore[arg-type]
        system_prompt="system rule",
        conversation_store=store,
        memory_store=memory_store,  # type: ignore[arg-type]
    )

    service.generate_reply(chat_id=1, user_text="I prefer concise answers")

    assert memory_store.memory_writes == []
    assert memory_store.pending_writes == [
        "- [note] existing\n- [preference] I prefer concise answers"
    ]
    assert memory_store.history_appends == [
        "User: I prefer concise answers",
        "Assistant: noted",
    ]


def test_agent_service_does_not_stage_plain_question_in_pending_memory() -> None:
    llm_client = StubLLMClient(replies=["answer"])
    memory_store = StubMemoryStore()
    store = ConversationStore(max_rounds=3)
    service = AgentService(
        llm_client=llm_client,  # type: ignore[arg-type]
        system_prompt="system rule",
        conversation_store=store,
        memory_store=memory_store,  # type: ignore[arg-type]
    )

    service.generate_reply(chat_id=1, user_text="What should we do next?")

    assert memory_store.memory_writes == []
    assert memory_store.pending_writes == []


def test_agent_service_writes_explicit_stable_fact_to_memory() -> None:
    llm_client = StubLLMClient(replies=["noted"])
    memory_store = StubMemoryStore(
        MemorySnapshot(
            self_text="",
            memory_text="- [note] existing fact",
            recent_context_text="",
            pending_text="",
            history_text="",
            consolidation_state=ConsolidationState(),
        )
    )
    store = ConversationStore(max_rounds=3)
    service = AgentService(
        llm_client=llm_client,  # type: ignore[arg-type]
        system_prompt="system rule",
        conversation_store=store,
        memory_store=memory_store,  # type: ignore[arg-type]
    )

    service.generate_reply(chat_id=1, user_text="Please remember that my name is Alex")

    assert memory_store.memory_writes == [
        "- [note] existing fact\n- [identity] My name is Alex"
    ]
    assert memory_store.pending_writes == []


def test_agent_service_promotes_repeated_pending_fact_to_memory_and_self() -> None:
    llm_client = StubLLMClient(replies=["noted"])
    memory_store = StubMemoryStore(
        MemorySnapshot(
            self_text="",
            memory_text="",
            recent_context_text="",
            pending_text="- [preference] I prefer concise answers",
            history_text="",
            consolidation_state=ConsolidationState(),
        )
    )
    store = ConversationStore(max_rounds=3)
    service = AgentService(
        llm_client=llm_client,  # type: ignore[arg-type]
        system_prompt="system rule",
        conversation_store=store,
        memory_store=memory_store,  # type: ignore[arg-type]
    )

    service.generate_reply(chat_id=1, user_text="I prefer concise answers")

    assert memory_store.memory_writes == ["- [preference] I prefer concise answers"]
    assert memory_store.pending_writes == [""]
    assert memory_store.self_writes
    assert "Concise answers" in memory_store.self_writes[-1]


def test_agent_service_normalizes_pending_entry_before_deduping() -> None:
    llm_client = StubLLMClient(replies=["noted"])
    memory_store = StubMemoryStore(
        MemorySnapshot(
            self_text="",
            memory_text="",
            recent_context_text="",
            pending_text="- [preference] I prefer concise answers",
            history_text="",
            consolidation_state=ConsolidationState(),
        )
    )
    store = ConversationStore(max_rounds=3)
    service = AgentService(
        llm_client=llm_client,  # type: ignore[arg-type]
        system_prompt="system rule",
        conversation_store=store,
        memory_store=memory_store,  # type: ignore[arg-type]
    )

    service.generate_reply(chat_id=1, user_text="I prefer concise answers.")

    assert memory_store.memory_writes == ["- [preference] I prefer concise answers"]
    assert memory_store.pending_writes == [""]


def test_agent_service_rebuilds_recent_context_after_three_user_messages() -> None:
    llm_client = StubLLMClient(replies=["a1", "a2", "a3"])
    service, _store, memory_store = build_service(llm_client)

    service.generate_reply(chat_id=1, user_text="u1")
    service.generate_reply(chat_id=1, user_text="u2")
    service.generate_reply(chat_id=1, user_text="u3")

    assert len(memory_store.state_writes) == 3
    assert memory_store.recent_context_writes
    recent = memory_store.recent_context_writes[-1]
    assert "## Compression" in recent
    assert "## Recent Turns" in recent
    assert "User: u3" in recent


def test_agent_service_can_complete_turn_with_tool_loop() -> None:
    llm_client = StubLLMClient(
        replies=[
            '{"type":"tool_call","tool_name":"echo","arguments":{"path":"MEMORY.md"}}',
            "tool-assisted reply",
        ]
    )
    registry = ToolRegistry(
        [
            ToolSpec(
                name="echo",
                description="echo",
                arguments=("path",),
                handler=lambda arguments: {"path": arguments["path"], "content": "memory text"},
            )
        ]
    )
    tool_loop = ToolLoop(registry=registry, executor=ToolExecutor(registry), max_tool_steps=2)
    service, store, memory_store = build_service(llm_client, tool_loop=tool_loop)

    reply = service.generate_reply(chat_id=1, user_text="check memory")

    assert reply == "tool-assisted reply"
    assert memory_store.history_appends == [
        "User: check memory",
        "Assistant: tool-assisted reply",
    ]
    assert [(turn.user_text, turn.assistant_text) for turn in store.get_history(1)] == [
        ("check memory", "tool-assisted reply")
    ]


def test_agent_service_keeps_reply_and_conversation_commit_when_memory_persistence_fails() -> None:
    llm_client = StubLLMClient(replies=["noted"])
    memory_store = SelectiveFailingMemoryStore(
        fail_on="write_memory",
        snapshot=MemorySnapshot(
            self_text="",
            memory_text="- [note] existing fact",
            recent_context_text="",
            pending_text="",
            history_text="",
            consolidation_state=ConsolidationState(),
        ),
    )
    store = ConversationStore(max_rounds=3)
    service = AgentService(
        llm_client=llm_client,  # type: ignore[arg-type]
        system_prompt="system rule",
        conversation_store=store,
        memory_store=memory_store,  # type: ignore[arg-type]
    )

    reply = service.generate_reply(chat_id=1, user_text="Please remember that my name is Alex")

    assert reply == "noted"
    assert [(turn.user_text, turn.assistant_text) for turn in store.get_history(1)] == [
        ("Please remember that my name is Alex", "noted")
    ]
    assert memory_store.history_appends == []
    assert memory_store.state_writes == []
