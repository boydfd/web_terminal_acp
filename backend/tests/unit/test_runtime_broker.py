import asyncio
from uuid import UUID, uuid4

import pytest

from app.services.runtime import broker as broker_module
from app.services.runtime.broker import TerminalBroker, terminal_status_message
from app.services.runtime.types import RuntimeWindow


class FakeRuntime:
    def __init__(self) -> None:
        self.created: list[tuple[str | None, str | None]] = []
        self.attached: list[RuntimeWindow] = []
        self.detached: list[RuntimeWindow] = []
        self.attached_local_window_ids = []
        self.detached_local_window_ids = []
        self.inputs: list[tuple[RuntimeWindow, bytes]] = []
        self.resizes: list[tuple[RuntimeWindow, int, int]] = []
        self.detach_started = asyncio.Event()
        self.allow_detach: asyncio.Event | None = None

    async def create_window(
        self, cwd: str | None = None, shell_command: str | None = None
    ) -> RuntimeWindow:
        self.created.append((cwd, shell_command))
        return RuntimeWindow(session_id="session", window_id="@1")

    async def attach(
        self,
        window: RuntimeWindow,
        sender,
        *,
        local_window_id=None,
        selection_callback=None,
    ) -> None:
        self.attached.append(window)
        self.attached_local_window_ids.append(local_window_id)
        await sender(b"attached")

    async def detach(self, window: RuntimeWindow, *, local_window_id=None) -> None:
        self.detach_started.set()
        self.detached_local_window_ids.append(local_window_id)
        if self.allow_detach is not None:
            await self.allow_detach.wait()
        self.detached.append(window)

    async def send_input(
        self, window: RuntimeWindow, data: bytes, *, local_window_id=None
    ) -> None:
        self.inputs.append((window, data))

    async def resize(self, window: RuntimeWindow, *, cols: int, rows: int, local_window_id=None) -> None:
        self.resizes.append((window, cols, rows))


@pytest.mark.asyncio
async def test_publish_output_fans_out_without_holding_subscription_lock() -> None:
    client_id = uuid4()
    window_id = uuid4()
    broker = TerminalBroker()
    received: list[tuple[str, bytes]] = []

    async def unsubscribing_sender(data: bytes) -> None:
        received.append(("first", data))
        await broker.unsubscribe(client_id, window_id, unsubscribing_sender)

    async def second_sender(data: bytes) -> None:
        received.append(("second", data))

    await broker.subscribe(client_id, window_id, unsubscribing_sender)
    await broker.subscribe(client_id, window_id, second_sender)

    await asyncio.wait_for(broker.publish_output(client_id, window_id, b"chunk"), timeout=1)

    assert sorted(received) == [("first", b"chunk"), ("second", b"chunk")]


