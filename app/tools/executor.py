from app.tools.base import ToolCall, ToolResult
from app.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def execute(self, tool_call: ToolCall) -> ToolResult:
        spec = self._registry.get(tool_call.tool_name)
        if spec is None:
            return ToolResult(
                status="error",
                tool_name=tool_call.tool_name,
                content="Requested tool is not available.",
                error_code="unknown_tool",
            )

        validation_error = _validate_arguments(
            arguments=tool_call.arguments,
            required=spec.arguments,
        )
        if validation_error is not None:
            return ToolResult(
                status="error",
                tool_name=tool_call.tool_name,
                content=validation_error,
                error_code="invalid_arguments",
            )

        try:
            content = spec.handler(_coerce_arguments(tool_call.arguments))
        except Exception:
            return ToolResult(
                status="error",
                tool_name=tool_call.tool_name,
                content="Tool execution failed.",
                error_code="execution_failed",
            )

        return ToolResult(status="ok", tool_name=tool_call.tool_name, content=content)


def _validate_arguments(*, arguments: dict[str, object], required: tuple[str, ...]) -> str | None:
    missing = [name for name in required if name not in arguments]
    if missing:
        return f"Missing required arguments: {', '.join(missing)}"

    extras = sorted(set(arguments) - set(required))
    if extras:
        return f"Unexpected arguments: {', '.join(extras)}"

    for name in required:
        if not isinstance(arguments[name], str) or not arguments[name].strip():
            return f"Argument '{name}' must be a non-empty string."
    return None


def _coerce_arguments(arguments: dict[str, object]) -> dict[str, str]:
    return {name: value.strip() for name, value in arguments.items() if isinstance(value, str)}
