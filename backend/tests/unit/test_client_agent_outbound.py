import asyncio
from uuid import UUID

import pytest

from app.client_agent.outbound import (
    BulkUploadWriter,
    ControlMessageWriter,
    OutboundWriterClosed,
)
from app.services.runtime.protocol import AgentMessage, TerminalPayload, decode_agent_message


CLIENT_ID = UUID("12345678-1234-5678-1234-567812345678")
WINDOW_ID = UUID("87654321-4321-8765-4321-876543218765")
OTHER_WINDOW_ID = UUID("11111111-2222-3333-4444-555555555555")


def _terminal_message(
    data: bytes,
    request_id: str,
    *,
    window_id: UUID = WINDOW_ID,
) -> AgentMessage:
    payload = TerminalPayload.from_bytes(window_id, data).model_dump(mode="json")
    return AgentMessage(
        type="terminal_output",
        client_id=CLIENT_ID,
        window_id=window_id,
        request_id=request_id,
        payload=payload,
    )


def _ai_event(request_id: str) -> AgentMessage:
    return AgentMessage(
        type="ai_event",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        request_id=request_id,
        payload={"payload": {"id": request_id}},
    )


@pytest.mark.asyncio
async def test_control_message_writer_serializes_heartbeat_then_inventory_in_order() -> None:
    sent: list[str] = []

    async def send(data: str) -> None:
        sent.append(data)

    writer = ControlMessageWriter(send)
    writer.start()
    try:
        heartbeat = AgentMessage(type="heartbeat", client_id=CLIENT_ID, request_id="heartbeat-1")
        inventory = AgentMessage(
            type="inventory",
            client_id=CLIENT_ID,
            request_id="inventory-1",
            payload={"windows": []},
        )

        await writer.send(heartbeat)
        await writer.send(inventory)
        await writer.drain()

        assert [decode_agent_message(data) for data in sent] == [heartbeat, inventory]
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_bulk_upload_writer_prioritizes_terminal_output_with_ai_event_fairness() -> None:
    sent: list[str] = []

    async def send(data: str) -> None:
        sent.append(data)

    writer = BulkUploadWriter(send, terminal_burst=2)
    writer.start()
    try:
        terminal_1 = _terminal_message(b"one", "terminal-1")
        terminal_2 = _terminal_message(b"two", "terminal-2")
        terminal_3 = _terminal_message(b"three", "terminal-3")
        ai_event = _ai_event("ai-event-1")

        await writer.send_terminal_output(terminal_1)
        await writer.send_terminal_output(terminal_2)
        await writer.send_terminal_output(terminal_3)
        await writer.send_ai_event(ai_event)
        await writer.drain()

        assert [decode_agent_message(data) for data in sent] == [
            terminal_1,
            terminal_2,
            ai_event,
            terminal_3,
        ]
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_bulk_upload_writer_rotates_terminal_output_between_windows() -> None:
    sent: list[str] = []

    async def send(data: str) -> None:
        sent.append(data)

    writer = BulkUploadWriter(send, terminal_burst=10)
    writer.start()
    try:
        busy_1 = _terminal_message(b"busy-1", "busy-1")
        busy_2 = _terminal_message(b"busy-2", "busy-2")
        busy_3 = _terminal_message(b"busy-3", "busy-3")
        other = _terminal_message(b"other", "other", window_id=OTHER_WINDOW_ID)

        await writer.send_terminal_output(busy_1)
        await writer.send_terminal_output(busy_2)
        await writer.send_terminal_output(busy_3)
        await writer.send_terminal_output(other)
        await writer.drain()

        assert [decode_agent_message(data) for data in sent] == [
            busy_1,
            other,
            busy_2,
            busy_3,
        ]
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_bulk_upload_writer_splits_large_terminal_output_for_window_fairness() -> None:
    sent: list[str] = []

    async def send(data: str) -> None:
        sent.append(data)

    writer = BulkUploadWriter(send, terminal_chunk_bytes=4)
    writer.start()
    try:
        large = _terminal_message(b"abcdefgh", "large")
        other = _terminal_message(b"othr", "other", window_id=OTHER_WINDOW_ID)

        await writer.send_terminal_output(large)
        await writer.send_terminal_output(other)
        await writer.drain()

        sent_messages = [decode_agent_message(data) for data in sent]
        assert [message.window_id for message in sent_messages] == [
            WINDOW_ID,
            OTHER_WINDOW_ID,
            WINDOW_ID,
        ]
        assert b"".join(
            TerminalPayload.model_validate(message.payload).to_bytes()
            for message in sent_messages
            if message.window_id == WINDOW_ID
        ) == b"abcdefgh"
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_bulk_upload_writer_blocks_enqueue_when_terminal_output_queue_is_full_until_writer_drains() -> None:
    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()
    sent: list[str] = []

    async def send(data: str) -> None:
        sent.append(data)
        first_send_started.set()
        await release_first_send.wait()

    writer = BulkUploadWriter(send, terminal_output_maxsize=1)
    writer.start()
    try:
        terminal_1 = _terminal_message(b"one", "terminal-1")
        terminal_2 = _terminal_message(b"two", "terminal-2")
        terminal_3 = _terminal_message(b"three", "terminal-3")

        await writer.send_terminal_output(terminal_1)
        await writer.send_terminal_output(terminal_2)
        await asyncio.wait_for(first_send_started.wait(), timeout=1)

        blocked_enqueue = asyncio.create_task(writer.send_terminal_output(terminal_3))
        await asyncio.sleep(0)

        assert not blocked_enqueue.done()
        assert [decode_agent_message(data) for data in sent] == [terminal_1]

        release_first_send.set()
        await asyncio.wait_for(blocked_enqueue, timeout=1)
        await writer.drain()

        assert [decode_agent_message(data) for data in sent] == [
            terminal_1,
            terminal_2,
            terminal_3,
        ]
    finally:
        await writer.close()


@pytest.mark.asyncio
async def test_control_message_writer_raises_outbound_writer_closed_after_close() -> None:
    async def send(data: str) -> None:
        raise AssertionError("send should not be called after close")

    writer = ControlMessageWriter(send)
    writer.start()
    await writer.close()

    with pytest.raises(OutboundWriterClosed):
        await writer.send(AgentMessage(type="heartbeat", client_id=CLIENT_ID))