@pytest.mark.asyncio
async def test_publish_output_drops_slow_subscriber_and_keeps_healthy_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscriber whose send blocks past the configured timeout must be
    dropped and unsubscribed without delaying the broker beyond the timeout,
    so a half-open browser WebSocket cannot stall the bulk-WS worker."""

    monkeypatch.setattr(
        broker_module,
        "PUBLISH_OUTPUT_SUBSCRIBER_TIMEOUT_SECONDS",
        0.05,
    )

    client_id = uuid4()
    window_id = uuid4()
    broker = TerminalBroker()
    healthy_received: list[bytes] = []
    slow_calls = 0
    release_slow = asyncio.Event()

    async def slow_sender(data: bytes) -> None:
        nonlocal slow_calls
        slow_calls += 1
        await release_slow.wait()

    async def healthy_sender(data: bytes) -> None:
        healthy_received.append(data)

    await broker.subscribe(client_id, window_id, slow_sender)
    await broker.subscribe(client_id, window_id, healthy_sender)

    started = asyncio.get_event_loop().time()
    await asyncio.wait_for(
        broker.publish_output(client_id, window_id, b"first"),
        timeout=1.0,
    )
    elapsed_first = asyncio.get_event_loop().time() - started

    await broker.publish_output(client_id, window_id, b"second")

    release_slow.set()

    assert slow_calls == 1
    assert healthy_received == [b"first", b"second"]
    assert elapsed_first < 0.5, (
        "publish_output must not wait substantially longer than the "
        "per-subscriber timeout when one subscriber is stuck"
    )


@pytest.mark.asyncio
async def test_publish_output_removes_failing_subscribers_and_continues() -> None:
    client_id = uuid4()
    window_id = uuid4()
    broker = TerminalBroker()
    failures: list[bytes] = []
    received: list[bytes] = []

    async def failing_sender(data: bytes) -> None:
        failures.append(data)
        raise RuntimeError("stale browser websocket")

    async def healthy_sender(data: bytes) -> None:
        received.append(data)

    await broker.subscribe(client_id, window_id, failing_sender)
    await broker.subscribe(client_id, window_id, healthy_sender)

    await broker.publish_output(client_id, window_id, b"first")
    await broker.publish_output(client_id, window_id, b"second")

    assert failures == [b"first"]
    assert received == [b"first", b"second"]


@pytest.mark.asyncio
async def test_broker_forwards_input_and_resize_to_registered_runtime() -> None:
    client_id = uuid4()
    browser_window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="session", window_id="@2")
    runtime = FakeRuntime()
    broker = TerminalBroker()
    broker.register_runtime(client_id, runtime)

    await broker.send_input(client_id, browser_window_id, runtime_window, b"ls -la\n")
    await broker.resize(client_id, browser_window_id, runtime_window, cols=120, rows=40)

    assert runtime.inputs == [(runtime_window, b"ls -la\n")]
    assert runtime.resizes == [(runtime_window, 120, 40)]


@pytest.mark.asyncio
async def test_broker_attach_uses_runtime_and_publishes_initial_output() -> None:
    client_id = UUID("00000000-0000-0000-0000-000000000001")
    browser_window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="session", window_id="@3")
    runtime = FakeRuntime()
    broker = TerminalBroker()
    broker.register_runtime(client_id, runtime)
    received: list[bytes] = []

    async def sender(data: bytes) -> None:
        received.append(data)

    await broker.subscribe(client_id, browser_window_id, sender)
    await broker.attach(client_id, browser_window_id, runtime_window)

    assert runtime.attached == [runtime_window]
    assert received == [b"attached"]


@pytest.mark.asyncio
async def test_broker_detaches_runtime_after_last_subscriber_unsubscribes() -> None:
    client_id = UUID("00000000-0000-0000-0000-000000000001")
    browser_window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="session", window_id="@4")
    runtime = FakeRuntime()
    broker = TerminalBroker()
    broker.register_runtime(client_id, runtime)

    async def first_sender(data: bytes) -> None:
        return None

    async def second_sender(data: bytes) -> None:
        return None

    await broker.subscribe(client_id, browser_window_id, first_sender)
    await broker.subscribe(client_id, browser_window_id, second_sender)
    await broker.attach(client_id, browser_window_id, runtime_window)

    await broker.unsubscribe(client_id, browser_window_id, first_sender)

    assert runtime.detached == []

    await broker.unsubscribe(client_id, browser_window_id, second_sender)

    assert runtime.detached == [runtime_window]


@pytest.mark.asyncio
async def test_broker_publishes_status_to_status_subscribers() -> None:
    client_id = UUID("00000000-0000-0000-0000-000000000001")
    browser_window_id = uuid4()
    broker = TerminalBroker()
    received_statuses: list[str] = []

    async def output_sender(data: bytes) -> None:
        raise AssertionError("status publish must not use output sender")

    async def status_sender(message: str) -> None:
        received_statuses.append(message)

    await broker.subscribe(client_id, browser_window_id, output_sender, status_sender)
    await broker.publish_status(
        client_id,
        browser_window_id,
        terminal_status_message("unavailable", reason="client_offline"),
    )

    assert received_statuses == [
        '{"type":"terminal_status","status":"unavailable","reason":"client_offline"}'
    ]


@pytest.mark.asyncio
async def test_broker_clear_client_removes_attachments_and_publishes_status() -> None:
    client_id = UUID("00000000-0000-0000-0000-000000000001")
    browser_window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="session", window_id="@4")
    runtime = FakeRuntime()
    broker = TerminalBroker()
    broker.register_runtime(client_id, runtime)
    received_statuses: list[str] = []

    async def output_sender(data: bytes) -> None:
        return None

    async def status_sender(message: str) -> None:
        received_statuses.append(message)

    await broker.subscribe(client_id, browser_window_id, output_sender, status_sender)
    await broker.attach(client_id, browser_window_id, runtime_window)

    await broker.clear_client(
        client_id,
        status_message=terminal_status_message("unavailable", reason="client_offline"),
    )
    await broker.attach(client_id, browser_window_id, runtime_window)

    assert runtime.attached == [runtime_window, runtime_window]
    assert runtime.detached == []
    assert received_statuses == [
        '{"type":"terminal_status","status":"unavailable","reason":"client_offline"}'
    ]


@pytest.mark.asyncio
async def test_publish_output_is_not_blocked_by_pending_detach() -> None:
    client_id = UUID("00000000-0000-0000-0000-000000000001")
    browser_window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="session", window_id="@5")
    runtime = FakeRuntime()
    runtime.allow_detach = asyncio.Event()
    broker = TerminalBroker()
    broker.register_runtime(client_id, runtime)

    async def sender(data: bytes) -> None:
        return None

    await broker.subscribe(client_id, browser_window_id, sender)
    await broker.attach(client_id, browser_window_id, runtime_window)

    unsubscribe_task = asyncio.create_task(
        broker.unsubscribe(client_id, browser_window_id, sender)
    )
    await runtime.detach_started.wait()

    await asyncio.wait_for(broker.publish_output(client_id, browser_window_id, b"late output"), timeout=0.1)

    runtime.allow_detach.set()
    await unsubscribe_task


@pytest.mark.asyncio
async def test_reconnect_waits_for_pending_final_detach_before_reattaching() -> None:
    client_id = UUID("00000000-0000-0000-0000-000000000001")
    browser_window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="session", window_id="@5")
    runtime = FakeRuntime()
    runtime.allow_detach = asyncio.Event()
    broker = TerminalBroker()
    broker.register_runtime(client_id, runtime)

    async def first_sender(data: bytes) -> None:
        return None

    async def reconnect_sender(data: bytes) -> None:
        return None

    await broker.subscribe(client_id, browser_window_id, first_sender)
    await broker.attach(client_id, browser_window_id, runtime_window)

    unsubscribe_task = asyncio.create_task(
        broker.unsubscribe(client_id, browser_window_id, first_sender)
    )
    await runtime.detach_started.wait()

    subscribe_completed = asyncio.Event()
    attach_completed = asyncio.Event()

    async def reconnect() -> None:
        await broker.subscribe(client_id, browser_window_id, reconnect_sender)
        subscribe_completed.set()
        await broker.attach(client_id, browser_window_id, runtime_window)
        attach_completed.set()

    reconnect_task = asyncio.create_task(reconnect())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    subscribe_completed_while_detaching = subscribe_completed.is_set()
    attach_completed_while_detaching = attach_completed.is_set()

    runtime.allow_detach.set()
    await unsubscribe_task
    await reconnect_task

    assert subscribe_completed_while_detaching
    assert not attach_completed_while_detaching
    assert runtime.detached == [runtime_window]
    assert runtime.attached == [runtime_window, runtime_window]
