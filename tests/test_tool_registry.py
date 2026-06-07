import pytest

from app.tools import DuplicateToolError, ToolRegistry, ToolSpec


def _noop(arguments: dict[str, str]) -> dict[str, str]:
    return arguments


def test_tool_registry_registers_and_resolves_specs() -> None:
    alpha = ToolSpec(
        name="alpha",
        description="alpha tool",
        arguments=("path",),
        handler=_noop,
    )
    beta = ToolSpec(
        name="beta",
        description="beta tool",
        arguments=("name",),
        handler=_noop,
    )
    registry = ToolRegistry([beta, alpha])

    assert registry.get("alpha") == alpha
    assert registry.get("beta") == beta
    assert registry.get("missing") is None
    assert [spec.name for spec in registry.list_specs()] == ["alpha", "beta"]


def test_tool_registry_rejects_duplicate_names() -> None:
    alpha = ToolSpec(
        name="alpha",
        description="alpha tool",
        arguments=("path",),
        handler=_noop,
    )
    registry = ToolRegistry([alpha])

    with pytest.raises(DuplicateToolError):
        registry.register(alpha)
