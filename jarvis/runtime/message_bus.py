from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    session_key: str
    text: str
    channel: str
    user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class OutboundMessage:
    message_id: str
    session_key: str
    text: str
    channel: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MessageBus:
    def __init__(self) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, message: InboundMessage) -> None:
        await self._inbound.put(message)

    async def next_inbound(self) -> InboundMessage:
        return await self._inbound.get()

    async def publish_outbound(self, message: OutboundMessage) -> None:
        await self._outbound.put(message)

    async def next_outbound(self) -> OutboundMessage:
        return await self._outbound.get()
