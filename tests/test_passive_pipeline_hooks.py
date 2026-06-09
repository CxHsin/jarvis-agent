import asyncio
from pathlib import Path

from jarvis.config import AppConfig, LlmConfig, RuntimeConfig, SearchConfig, TelegramConfig
from jarvis.pipelines.passive.runner import PassivePipeline
from jarvis.runtime.event_bus import EventBus
from jarvis.services import SessionStore, build_tool_runtime


class _ImmediateReplyLlm:
    async def complete(self, messages, tools):
        return {"content": "base reply", "tool_calls": []}


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(bot_token="token", allowed_chat_ids=[]),
        llm=LlmConfig(api_key="key", model="demo"),
        search=SearchConfig(),
        runtime=RuntimeConfig(workspace=str(tmp_path), data_dir="data", log_dir="logs", memory_enabled=False),
    )


def test_passive_pipeline_emits_lifecycle_events_in_order(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sessions = SessionStore(tmp_path / "data" / "sessions.json", history_limit=12)
    tools = build_tool_runtime(tmp_path)
    bus = EventBus()
    seen: list[str] = []

    async def on_context(payload):
        seen.append(f'context:{payload["chat_id"]}')
        return payload

    async def on_tool_loop(payload):
        seen.append(f'tool_loop:{payload["tool_rounds"]}')
        return payload

    async def on_reply(payload):
        seen.append(f'reply:{payload["reply"]}')
        return payload

    bus.subscribe("passive.context_built", on_context)
    bus.subscribe("passive.tool_loop_completed", on_tool_loop)
    bus.subscribe("passive.reply_finalized", on_reply)

    pipeline = PassivePipeline(
        config=config,
        llm=_ImmediateReplyLlm(),
        sessions=sessions,
        tools=tools,
        event_bus=bus,
    )

    reply = asyncio.run(pipeline.run_turn("chat-1", "hello"))

    assert reply == "base reply"
    assert seen == ["context:chat-1", "tool_loop:0", "reply:base reply"]


def test_passive_pipeline_hooks_can_rewrite_context_and_reply(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sessions = SessionStore(tmp_path / "data" / "sessions.json", history_limit=12)
    tools = build_tool_runtime(tmp_path)
    bus = EventBus()

    async def change_context(payload):
        payload["messages"][-1]["content"] = "rewritten"
        return payload

    async def change_reply(payload):
        payload["reply"] = f'{payload["reply"]}!'
        return payload

    class _EchoUserLlm:
        async def complete(self, messages, tools):
            return {"content": messages[-1]["content"], "tool_calls": []}

    bus.subscribe("passive.context_built", change_context)
    bus.subscribe("passive.reply_finalized", change_reply)

    pipeline = PassivePipeline(
        config=config,
        llm=_EchoUserLlm(),
        sessions=sessions,
        tools=tools,
        event_bus=bus,
    )

    reply = asyncio.run(pipeline.run_turn("chat-2", "hello"))

    assert reply == "rewritten!"
