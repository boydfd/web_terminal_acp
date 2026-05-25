import asyncio
from uuid import UUID

import pytest

from app.routers.client_agent import _WindowFairMessageQueue, _enqueue_background_message
from app.services.runtime.protocol import AgentMessage


CLIENT_ID = UUID("12345678-1234-5678-1234-567812345678")
WINDOW_ID = UUID("87654321-4321-8765-4321-876543218765")
OTHER_WINDOW_ID = UUID("11111111-2222-3333-4444-555555555555")


@pytest.mark.asyncio
async def test_terminal_output_enqueue_waits_when_queue_is_full_without_dropping() -> None:
    queue: asyncio.Queue[AgentMessage] = asyncio.Queue(maxsize=1)
    oldest = AgentMessage(type="terminal_output", client_id=CLIENT_ID, window_id=WINDOW_ID)
    newest = AgentMessage(
        type="terminal_output",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        payload={"data": "newest"},
    )
    queue.put_nowait(oldest)

    enqueue_task = asyncio.create_task(
        _enqueue_background_message(
            queue,
            client_id=CLIENT_ID,
            message=newest,
            queue_name="terminal_output",
        )
    )
    await asyncio.sleep(0)

    assert not enqueue_task.done()
    assert queue.get_nowait() is oldest
    queue.task_done()
    await asyncio.wait_for(enqueue_task, timeout=0.1)
    assert queue.get_nowait() is newest
    queue.task_done()


@pytest.mark.asyncio
async def test_window_fair_message_queue_rotates_between_terminal_windows() -> None:
    queue = _WindowFairMessageQueue(maxsize=10)
    busy_1 = AgentMessage(type="terminal_output", client_id=CLIENT_ID, window_id=WINDOW_ID)
    busy_2 = AgentMessage(type="terminal_output", client_id=CLIENT_ID, window_id=WINDOW_ID)
    busy_3 = AgentMessage(type="terminal_output", client_id=CLIENT_ID, window_id=WINDOW_ID)
    other = AgentMessage(type="terminal_output", client_id=CLIENT_ID, window_id=OTHER_WINDOW_ID)

    await queue.put(busy_1)
    await queue.put(busy_2)
    await queue.put(busy_3)
    await queue.put(other)

    assert await queue.get() is busy_1
    queue.task_done()
    assert await queue.get() is other
    queue.task_done()
    assert await queue.get() is busy_2
    queue.task_done()
    assert await queue.get() is busy_3
    queue.task_done()


@pytest.mark.asyncio
async def test_ai_event_enqueue_waits_when_queue_is_full_without_dropping() -> None:
    queue: asyncio.Queue[AgentMessage] = asyncio.Queue(maxsize=1)
    oldest = AgentMessage(type="ai_event", client_id=CLIENT_ID, window_id=WINDOW_ID)
    newest = AgentMessage(
        type="ai_event",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        payload={"payload": {"id": "newest"}},
    )
    queue.put_nowait(oldest)

    enqueue_task = asyncio.create_task(
        _enqueue_background_message(
            queue,
            client_id=CLIENT_ID,
            message=newest,
            queue_name="ai_event",
        )
    )
    await asyncio.sleep(0)

    assert not enqueue_task.done()
    assert queue.get_nowait() is oldest
    queue.task_done()
    await asyncio.wait_for(enqueue_task, timeout=0.1)
    assert queue.get_nowait() is newest
    queue.task_done()
