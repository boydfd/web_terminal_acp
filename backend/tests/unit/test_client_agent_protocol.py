import asyncio
import base64
from uuid import uuid4

import pytest

from app.services.runtime.client_connections import (
    ClientConnection,
    ClientConnectionClosed,
    ClientConnectionRegistry,
)
from app.services.runtime.protocol import (
    AgentMessage,
    TerminalPayload,
    decode_agent_message,
    encode_agent_message,
)


def test_agent_message_json_round_trip_preserves_envelope_fields():
    client_id = uuid4()
    window_id = uuid4()
    message = AgentMessage(
        type="terminal.output",
        client_id=client_id,
        window_id=window_id,
        request_id="request-123",
        payload={"ok": True, "nested": {"count": 2}},
    )

    encoded = encode_agent_message(message)
    decoded = decode_agent_message(encoded)

    assert decoded == message
    assert decoded.client_id == client_id
    assert decoded.window_id == window_id
    assert decoded.request_id == "request-123"
    assert decoded.payload == {"ok": True, "nested": {"count": 2}}


def test_terminal_payload_encodes_bytes_as_base64_and_decodes_them():
    window_id = uuid4()
    raw = b"\x00hello terminal\xff"

    payload = TerminalPayload.from_bytes(window_id, raw)

    assert payload.window_id == window_id
    assert payload.data == base64.b64encode(raw).decode("ascii")
    assert payload.to_bytes() == raw


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent_text: list[str] = []
        self.close_codes: list[int] = []

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)


class ClosingWebSocket(FakeWebSocket):
    async def send_text(self, data: str) -> None:
        raise RuntimeError('WebSocket is not connected. Need to call "accept" first.')


@pytest.mark.asyncio
async def test_client_connection_abort_fails_pending_requests():
    client_id = uuid4()
    websocket = FakeWebSocket()
    connection = ClientConnection(websocket=websocket, client_id=client_id)

    request_task = asyncio.create_task(
        connection.request(
            AgentMessage(type="terminal.input", client_id=client_id, request_id="request-1"),
            timeout=30,
        )
    )
    await asyncio.sleep(0)

    assert websocket.sent_text
    connection.abort()

    with pytest.raises(ClientConnectionClosed):
        await request_task
    assert connection.closed is True


@pytest.mark.asyncio
async def test_client_connection_send_marks_runtime_closed_as_closed():
    client_id = uuid4()
    connection = ClientConnection(websocket=ClosingWebSocket(), client_id=client_id)

    with pytest.raises(ClientConnectionClosed):
        await connection.send(AgentMessage(type="heartbeat_ack", client_id=client_id))

    assert connection.closed is True


@pytest.mark.asyncio
async def test_register_replaces_existing_connection_and_preserves_newer_unregister():
    client_id = uuid4()
    old_websocket = FakeWebSocket()
    new_websocket = FakeWebSocket()
    old_connection = ClientConnection(websocket=old_websocket, client_id=client_id)
    new_connection = ClientConnection(websocket=new_websocket, client_id=client_id)
    registry = ClientConnectionRegistry()
    await registry.register(old_connection)
    request_task = asyncio.create_task(
        old_connection.request(
            AgentMessage(type="terminal.input", client_id=client_id, request_id="request-1"),
            timeout=30,
        )
    )
    await asyncio.sleep(0)

    await registry.register(new_connection)
    await registry.unregister(old_connection)

    with pytest.raises(ClientConnectionClosed):
        await request_task
    assert registry.get(client_id) is new_connection
    assert old_connection.closed is True
    assert old_websocket.close_codes == [1000]
