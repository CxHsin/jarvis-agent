import threading
from collections import deque

from app.agent import AgentService
from app.conversation_store import ConversationStore
from app.llm_client import ChatMessage, LLMClientError


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


def build_service(llm_client: object, *, max_rounds: int = 3) -> tuple[AgentService, ConversationStore]:
    store = ConversationStore(max_rounds=max_rounds)
    service = AgentService(
        llm_client=llm_client,  # type: ignore[arg-type]
        system_prompt="system rule",
        conversation_store=store,
    )
    return service, store


def test_agent_service_generates_reply_from_single_turn_prompt() -> None:
    llm_client = StubLLMClient(replies=["hello back"])
    service, _store = build_service(llm_client)

    reply = service.generate_reply(chat_id=1, user_text="  hello  ")

    assert reply == "hello back"
    assert llm_client.messages == [
        [
            ChatMessage(role="system", content="system rule"),
            ChatMessage(role="user", content="hello"),
        ]
    ]


def test_agent_service_includes_recent_history_in_prompt() -> None:
    llm_client = StubLLMClient(replies=["first reply", "second reply"])
    service, _store = build_service(llm_client)

    service.generate_reply(chat_id=1, user_text="first")
    reply = service.generate_reply(chat_id=1, user_text="second")

    assert reply == "second reply"
    assert llm_client.messages[1] == [
        ChatMessage(role="system", content="system rule"),
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="first reply"),
        ChatMessage(role="user", content="second"),
    ]


def test_agent_service_trims_to_recent_round_limit() -> None:
    llm_client = StubLLMClient(replies=["r1", "r2", "r3"])
    service, store = build_service(llm_client, max_rounds=2)

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
    service, _store = build_service(llm_client)

    try:
        service.generate_reply(chat_id=1, user_text="   ")
    except ValueError as exc:
        assert "must not be empty" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_agent_service_does_not_store_failed_llm_turn() -> None:
    service, store = build_service(FailingLLMClient())

    try:
        service.generate_reply(chat_id=1, user_text="hello")
    except LLMClientError:
        pass
    else:
        raise AssertionError("Expected LLMClientError")

    assert store.get_history(1) == []


def test_agent_service_keeps_histories_isolated_per_chat() -> None:
    llm_client = StubLLMClient(replies=["chat1", "chat2"])
    service, store = build_service(llm_client)

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
    service, store = build_service(llm_client)
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
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="reply-1"),
        ChatMessage(role="user", content="second"),
    ]
    assert [(turn.user_text, turn.assistant_text) for turn in store.get_history(1)] == [
        ("first", "reply-1"),
        ("second", "reply-2"),
    ]
