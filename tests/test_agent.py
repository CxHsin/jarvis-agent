from app.agent import AgentService
from app.llm_client import ChatMessage


class StubLLMClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.messages: list[ChatMessage] | None = None

    def chat(self, messages: list[ChatMessage]) -> str:
        self.messages = messages
        return self.reply


def test_agent_service_generates_reply_from_single_turn_prompt() -> None:
    llm_client = StubLLMClient(reply="hello back")
    service = AgentService(
        llm_client=llm_client,  # type: ignore[arg-type]
        system_prompt="system rule",
    )

    reply = service.generate_reply("  hello  ")

    assert reply == "hello back"
    assert llm_client.messages == [
        ChatMessage(role="system", content="system rule"),
        ChatMessage(role="user", content="hello"),
    ]


def test_agent_service_rejects_empty_user_text() -> None:
    llm_client = StubLLMClient(reply="unused")
    service = AgentService(
        llm_client=llm_client,  # type: ignore[arg-type]
        system_prompt="system rule",
    )

    try:
        service.generate_reply("   ")
    except ValueError as exc:
        assert "must not be empty" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
