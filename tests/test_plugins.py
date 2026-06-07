from pathlib import Path

import pytest

from app.conversation_store import ConversationStore
from app.memory_normalizer import MemoryEntry
from app.memory_store import ConsolidationState, MemorySnapshot
from app.plugins import (
    MemoryWriteContext,
    ModelCallContext,
    ModelCallResult,
    PluginHost,
    PluginOutcome,
    PluginSpec,
    TurnContext,
    TurnNote,
    TurnResult,
)
from app.tools import DuplicateToolError, ToolRegistry, ToolSpec

from tests.test_agent import StubLLMClient, StubMemoryStore
from app.agent import AgentService


def test_plugin_host_discovers_enabled_plugins_and_registers_tools() -> None:
    registry = ToolRegistry()
    host = PluginHost(
        registry=registry,
        enabled_plugins=("ping_tool",),
    )

    host.initialize()

    assert "ping_tool" in host.loaded_plugin_ids
    assert registry.get("ping") is not None


def test_plugin_host_applies_build_context_and_memory_hooks() -> None:
    registry = ToolRegistry()
    host = PluginHost(
        registry=registry,
        enabled_plugins=("concise_context", "note_memory"),
    )
    host.initialize()
    snapshot = MemorySnapshot(
        self_text="",
        memory_text="",
        recent_context_text="",
        pending_text="",
        history_text="",
        consolidation_state=ConsolidationState(),
    )

    context_sections = host.build_context(
        TurnContext(
            chat_id=1,
            user_text="be concise",
            history=(),
            memory_snapshot=snapshot,
            available_tools=(),
        )
    )
    memory_entries = host.before_memory_write(
        MemoryWriteContext(
            chat_id=1,
            user_text="note: buy milk",
            assistant_text="noted",
            memory_snapshot=snapshot,
            turn_notes=(),
        )
    )

    assert context_sections == [
        "Plugin hint: The user explicitly requested concise output. Keep the answer brief."
    ]
    assert memory_entries == [
        MemoryEntry(tag="note", canonical_key="note:buy milk", display_text="buy milk")
    ]


def test_plugin_host_skips_invalid_hook_result(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING")
    registry = ToolRegistry()
    host = PluginHost(registry=registry, plugins_package="tests.fixtures.invalid_plugins")

    host.initialize()
    sections = host.build_context(
        TurnContext(
            chat_id=1,
            user_text="hello",
            history=(),
            memory_snapshot=None,
            available_tools=(),
        )
    )

    assert sections == []
    assert "Plugin contribution rejected" in caplog.text


def test_plugin_host_orders_plugins_using_before_directive() -> None:
    call_order: list[str] = []

    def first_hook(context: TurnContext) -> list[str]:
        call_order.append("first")
        return ["first"]

    def second_hook(context: TurnContext) -> list[str]:
        call_order.append("second")
        return ["second"]

    first = PluginSpec(plugin_id="first", plugin_name="First", before=("second",), build_context=first_hook)
    second = PluginSpec(plugin_id="second", plugin_name="Second", build_context=second_hook)
    registry = ToolRegistry()
    host = PluginHost(registry=registry, plugins_package="app.plugins")
    host._plugins = host._order_plugins([second, first])  # type: ignore[attr-defined]

    sections = host.build_context(
        TurnContext(
            chat_id=1,
            user_text="hello",
            history=(),
            memory_snapshot=None,
            available_tools=(),
        )
    )

    assert sections == ["first", "second"]
    assert call_order == ["first", "second"]


def test_plugin_host_rejects_duplicate_tool_names() -> None:
    registry = ToolRegistry(
        [
            ToolSpec(
                name="alpha",
                description="alpha",
                arguments=("path",),
                handler=lambda arguments: arguments,
            )
        ]
    )
    host = PluginHost(registry=registry, plugins_package="tests.fixtures.duplicate_tool_plugins")

    host.initialize()

    assert registry.get("alpha") is not None
    assert any(failure.stage == "register_tools" for failure in host.load_failures)


def test_agent_service_applies_plugin_context_and_memory_candidates() -> None:
    llm_client = StubLLMClient(replies=["ok"])
    memory_store = StubMemoryStore()
    registry = ToolRegistry()
    host = PluginHost(
        registry=registry,
        enabled_plugins=("concise_context", "note_memory"),
    )
    host.initialize()
    service = AgentService(
        llm_client=llm_client,  # type: ignore[arg-type]
        system_prompt="system rule",
        conversation_store=ConversationStore(max_rounds=3),
        memory_store=memory_store,  # type: ignore[arg-type]
        plugin_host=host,
    )

    service.generate_reply(chat_id=1, user_text="note: buy milk and be concise")

    assert "Plugin hint: The user explicitly requested concise output." in llm_client.messages[0][2].content
    assert memory_store.memory_writes == ["- [note] buy milk and be concise"]

