from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from jarvis.memory import MemoryEngine
from jarvis.runtime.event_bus import EventBus
from jarvis.runtime.processing import ProcessingState
from jarvis.services.scheduler import SchedulerTick


@dataclass(frozen=True)
class ProactiveOutcome:
    status: str
    delivered: bool
    reason: str
    tick_id: str
    occurred_at: datetime


class ProactivePipeline:
    def __init__(
        self,
        *,
        processing: ProcessingState,
        event_bus: EventBus,
        memory: MemoryEngine | None = None,
    ) -> None:
        self._processing = processing
        self._event_bus = event_bus
        self._memory = memory

    async def handle_tick(self, tick: SchedulerTick) -> ProactiveOutcome:
        payload = await self._event_bus.emit(
            "proactive.tick_received",
            {
                "tick_id": tick.tick_id,
                "occurred_at": tick.occurred_at,
                "source": tick.source,
            },
        )
        status = "idle"
        reason = "no proactive strategy configured"
        if self._memory is not None:
            optimized, reason = self._memory.optimize(payload["occurred_at"])
            status = "memory_optimized" if optimized else "idle"
        outcome = ProactiveOutcome(
            status=status,
            delivered=False,
            reason=reason,
            tick_id=str(payload["tick_id"]),
            occurred_at=payload["occurred_at"],
        )
        await self._event_bus.emit(
            "proactive.tick_completed",
            {
                "tick_id": outcome.tick_id,
                "occurred_at": outcome.occurred_at,
                "status": outcome.status,
                "delivered": outcome.delivered,
                "reason": outcome.reason,
            },
        )
        return outcome
