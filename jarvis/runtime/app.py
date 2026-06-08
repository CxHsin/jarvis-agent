from __future__ import annotations

from jarvis.pipelines.passive.runner import PassivePipeline
from jarvis.pipelines.proactive.runner import ProactiveOutcome, ProactivePipeline
from jarvis.runtime.event_bus import EventBus
from jarvis.runtime.message_bus import InboundMessage, MessageBus, OutboundMessage
from jarvis.runtime.messages import OutboundMessageFactory
from jarvis.runtime.processing import ProcessingState
from jarvis.runtime.ticks import TickFactory


class AppRuntime:
    def __init__(
        self,
        passive_pipeline: PassivePipeline,
        proactive_pipeline: ProactivePipeline,
        *,
        event_bus: EventBus,
        message_bus: MessageBus,
        processing: ProcessingState,
        outbound_factory: OutboundMessageFactory,
        tick_factory: TickFactory,
    ) -> None:
        self._passive_pipeline = passive_pipeline
        self._proactive_pipeline = proactive_pipeline
        self._event_bus = event_bus
        self._message_bus = message_bus
        self._processing = processing
        self._outbound_factory = outbound_factory
        self._tick_factory = tick_factory

    @property
    def processing(self) -> ProcessingState:
        return self._processing

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    async def process_inbound(self, message: InboundMessage) -> OutboundMessage:
        await self._message_bus.publish_inbound(message)
        queued = await self._message_bus.next_inbound()
        payload = await self._event_bus.emit(
            "runtime.inbound_message.received",
            {
                "message_id": queued.message_id,
                "chat_id": queued.session_key,
                "text": queued.text,
                "channel": queued.channel,
                "user_id": queued.user_id,
            },
        )
        self._processing.enter(queued.session_key)
        try:
            reply = await self._passive_pipeline.run_turn(
                chat_id=payload["chat_id"],
                user_text=payload["text"],
            )
        finally:
            self._processing.exit(queued.session_key)
        payload["reply"] = reply
        payload = await self._event_bus.emit("runtime.inbound_message.completed", payload)
        outbound = self._outbound_factory.create(
            session_key=str(payload["chat_id"]),
            text=str(payload["reply"]),
            channel=str(payload["channel"]),
        )
        await self._message_bus.publish_outbound(outbound)
        return outbound

    async def handle_scheduler_tick(self, source: str = "scheduler") -> ProactiveOutcome:
        tick = self._tick_factory.create(source=source)
        payload = await self._event_bus.emit(
            "runtime.scheduler_tick.received",
            {
                "tick_id": tick.tick_id,
                "occurred_at": tick.occurred_at,
                "source": tick.source,
            },
        )
        outcome = await self._proactive_pipeline.handle_tick(
            tick=type(tick)(
                tick_id=str(payload["tick_id"]),
                occurred_at=payload["occurred_at"],
                source=str(payload["source"]),
            )
        )
        payload = await self._event_bus.emit(
            "runtime.scheduler_tick.completed",
            {
                "tick_id": outcome.tick_id,
                "occurred_at": outcome.occurred_at,
                "source": payload["source"],
                "status": outcome.status,
                "delivered": outcome.delivered,
                "reason": outcome.reason,
            },
        )
        return ProactiveOutcome(
            status=str(payload["status"]),
            delivered=bool(payload["delivered"]),
            reason=str(payload["reason"]),
            tick_id=str(payload["tick_id"]),
            occurred_at=payload["occurred_at"],
        )

    async def next_outbound(self) -> OutboundMessage:
        return await self._message_bus.next_outbound()
