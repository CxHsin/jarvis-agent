import asyncio
from datetime import datetime, timezone
from pathlib import Path

from jarvis.config import AppConfig, LlmConfig, RuntimeConfig, SearchConfig, TelegramConfig
from jarvis.memory.embeddings import OpenAICompatibleEmbeddingProvider
from jarvis.memory import TurnCommitted, build_memory_engine
from jarvis.pipelines.passive.context import PassiveContextAssembler
from jarvis.pipelines.proactive.runner import ProactivePipeline
from jarvis.runtime.event_bus import EventBus
from jarvis.runtime.processing import ProcessingState
from jarvis.runtime.ticks import TickFactory
from jarvis.services.scheduler import Clock


class _FakeEmbeddingProvider:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            if not text.strip():
                vectors.append([])
                continue
            lowered = text.lower()
            vectors.append(
                [
                    1.0 if any(token in lowered for token in ("rust", "language", "like")) else 0.0,
                    1.0 if any(token in lowered for token in ("tea", "prefer")) else 0.0,
                    1.0 if "compiler" in lowered else 0.0,
                ]
            )
        return vectors


def _config(tmp_path: Path, **runtime_overrides: object) -> AppConfig:
    runtime = RuntimeConfig(workspace=str(tmp_path), data_dir="data", log_dir="logs", **runtime_overrides)
    return AppConfig(
        telegram=TelegramConfig(bot_token="token", allowed_chat_ids=[]),
        llm=LlmConfig(api_key="key", model="demo"),
        search=SearchConfig(),
        runtime=runtime,
    )


def _seed_conversation(memory) -> None:
    turns = [
        ("I like Rust", "ack 1"),
        ("remember that I prefer tea", "ack 2"),
        ("I started learning compilers", "ack 3"),
    ]
    for user_text, assistant_text in turns:
        user_turn = memory.append_turn("chat-1", "user", user_text)
        assistant_turn = memory.append_turn("chat-1", "assistant", assistant_text)
        memory.on_turn_committed(
            TurnCommitted(
                session_key="chat-1",
                user_turn=user_turn,
                assistant_turn=assistant_turn,
            )
        )


def test_memory_prompt_assembly_injects_memory_layers_before_raw_recent_turns(tmp_path: Path) -> None:
    config = _config(tmp_path)
    memory = build_memory_engine(config, embedding_provider=_FakeEmbeddingProvider())
    _seed_conversation(memory)
    assembler = PassiveContextAssembler(memory)

    messages = assembler.begin_turn("chat-1", "what language did I like?").messages

    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith("You are Jarvis")
    assert messages[1]["content"].startswith("# Self Model")
    assert messages[2]["content"].startswith("# Memory")
    assert messages[3]["content"].startswith("# Recent Context")
    assert messages[4]["content"].startswith("# Semantic Recall")
    assert messages[5] == {"role": "user", "content": "I like Rust"}
    assert messages[-1] == {"role": "user", "content": "what language did I like?"}


def test_consolidation_writes_markdown_outputs_and_is_noop_without_new_messages(tmp_path: Path) -> None:
    config = _config(tmp_path)
    memory = build_memory_engine(config, embedding_provider=_FakeEmbeddingProvider())
    _seed_conversation(memory)

    history_path = config.memory_dir_path / "HISTORY.md"
    pending_path = config.memory_dir_path / "PENDING.md"
    recent_context_path = config.memory_dir_path / "RECENT_CONTEXT.md"
    history_before = history_path.read_text(encoding="utf-8")

    assert '<!-- consolidation:["chat-1:1"]:history_entry -->' in history_before
    assert "[requested_memory] remember that I prefer tea" in pending_path.read_text(encoding="utf-8")
    assert "## Recent Turns" in recent_context_path.read_text(encoding="utf-8")
    assert memory.consolidate("chat-1") is False
    assert history_path.read_text(encoding="utf-8") == history_before


def test_optimizer_archives_pending_items_into_prompt_visible_memory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    memory = build_memory_engine(config, embedding_provider=_FakeEmbeddingProvider())
    memory.remember("chat-1", "remember that I prefer tea")

    optimized, reason = memory.optimize(datetime(2026, 6, 10, tzinfo=timezone.utc))

    assert optimized is True
    assert "optimized 1 pending" in reason
    assert "remember that I prefer tea" in (config.memory_dir_path / "MEMORY.md").read_text(encoding="utf-8")
    assert (config.memory_dir_path / "PENDING.md").read_text(encoding="utf-8") == "# Pending Memory\n\n"


def test_proactive_pipeline_runs_memory_optimizer_on_scheduler_tick(tmp_path: Path) -> None:
    config = _config(tmp_path)
    memory = build_memory_engine(config, embedding_provider=_FakeEmbeddingProvider())
    memory.remember("chat-1", "remember that I prefer tea")
    pipeline = ProactivePipeline(
        processing=ProcessingState(),
        event_bus=EventBus(),
        memory=memory,
    )

    outcome = asyncio.run(pipeline.handle_tick(TickFactory(Clock()).create(source="cron")))

    assert outcome.status == "memory_optimized"
    assert "optimized 1 pending" in outcome.reason


def test_openai_compatible_embedding_provider_uses_embedding_model_from_config() -> None:
    captured: dict[str, object] = {}

    class _EmbeddingsApi:
        def create(self, *, model, input):
            captured["model"] = model
            captured["input"] = input
            return type(
                "Response",
                (),
                {
                    "data": [
                        type("Item", (), {"embedding": [0.1, 0.2]})(),
                        type("Item", (), {"embedding": [0.3, 0.4]})(),
                    ]
                },
            )()

    client = type("Client", (), {"embeddings": _EmbeddingsApi()})()
    provider = OpenAICompatibleEmbeddingProvider(
        LlmConfig(api_key="key", model="chat-model", embedding_model="embed-model"),
        client=client,
    )

    vectors = provider.embed_texts(["alpha", "beta"])

    assert captured == {"model": "embed-model", "input": ["alpha", "beta"]}
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
