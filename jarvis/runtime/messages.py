from __future__ import annotations

from jarvis.runtime.message_bus import InboundMessage, OutboundMessage


class InboundMessageFactory:
    def __init__(self) -> None:
        self._counter = 0

    def create(
        self,
        *,
        session_key: str,
        text: str,
        channel: str,
        user_id: str | None = None,
    ) -> InboundMessage:
        self._counter += 1
        return InboundMessage(
            message_id=f"{channel}:{self._counter}",
            session_key=session_key,
            text=text,
            channel=channel,
            user_id=user_id,
        )


class OutboundMessageFactory:
    def __init__(self) -> None:
        self._counter = 0

    def create(
        self,
        *,
        session_key: str,
        text: str,
        channel: str,
    ) -> OutboundMessage:
        self._counter += 1
        return OutboundMessage(
            message_id=f"{channel}:out:{self._counter}",
            session_key=session_key,
            text=text,
            channel=channel,
        )
