from __future__ import annotations

import asyncio
from pathlib import Path

from jarvis.config import AppConfig, LlmConfig, RuntimeConfig, SearchConfig, TelegramConfig
from jarvis.core.agent import Agent
from jarvis.services import SessionStore, build_tool_runtime


class _FakeLlm:
    def __init__(self) -> None:
        self._calls = 0

    async def complete(self, messages, tools):
        self._calls += 1
        if self._calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_search",
                        "name": "tool_search",
                        "arguments": {"query": "select:write_file"},
                    }
                ],
            }
        if self._calls == 2:
            visible_names = {item["function"]["name"] for item in tools}
            assert "write_file" in visible_names
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_write",
                        "name": "write_file",
                        "arguments": {"path": "notes.txt", "content": "hello from tool"},
                    }
                ],
            }
        return {"content": "done", "tool_calls": []}


class _LoopingFakeLlm:
    def __init__(self) -> None:
        self._calls = 0

    async def complete(self, messages, tools):
        self._calls += 1
        if self._calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_fetch_1",
                        "name": "fetch_url",
                        "arguments": {"url": "https://example.com/article"},
                    }
                ],
            }
        if self._calls == 2:
            assert tools
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_fetch_2",
                        "name": "fetch_url",
                        "arguments": {"url": "https://example.com/article-2"},
                    }
                ],
            }
        assert tools == []
        assert messages[-1]["role"] == "system"
        assert "Stop calling tools" in messages[-1]["content"]
        return {"content": "final summary from gathered tool results", "tool_calls": []}


class _RepeatedToolLoopFakeLlm:
    def __init__(self) -> None:
        self._calls = 0

    async def complete(self, messages, tools):
        self._calls += 1
        if self._calls <= 3:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_fetch_{self._calls}",
                        "name": "fetch_url",
                        "arguments": {"url": "https://example.com/repeat"},
                    }
                ],
            }
        assert tools == []
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        assert tool_messages[-1]["content"] == "Skipped because the same tool call pattern repeated too many times."
        return {"content": "forced summary after repeated tool loop", "tool_calls": []}


class _ChangingArgsFakeLlm:
    def __init__(self) -> None:
        self._calls = 0

    async def complete(self, messages, tools):
        self._calls += 1
        if self._calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_fetch_1",
                        "name": "fetch_url",
                        "arguments": {"url": "https://example.com/a"},
                    }
                ],
            }
        if self._calls == 2:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_fetch_2",
                        "name": "fetch_url",
                        "arguments": {"url": "https://example.com/b"},
                    }
                ],
            }
        return {"content": "done without false positive", "tool_calls": []}


def test_agent_tool_round_trip_with_unlock(tmp_path: Path) -> None:
    config = AppConfig(
        telegram=TelegramConfig(bot_token="token", allowed_chat_ids=[]),
        llm=LlmConfig(api_key="key", model="demo"),
        search=SearchConfig(),
        runtime=RuntimeConfig(workspace=str(tmp_path), data_dir="data", log_dir="logs"),
    )
    store = SessionStore(tmp_path / "data" / "sessions.json", history_limit=12)
    tools = build_tool_runtime(tmp_path)
    agent = Agent(config=config, llm=_FakeLlm(), sessions=store, tools=tools)

    reply = asyncio.run(agent.run_turn("chat-1", "write a note"))

    assert reply == "done"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "hello from tool"
    saved = store.get_messages("chat-1")
    assert [item.content for item in saved] == ["write a note", "done"]


def test_agent_forces_final_answer_when_tool_round_limit_is_hit(tmp_path: Path) -> None:
    config = AppConfig(
        telegram=TelegramConfig(bot_token="token", allowed_chat_ids=[]),
        llm=LlmConfig(api_key="key", model="demo"),
        search=SearchConfig(),
        runtime=RuntimeConfig(
            workspace=str(tmp_path),
            data_dir="data",
            log_dir="logs",
            max_tool_round_trips=1,
        ),
    )
    store = SessionStore(tmp_path / "data" / "sessions.json", history_limit=12)
    tools = build_tool_runtime(tmp_path)
    agent = Agent(config=config, llm=_LoopingFakeLlm(), sessions=store, tools=tools)

    reply = asyncio.run(agent.run_turn("chat-2", "summarize this page"))

    assert reply == "final summary from gathered tool results"


def test_agent_breaks_repeated_tool_loop_and_forces_summary(tmp_path: Path) -> None:
    config = AppConfig(
        telegram=TelegramConfig(bot_token="token", allowed_chat_ids=[]),
        llm=LlmConfig(api_key="key", model="demo"),
        search=SearchConfig(),
        runtime=RuntimeConfig(workspace=str(tmp_path), data_dir="data", log_dir="logs"),
    )
    store = SessionStore(tmp_path / "data" / "sessions.json", history_limit=12)
    tools = build_tool_runtime(tmp_path)
    agent = Agent(config=config, llm=_RepeatedToolLoopFakeLlm(), sessions=store, tools=tools)

    reply = asyncio.run(agent.run_turn("chat-3", "summarize this page"))

    assert reply == "forced summary after repeated tool loop"


def test_agent_does_not_flag_repeated_tool_when_arguments_change(tmp_path: Path) -> None:
    config = AppConfig(
        telegram=TelegramConfig(bot_token="token", allowed_chat_ids=[]),
        llm=LlmConfig(api_key="key", model="demo"),
        search=SearchConfig(),
        runtime=RuntimeConfig(workspace=str(tmp_path), data_dir="data", log_dir="logs"),
    )
    store = SessionStore(tmp_path / "data" / "sessions.json", history_limit=12)
    tools = build_tool_runtime(tmp_path)
    agent = Agent(config=config, llm=_ChangingArgsFakeLlm(), sessions=store, tools=tools)

    reply = asyncio.run(agent.run_turn("chat-4", "summarize this page"))

    assert reply == "done without false positive"
