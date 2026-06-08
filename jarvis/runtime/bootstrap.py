from __future__ import annotations

from dataclasses import dataclass

from jarvis.config import AppConfig
from jarvis.pipelines.passive.runner import PassivePipeline
from jarvis.pipelines.proactive.runner import ProactivePipeline
from jarvis.runtime.app import AppRuntime
from jarvis.runtime.event_bus import EventBus
from jarvis.runtime.message_bus import MessageBus
from jarvis.runtime.messages import OutboundMessageFactory
from jarvis.runtime.processing import ProcessingState
from jarvis.runtime.ticks import TickFactory
from jarvis.services import Clock, LlmClient, SessionStore, build_tool_runtime
from jarvis.tools.runtime import ToolRuntime


@dataclass(frozen=True)
class RuntimeContainer:
    app: AppRuntime
    passive_pipeline: PassivePipeline
    proactive_pipeline: ProactivePipeline
    sessions: SessionStore
    tools: ToolRuntime
    llm: LlmClient
    event_bus: EventBus
    message_bus: MessageBus
    processing: ProcessingState
    outbound_factory: OutboundMessageFactory
    clock: Clock
    tick_factory: TickFactory


def build_runtime(config: AppConfig) -> RuntimeContainer:
    config.data_dir_path.mkdir(parents=True, exist_ok=True)
    config.log_dir_path.mkdir(parents=True, exist_ok=True)

    sessions = SessionStore(config.session_store_path, config.runtime.session_history_limit)
    llm = LlmClient(config.llm)
    tools = build_tool_runtime(config.workspace_path)
    event_bus = EventBus()
    message_bus = MessageBus()
    processing = ProcessingState()
    outbound_factory = OutboundMessageFactory()
    clock = Clock()
    tick_factory = TickFactory(clock)
    passive_pipeline = PassivePipeline(
        config=config,
        llm=llm,
        sessions=sessions,
        tools=tools,
        event_bus=event_bus,
    )
    proactive_pipeline = ProactivePipeline(
        processing=processing,
        event_bus=event_bus,
    )
    app = AppRuntime(
        passive_pipeline,
        proactive_pipeline,
        event_bus=event_bus,
        message_bus=message_bus,
        processing=processing,
        outbound_factory=outbound_factory,
        tick_factory=tick_factory,
    )
    return RuntimeContainer(
        app=app,
        passive_pipeline=passive_pipeline,
        proactive_pipeline=proactive_pipeline,
        sessions=sessions,
        tools=tools,
        llm=llm,
        event_bus=event_bus,
        message_bus=message_bus,
        processing=processing,
        outbound_factory=outbound_factory,
        clock=clock,
        tick_factory=tick_factory,
    )
