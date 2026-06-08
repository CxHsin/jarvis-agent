import asyncio

from jarvis.runtime.app import AppRuntime
from jarvis.runtime.event_bus import EventBus
from jarvis.runtime.message_bus import InboundMessage, MessageBus
from jarvis.runtime.messages import InboundMessageFactory, OutboundMessageFactory
from jarvis.runtime.processing import ProcessingState
from jarvis.runtime.ticks import TickFactory
from jarvis.services.scheduler import Clock


class _PassiveStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def run_turn(self, chat_id: str, user_text: str) -> str:
        self.calls.append((chat_id, user_text))
        return f"reply:{user_text}"


class _ProactiveStub:
    def __init__(self) -> None:
        self.sources: list[str] = []

    async def handle_tick(self, tick) -> object:
        self.sources.append(tick.source)
        return type(
            "Outcome",
            (),
            {
                "status": "idle",
                "delivered": False,
                "reason": f"source={tick.source}",
                "tick_id": tick.tick_id,
                "occurred_at": tick.occurred_at,
            },
        )()


def test_app_runtime_routes_inbound_message_through_passive_pipeline() -> None:
    pipeline = _PassiveStub()
    proactive = _ProactiveStub()
    runtime = AppRuntime(
        pipeline,
        proactive,
        event_bus=EventBus(),
        message_bus=MessageBus(),
        processing=ProcessingState(),
        outbound_factory=OutboundMessageFactory(),
        tick_factory=TickFactory(Clock()),
    )

    reply = asyncio.run(
        runtime.process_inbound(
            InboundMessage(
                message_id="telegram:1",
                session_key="chat-1",
                text="hello",
                channel="telegram",
            )
        )
    )

    assert reply.text == "reply:hello"
    assert pipeline.calls == [("chat-1", "hello")]
    assert runtime.processing.is_busy("chat-1") is False


def test_app_runtime_emits_events_and_allows_payload_rewrite() -> None:
    pipeline = _PassiveStub()
    proactive = _ProactiveStub()
    bus = EventBus()

    async def rewrite(payload):
        payload["text"] = payload["text"].upper()
        return payload

    async def add_suffix(payload):
        payload["reply"] = f'{payload["reply"]}!'
        return payload

    bus.subscribe("runtime.inbound_message.received", rewrite)
    bus.subscribe("runtime.inbound_message.completed", add_suffix)

    runtime = AppRuntime(
        pipeline,
        proactive,
        event_bus=bus,
        message_bus=MessageBus(),
        processing=ProcessingState(),
        outbound_factory=OutboundMessageFactory(),
        tick_factory=TickFactory(Clock()),
    )

    reply = asyncio.run(
        runtime.process_inbound(
            InboundMessage(
                message_id="telegram:2",
                session_key="chat-2",
                text="hello",
                channel="telegram",
            )
        )
    )

    assert reply.text == "reply:HELLO!"
    assert pipeline.calls == [("chat-2", "HELLO")]


def test_app_runtime_routes_scheduler_tick_through_proactive_pipeline() -> None:
    pipeline = _PassiveStub()
    proactive = _ProactiveStub()
    runtime = AppRuntime(
        pipeline,
        proactive,
        event_bus=EventBus(),
        message_bus=MessageBus(),
        processing=ProcessingState(),
        outbound_factory=OutboundMessageFactory(),
        tick_factory=TickFactory(Clock()),
    )

    outcome = asyncio.run(runtime.handle_scheduler_tick(source="cron"))

    assert outcome.status == "idle"
    assert outcome.delivered is False
    assert outcome.reason == "source=cron"
    assert proactive.sources == ["cron"]


def test_app_runtime_emits_scheduler_tick_events_and_allows_payload_rewrite() -> None:
    pipeline = _PassiveStub()
    proactive = _ProactiveStub()
    bus = EventBus()

    async def rewrite(payload):
        payload["source"] = "rewritten"
        return payload

    async def override_reason(payload):
        payload["reason"] = "hooked"
        return payload

    bus.subscribe("runtime.scheduler_tick.received", rewrite)
    bus.subscribe("runtime.scheduler_tick.completed", override_reason)

    runtime = AppRuntime(
        pipeline,
        proactive,
        event_bus=bus,
        message_bus=MessageBus(),
        processing=ProcessingState(),
        outbound_factory=OutboundMessageFactory(),
        tick_factory=TickFactory(Clock()),
    )

    outcome = asyncio.run(runtime.handle_scheduler_tick(source="cron"))

    assert outcome.reason == "hooked"
    assert proactive.sources == ["rewritten"]


def test_app_runtime_accepts_standard_inbound_message_objects() -> None:
    pipeline = _PassiveStub()
    proactive = _ProactiveStub()
    runtime = AppRuntime(
        pipeline,
        proactive,
        event_bus=EventBus(),
        message_bus=MessageBus(),
        processing=ProcessingState(),
        outbound_factory=OutboundMessageFactory(),
        tick_factory=TickFactory(Clock()),
    )

    outbound = asyncio.run(
        runtime.process_inbound(
            InboundMessage(
                message_id="telegram:42",
                session_key="chat-42",
                text="hello from object",
                channel="telegram",
                user_id="user-1",
            )
        )
    )

    assert outbound.text == "reply:hello from object"
    assert pipeline.calls == [("chat-42", "hello from object")]


def test_app_runtime_publishes_standard_outbound_message_for_passive_reply() -> None:
    pipeline = _PassiveStub()
    proactive = _ProactiveStub()
    runtime = AppRuntime(
        pipeline,
        proactive,
        event_bus=EventBus(),
        message_bus=MessageBus(),
        processing=ProcessingState(),
        outbound_factory=OutboundMessageFactory(),
        tick_factory=TickFactory(Clock()),
    )

    async def scenario():
        outbound = await runtime.process_inbound(
            InboundMessage(
                message_id="telegram:7",
                session_key="chat-7",
                text="hello",
                channel="telegram",
            )
        )
        queued = await runtime.next_outbound()
        return outbound, queued

    outbound, queued = asyncio.run(scenario())

    assert outbound.text == "reply:hello"
    assert outbound.session_key == "chat-7"
    assert queued == outbound
