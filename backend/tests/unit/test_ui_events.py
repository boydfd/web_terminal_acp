import asyncio
import json
from uuid import uuid4

import pytest

from app.services.ui_events import UiEventHub


@pytest.mark.asyncio
async def test_ui_event_hub_publishes_invalidation_to_subscribers():
    hub = UiEventHub()
    messages: list[str] = []

    async def sender(message: str) -> None:
        messages.append(message)

    client_id = uuid4()
    window_id = uuid4()
    await hub.subscribe(sender)
    await hub.publish_invalidation(
        ["tree", "window", "tree"],
        client_id=client_id,
        window_id=window_id,
        reason="window_updated",
    )

    assert len(messages) == 1
    payload = json.loads(messages[0])
    assert payload["type"] == "invalidate"
    assert payload["seq"] == 1
    assert payload["resources"] == ["tree", "window"]
    assert payload["client_id"] == str(client_id)
    assert payload["window_id"] == str(window_id)
    assert payload["reason"] == "window_updated"


@pytest.mark.asyncio
async def test_ui_event_hub_debounces_invalidations_by_key():
    hub = UiEventHub()
    messages: list[str] = []

    async def sender(message: str) -> None:
        messages.append(message)

    client_id = uuid4()
    await hub.subscribe(sender)
    await hub.publish_debounced_invalidation(
        ("terminal_output", client_id),
        ["window"],
        client_id=client_id,
        reason="terminal_output",
        delay_seconds=0.01,
    )
    await hub.publish_debounced_invalidation(
        ("terminal_output", client_id),
        ["tree", "search"],
        client_id=client_id,
        reason="terminal_output",
        delay_seconds=0.01,
    )
    await asyncio.sleep(0.05)

    assert len(messages) == 1
    payload = json.loads(messages[0])
    assert payload["type"] == "invalidate"
    assert set(payload["resources"]) == {"window", "tree", "search"}
    assert payload["client_id"] == str(client_id)
