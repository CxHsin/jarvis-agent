from __future__ import annotations

from collections import defaultdict
from typing import Any, Awaitable, Callable


EventHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]
ObserverHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._observers: dict[str, list[ObserverHandler]] = defaultdict(list)

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        self._handlers[event_name].append(handler)

    def observe(self, event_name: str, handler: ObserverHandler) -> None:
        self._observers[event_name].append(handler)

    async def emit(self, event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = payload
        for handler in self._handlers.get(event_name, []):
            result = handler(current)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[assignment]
            if result is not None:
                current = result
        for observer in self._observers.get(event_name, []):
            result = observer(current)
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]
        return current
