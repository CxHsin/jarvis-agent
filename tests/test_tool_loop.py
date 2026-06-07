import json

from app.llm_client import ChatMessage
from app.tools import ToolExecutor, ToolLoop, ToolRegistry, ToolSpec


class StubLLMClient:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.messages: list[list[ChatMessage]] = []

    def chat(self, messages: list[ChatMessage]) -> str:
        self.messages.append(list(messages))
        return self._replies.pop(0)


def _registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec(
                name="echo",
                description="echo path",
                arguments=("path",),
                handler=lambda arguments: {"path": arguments["path"], "content": "ok"},
            )
        ]
    )


def test_tool_loop_bypasses_tool_execution_when_model_returns_text() -> None:
    llm_client = StubLLMClient(["final answer"])
    loop = ToolLoop(registry=_registry(), executor=ToolExecutor(_registry()))

    reply = loop.run(
        llm_client=llm_client,
        messages=[ChatMessage(role="user", content="hello")],
    )

    assert reply == "final answer"
    assert llm_client.messages[0][-1].role == "system"
    assert "Tool protocol" in llm_client.messages[0][-1].content


def test_tool_loop_executes_tool_and_reenters_model() -> None:
    llm_client = StubLLMClient(
        [
            '{"type":"tool_call","tool_name":"echo","arguments":{"path":"app/main.py"}}',
            "final answer",
        ]
    )
    registry = _registry()
    loop = ToolLoop(registry=registry, executor=ToolExecutor(registry))

    reply = loop.run(
        llm_client=llm_client,
        messages=[ChatMessage(role="user", content="inspect file")],
    )

    assert reply == "final answer"
    result_message = llm_client.messages[1][-1]
    assert result_message.role == "system"
    payload = json.loads(result_message.content)
    assert payload["type"] == "tool_result"
    assert payload["tool_name"] == "echo"
    assert payload["status"] == "ok"


def test_tool_loop_stops_after_tool_limit() -> None:
    llm_client = StubLLMClient(
        [
            '{"type":"tool_call","tool_name":"echo","arguments":{"path":"a"}}',
            '{"type":"tool_call","tool_name":"echo","arguments":{"path":"b"}}',
            '{"type":"tool_call","tool_name":"echo","arguments":{"path":"c"}}',
            "final after limit",
        ]
    )
    registry = _registry()
    loop = ToolLoop(registry=registry, executor=ToolExecutor(registry), max_tool_steps=2)

    reply = loop.run(
        llm_client=llm_client,
        messages=[ChatMessage(role="user", content="loop")],
    )

    assert reply == "final after limit"
    payload = json.loads(llm_client.messages[3][-1].content)
    assert payload["error_code"] == "tool_limit_reached"


def test_tool_loop_surfaces_unknown_tool_as_recoverable_error() -> None:
    llm_client = StubLLMClient(
        [
            '{"type":"tool_call","tool_name":"missing","arguments":{"path":"a"}}',
            "fallback answer",
        ]
    )
    registry = _registry()
    loop = ToolLoop(registry=registry, executor=ToolExecutor(registry))

    reply = loop.run(
        llm_client=llm_client,
        messages=[ChatMessage(role="user", content="loop")],
    )

    assert reply == "fallback answer"
    payload = json.loads(llm_client.messages[1][-1].content)
    assert payload["error_code"] == "unknown_tool"
