from app.tools import ToolCall, ToolExecutor, ToolRegistry, ToolSpec


def _reader(arguments: dict[str, str]) -> dict[str, str]:
    return {"echo": arguments["path"]}


def _boom(arguments: dict[str, str]) -> dict[str, str]:  # noqa: ARG001
    raise RuntimeError("boom")


def test_tool_executor_executes_valid_tool_call() -> None:
    registry = ToolRegistry(
        [
            ToolSpec(
                name="read",
                description="read path",
                arguments=("path",),
                handler=_reader,
            )
        ]
    )
    executor = ToolExecutor(registry)

    result = executor.execute(ToolCall(tool_name="read", arguments={"path": "app/main.py"}))

    assert result.status == "ok"
    assert result.tool_name == "read"
    assert result.content == {"echo": "app/main.py"}
    assert result.error_code is None


def test_tool_executor_rejects_unknown_tool() -> None:
    executor = ToolExecutor(ToolRegistry())

    result = executor.execute(ToolCall(tool_name="missing", arguments={"path": "x"}))

    assert result.status == "error"
    assert result.error_code == "unknown_tool"


def test_tool_executor_rejects_invalid_arguments() -> None:
    registry = ToolRegistry(
        [
            ToolSpec(
                name="read",
                description="read path",
                arguments=("path",),
                handler=_reader,
            )
        ]
    )
    executor = ToolExecutor(registry)

    result = executor.execute(ToolCall(tool_name="read", arguments={"name": "x"}))

    assert result.status == "error"
    assert result.error_code == "invalid_arguments"


def test_tool_executor_converts_runtime_exception_to_structured_error() -> None:
    registry = ToolRegistry(
        [
            ToolSpec(
                name="boom",
                description="boom",
                arguments=("path",),
                handler=_boom,
            )
        ]
    )
    executor = ToolExecutor(registry)

    result = executor.execute(ToolCall(tool_name="boom", arguments={"path": "x"}))

    assert result.status == "error"
    assert result.error_code == "execution_failed"
