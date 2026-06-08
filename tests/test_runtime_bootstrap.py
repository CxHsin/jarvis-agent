from pathlib import Path

from jarvis.config import AppConfig, LlmConfig, RuntimeConfig, SearchConfig, TelegramConfig
from jarvis.pipelines.passive.runner import PassivePipeline
from jarvis.pipelines.proactive.runner import ProactivePipeline
from jarvis.runtime.app import AppRuntime
from jarvis.runtime.bootstrap import build_runtime


def test_build_runtime_returns_runtime_container_with_expected_boundaries(tmp_path: Path) -> None:
    config = AppConfig(
        telegram=TelegramConfig(bot_token="token", allowed_chat_ids=[]),
        llm=LlmConfig(api_key="key", model="demo"),
        search=SearchConfig(),
        runtime=RuntimeConfig(workspace=str(tmp_path), data_dir="data", log_dir="logs"),
    )

    container = build_runtime(config)

    assert isinstance(container.app, AppRuntime)
    assert isinstance(container.passive_pipeline, PassivePipeline)
    assert isinstance(container.proactive_pipeline, ProactivePipeline)
    assert container.app.processing is container.processing
    assert container.app.event_bus is container.event_bus
    assert container.message_bus is not None
    assert container.outbound_factory is not None
    assert container.tools is not None
    assert container.sessions is not None
