from __future__ import annotations


class ProcessingState:
    """Session-scoped passive processing counter."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def enter(self, session_key: str) -> None:
        self._counts[session_key] = self._counts.get(session_key, 0) + 1

    def exit(self, session_key: str) -> None:
        self._counts[session_key] = max(0, self._counts.get(session_key, 0) - 1)

    def is_busy(self, session_key: str) -> bool:
        return self._counts.get(session_key, 0) > 0
