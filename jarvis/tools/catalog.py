from __future__ import annotations

from pathlib import Path

from jarvis.tools.executor import ToolExecutor
from jarvis.tools.filesystem import ReadFileTool, WriteFileTool
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.runtime import ToolRuntime, ToolSearchTool
from jarvis.tools.shell import ShellExecTool
from jarvis.tools.web import FetchUrlTool, WebSearchTool


def build_tool_runtime(workspace: Path) -> ToolRuntime:
    workspace = Path(workspace)
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace))
    registry.register(WriteFileTool(workspace))
    registry.register(ShellExecTool(workspace))
    registry.register(WebSearchTool())
    registry.register(FetchUrlTool())
    registry.register(ToolSearchTool(registry))
    executor = ToolExecutor(registry)
    return ToolRuntime(registry, executor)
