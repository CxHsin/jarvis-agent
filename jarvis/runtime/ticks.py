from __future__ import annotations

from jarvis.services.scheduler import Clock, SchedulerTick


class TickFactory:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._counter = 0

    def create(self, source: str = "scheduler") -> SchedulerTick:
        self._counter += 1
        return SchedulerTick(
            tick_id=f"{source}:{self._counter}",
            occurred_at=self._clock.now(),
            source=source,
        )
