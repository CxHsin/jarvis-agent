import asyncio
import json

from jarvis.tools.base import Tool, ToolCallResult, ToolMeta
from jarvis.tools.catalog import build_tool_runtime
from jarvis.tools.executor import ToolExecutor
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.runtime import ToolRuntime, ToolSearchTool


class _DemoTool(Tool):
    name = "demo"
    description = "demo tool"
    parameters = {"type": "object", "properties": {}}
    meta = ToolMeta(risk_level="read-only", always_on=True, default_timeout_seconds=5)

    async def execute(self, arguments):
        return ToolCallResult(
            tool_name=self.name,
            status="success",
            content="ok",
            structured={"ok": True},
            risk_level=self.meta.risk_level,
        )


def test_tool_runtime_executes_visible_tool() -> None:
    registry = ToolRegistry()
    registry.register(_DemoTool())
    runtime = ToolRuntime(registry, ToolExecutor(registry))
    result = asyncio.run(runtime.execute("demo", {}, chat_id="chat-1"))
    assert result.success is True
    assert result.content == "ok"


def test_hidden_tool_is_rejected_until_unlocked() -> None:
    runtime = build_tool_runtime(".")
    result = asyncio.run(
        runtime.execute(
            "write_file",
            {"path": "note.txt", "content": "hello"},
            chat_id="chat-1",
        )
    )
    assert result.status == "denied"
    assert "tool_search" in result.content


def test_tool_search_select_returns_selected_names() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSearchTool(registry),
    )
    registry.register(
        _WriteLikeTool(),
    )
    runtime = ToolRuntime(registry, ToolExecutor(registry))
    result = asyncio.run(
        runtime.execute("tool_search", {"query": "select:write_file"}, chat_id="chat-1")
    )
    data = result.structured or {}
    assert result.success is True
    assert data["selected_names"] == ["write_file"]


class _WriteLikeTool(Tool):
    name = "write_file"
    description = "Write a file"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    meta = ToolMeta(
        risk_level="write",
        always_on=False,
        default_timeout_seconds=5,
        search_hint="write save file",
    )

    async def execute(self, arguments):
        return ToolCallResult(
            tool_name=self.name,
            status="success",
            content=json.dumps({"path": arguments["path"]}),
            structured={"path": arguments["path"]},
            risk_level=self.meta.risk_level,
        )
