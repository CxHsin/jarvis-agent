import json
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolCall:
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    status: str
    tool_name: str
    content: Any
    error_code: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    arguments: tuple[str, ...]
    handler: Callable[[dict[str, str]], Any]


def parse_tool_call(text: str) -> ToolCall | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "tool_call":
        return None

    tool_name = payload.get("tool_name")
    arguments = payload.get("arguments")
    if not isinstance(tool_name, str) or not tool_name.strip():
        return None
    if not isinstance(arguments, dict):
        return None

    return ToolCall(tool_name=tool_name.strip(), arguments=arguments)
