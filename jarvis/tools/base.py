from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolMeta:
    risk_level: str = "read-only"
    always_on: bool = False
    default_timeout_seconds: int = 30
    search_hint: str | None = None
    requires_confirmation: bool = False


@dataclass(frozen=True)
class ToolCallRequest:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    chat_id: str
    channel: str = "telegram"
    visible_tools: tuple[str, ...] = ()
    unlocked_tools: tuple[str, ...] = ()


@dataclass
class ToolCallResult:
    tool_name: str
    status: str
    content: str
    structured: dict[str, Any] | None = None
    duration_ms: int = 0
    risk_level: str = "read-only"
    artifact_refs: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.status == "success"


class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    meta = ToolMeta()

    async def execute(self, arguments: dict[str, Any]) -> ToolCallResult:
        raise NotImplementedError

    def to_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate_params(self, arguments: dict[str, Any]) -> list[str]:
        schema = self.parameters or {}
        if schema.get("type", "object") != "object":
            return ["Tool schema top level type must be object."]
        return _validate_schema(arguments, schema, "")


def error_result(
    tool_name: str,
    message: str,
    *,
    status: str = "error",
    risk_level: str = "read-only",
    structured: dict[str, Any] | None = None,
    duration_ms: int = 0,
) -> ToolCallResult:
    return ToolCallResult(
        tool_name=tool_name,
        status=status,
        content=message,
        structured=structured,
        duration_ms=duration_ms,
        risk_level=risk_level,
    )


async def run_with_timeout(awaitable, timeout_seconds: int):
    return await asyncio.wait_for(awaitable, timeout=timeout_seconds)


def _validate_schema(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    label = path or "arguments"
    schema_type = schema.get("type")
    errors: list[str] = []

    if schema_type == "object":
        if not isinstance(value, dict):
            return [f"{label} must be an object."]
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        for key in required:
            if key not in value:
                errors.append(f"Missing required field: {path + '.' + key if path else key}")
        for key, item in value.items():
            prop_schema = properties.get(key)
            if prop_schema:
                errors.extend(
                    _validate_schema(item, prop_schema, f"{path}.{key}" if path else key)
                )
        return errors

    if schema_type == "array":
        if not isinstance(value, list):
            return [f"{label} must be an array."]
        item_schema = schema.get("items")
        if not item_schema:
            return []
        for index, item in enumerate(value):
            errors.extend(_validate_schema(item, item_schema, f"{label}[{index}]"))
        return errors

    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
    }
    expected = type_map.get(schema_type)
    if expected and not isinstance(value, expected):
        return [f"{label} must be of type {schema_type}."]
    return errors
