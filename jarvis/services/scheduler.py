from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SchedulerTick:
    tick_id: str
    occurred_at: datetime
    source: str = "scheduler"
