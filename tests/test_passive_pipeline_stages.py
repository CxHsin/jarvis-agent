import asyncio
from pathlib import Path

from jarvis.config import AppConfig, LlmConfig, RuntimeConfig, SearchConfig, TelegramConfig
from jarvis.pipelines.passive import PassiveContextAssembler, PassiveToolLoop, PassiveTurnFinalizer
from jarvis.services import SessionStore, build_tool_runtime


class _ImmediateReplyLlm:
    async def complete(self, messages, tools):
        return {"content": "hello back", "tool_calls": []}


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        telegram=TelegramConfig(bot_token="token", allowed_chat_ids=[]),
        llm=LlmConfig(api_key="key", model="demo"),
        search=SearchConfig(),
        runtime=RuntimeConfig(workspace=str(tmp_path), data_dir="data", log_dir="logs", memory_enabled=False),
    )


def test_context_assembler_appends_user_message_and_builds_prompt(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "data" / "sessions.json", history_limit=12)
    assembler = PassiveContextAssembler(store)

    messages = assembler.start_turn("chat-1", "hello")

    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "hello"}


def test_tool_loop_returns_immediate_reply_without_tool_rounds(tmp_path: Path) -> None:
    config = _config(tmp_path)
    tools = build_tool_runtime(tmp_path)
    loop = PassiveToolLoop(config.runtime, _ImmediateReplyLlm(), tools)
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}]

    outcome = asyncio.run(loop.run("chat-1", messages))

    assert outcome.assistant_message["content"] == "hello back"
    assert outcome.tool_rounds == 0


def test_finalizer_persists_assistant_reply_and_normalizes_empty_content(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "data" / "sessions.json", history_limit=12)
    finalizer = PassiveTurnFinalizer(store)

    reply = finalizer.finalize_reply("chat-1", " ")

    assert reply == "I finished the turn but the model returned an empty reply."
    saved = store.get_messages("chat-1")
    assert [item.content for item in saved] == ["I finished the turn but the model returned an empty reply."]
