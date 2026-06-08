from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from jarvis.tools.base import Tool, ToolCallResult, ToolMeta

_MAX_STDIO_CHARS = 8000


class ShellExecTool(Tool):
    name = "shell_exec"
    description = "Execute a local shell command and return stdout, stderr, and exit code."
    meta = ToolMeta(
        risk_level="external-side-effect",
        always_on=False,
        default_timeout_seconds=60,
        search_hint="run shell command terminal powershell bash execute command",
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to execute."},
            "timeout_seconds": {
                "type": "integer",
                "description": "Execution timeout in seconds.",
                "default": 60,
            },
        },
        "required": ["command"],
    }

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    async def execute(self, arguments: dict[str, Any]) -> ToolCallResult:
        command = str(arguments["command"])
        timeout = int(arguments.get("timeout_seconds", 60))
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self._workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            payload = {
                "command": command,
                "exit_code": proc.returncode,
                "stdout": _truncate_text(stdout.decode(errors="replace")),
                "stderr": _truncate_text(stderr.decode(errors="replace")),
            }
            return ToolCallResult(
                tool_name=self.name,
                status="success" if proc.returncode == 0 else "error",
                content=json.dumps(payload, ensure_ascii=False),
                structured=payload,
                risk_level=self.meta.risk_level,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            payload = {"command": command, "error": f"Timed out after {timeout}s"}
            return ToolCallResult(
                tool_name=self.name,
                status="timeout",
                content=json.dumps(
                    payload,
                    ensure_ascii=False,
                ),
                structured=payload,
                risk_level=self.meta.risk_level,
            )


def _truncate_text(text: str) -> str:
    if len(text) <= _MAX_STDIO_CHARS:
        return text
    return text[: _MAX_STDIO_CHARS - 32] + "\n...[truncated]..."
