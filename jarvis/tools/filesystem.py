from __future__ import annotations

from pathlib import Path
from typing import Any

from jarvis.tools.base import Tool, ToolCallResult, ToolMeta


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a UTF-8 text file from the local workspace."
    meta = ToolMeta(
        risk_level="read-only",
        always_on=True,
        default_timeout_seconds=10,
        search_hint="read file view file open text file",
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to a text file."},
        },
        "required": ["path"],
    }

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    async def execute(self, arguments: dict[str, Any]) -> ToolCallResult:
        path = self._workspace / str(arguments["path"])
        text = path.read_text(encoding="utf-8")
        return ToolCallResult(
            tool_name=self.name,
            status="success",
            content=text,
            structured={"path": str(path), "text": text},
            risk_level=self.meta.risk_level,
        )


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write UTF-8 text content to a file in the local workspace."
    meta = ToolMeta(
        risk_level="write",
        always_on=False,
        default_timeout_seconds=10,
        search_hint="write file save text create file edit file",
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Target file path."},
            "content": {"type": "string", "description": "Text content to write."},
        },
        "required": ["path", "content"],
    }

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    async def execute(self, arguments: dict[str, Any]) -> ToolCallResult:
        path = self._workspace / str(arguments["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(arguments["content"]), encoding="utf-8")
        return ToolCallResult(
            tool_name=self.name,
            status="success",
            content=f"Wrote {path}",
            structured={"path": str(path)},
            risk_level=self.meta.risk_level,
        )
