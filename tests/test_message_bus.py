import asyncio

from jarvis.runtime.message_bus import InboundMessage, MessageBus, OutboundMessage


def test_message_bus_round_trips_inbound_and_outbound_messages() -> None:
    bus = MessageBus()
    inbound = InboundMessage(
        message_id="in-1",
        session_key="chat-1",
        text="hello",
        channel="telegram",
    )
    outbound = OutboundMessage(
        message_id="out-1",
        session_key="chat-1",
        text="reply",
        channel="telegram",
    )

    async def scenario():
        await bus.publish_inbound(inbound)
        await bus.publish_outbound(outbound)
        received_inbound = await bus.next_inbound()
        received_outbound = await bus.next_outbound()
        return received_inbound, received_outbound

    received_inbound, received_outbound = asyncio.run(scenario())

    assert received_inbound == inbound
    assert received_outbound == outbound
