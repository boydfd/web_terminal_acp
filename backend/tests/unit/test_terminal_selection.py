import json
from uuid import uuid4

import pytest

from app.services.terminal_selection import TerminalSelectionHub


@pytest.mark.asyncio
async def test_terminal_selection_hub_publishes_to_matching_client_only() -> None:
    client_id = uuid4()
    other_client_id = uuid4()
    window_id = uuid4()
    received: list[str] = []
    other_received: list[str] = []

    async def sender(message: str) -> None:
        received.append(message)

    async def other_sender(message: str) -> None:
        other_received.append(message)

    hub = TerminalSelectionHub()
    await hub.subscribe(client_id, sender)
    await hub.subscribe(other_client_id, other_sender)

    await hub.publish(client_id, window_id)

    assert [json.loads(message) for message in received] == [
        {
            "type": "terminal_selection",
            "client_id": str(client_id),
            "window_id": str(window_id),
        }
    ]
    assert other_received == []
