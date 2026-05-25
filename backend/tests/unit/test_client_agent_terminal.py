import asyncio
import contextlib
import threading
from uuid import UUID

import pytest

import app.client_agent.terminal as client_terminal
from app.client_agent.terminal import (
    PTY_DRAIN_BUFFER_MAX_BYTES,
    ClientTerminalMultiplexer,
    _AttachedTerminal,
)


WINDOW_ID = UUID("87654321-4321-8765-4321-876543218765")
OTHER_WINDOW_ID = UUID("11111111-2222-3333-4444-555555555555")


@pytest.mark.asyncio
async def test_send_input_writes_raw_bytes_to_attached_pty(monkeypatch) -> None:
    writes: list[tuple[int, bytes]] = []
    keepalive = asyncio.create_task(asyncio.sleep(10))

    def fake_write(fd: int, data: bytes) -> int:
        writes.append((fd, data))
        return len(data)

    monkeypatch.setattr(client_terminal.os, "write", fake_write)
    multiplexer = ClientTerminalMultiplexer()
    multiplexer._attached[str(WINDOW_ID)] = _AttachedTerminal(master_fd=123, process=object(), task=keepalive)
    try:
        await multiplexer.send_input(WINDOW_ID, b"hello terminal\r")
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert writes == [(123, b"hello terminal\r")]


@pytest.mark.asyncio
async def test_resize_applies_dimensions_to_attached_pty_and_shadow_tmux_window(monkeypatch) -> None:
    resizes: list[tuple[int, int, int]] = []
    calls: list[list[str]] = []
    keepalive = asyncio.create_task(asyncio.sleep(10))

    def fake_resize(fd: int, *, cols: int, rows: int) -> None:
        resizes.append((fd, cols, rows))

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(client_terminal, "_apply_pty_resize", fake_resize)
    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")
    multiplexer._attached[str(WINDOW_ID)] = _AttachedTerminal(master_fd=123, process=object(), task=keepalive)
    try:
        await multiplexer.resize(WINDOW_ID, cols=41, rows=44)
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert resizes == [(123, 41, 44)]
    assert calls == [["tmux", "resize-window", "-t", "web_terminal_view__7:@7", "-x", "41", "-y", "44"]]


@pytest.mark.asyncio
async def test_resize_ignores_repeated_dimensions(monkeypatch) -> None:
    resizes: list[tuple[int, int, int]] = []
    calls: list[list[str]] = []
    keepalive = asyncio.create_task(asyncio.sleep(10))

    def fake_resize(fd: int, *, cols: int, rows: int) -> None:
        resizes.append((fd, cols, rows))

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(client_terminal, "_apply_pty_resize", fake_resize)
    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")
    multiplexer._attached[str(WINDOW_ID)] = _AttachedTerminal(master_fd=123, process=object(), task=keepalive)
    try:
        await multiplexer.resize(WINDOW_ID, cols=41, rows=44)
        await multiplexer.resize(WINDOW_ID, cols=41, rows=44)
        await multiplexer.resize(WINDOW_ID, cols=42, rows=44)
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert resizes == [(123, 41, 44), (123, 42, 44)]
    assert calls == [
        ["tmux", "resize-window", "-t", "web_terminal_view__7:@7", "-x", "41", "-y", "44"],
        ["tmux", "resize-window", "-t", "web_terminal_view__7:@7", "-x", "42", "-y", "44"],
    ]


@pytest.mark.asyncio
async def test_capture_output_returns_terminal_payload_with_base64_output() -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        return "line one\nline two\n"

    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")

    payload = await multiplexer.capture_output(WINDOW_ID)

    assert payload.window_id == WINDOW_ID
    assert payload.to_bytes() == b"line one\nline two\n"
    assert calls == [["tmux", "capture-pane", "-p", "-t", "client_pool:@7"]]


@pytest.mark.asyncio
async def test_attach_streams_raw_tmux_pty_bytes(monkeypatch) -> None:
    calls: list[list[str]] = []
    raw_configured: list[int] = []
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    received: list[bytes] = []
    sent = asyncio.Event()
    reads = [b"\x1b[31mtmux\x1b[0m"]

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args == ["tmux", "has-session", "-t", "web_terminal_view__7"]:
            raise RuntimeError("missing")
        return ""

    class FakeProcess:
        returncode = None

        def terminate(self) -> None:
            self.returncode = -15

        async def wait(self) -> int:
            return self.returncode or 0

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        subprocess_calls.append((args, kwargs))
        return FakeProcess()

    def fake_read(fd: int, size: int) -> bytes:
        assert fd == 10
        assert size == client_terminal.PTY_READ_CHUNK_BYTES
        if reads:
            return reads.pop(0)
        raise OSError

    async def sender(data: bytes) -> None:
        received.append(data)
        sent.set()

    monkeypatch.setattr(client_terminal.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(client_terminal, "_configure_pty_slave", lambda fd: raw_configured.append(fd))
    monkeypatch.setattr(client_terminal.os, "close", lambda fd: None)
    monkeypatch.setattr(client_terminal.os, "read", fake_read)
    monkeypatch.setattr(client_terminal.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")
    await multiplexer.attach(WINDOW_ID, sender)
    await asyncio.wait_for(sent.wait(), timeout=1)
    await multiplexer.detach(WINDOW_ID)

    assert received == [b"\x1b[31mtmux\x1b[0m"]
    assert calls == [
        ["tmux", "has-session", "-t", "web_terminal_view__7"],
        ["tmux", "new-session", "-d", "-t", "client_pool", "-s", "web_terminal_view__7"],
        ["tmux", "set-option", "-t", "web_terminal_view__7", "window-size", "manual"],
        ["tmux", "select-window", "-t", "web_terminal_view__7:@7"],
    ]
    assert not any(call[:2] == ["tmux", "pipe-pane"] for call in calls)
    assert subprocess_calls[0][0][:4] == ("tmux", "attach-session", "-t", "web_terminal_view__7")
    assert raw_configured == [11]


@pytest.mark.asyncio
async def test_watch_active_window_emits_selection_when_shadow_session_changes(
    monkeypatch,
) -> None:
    calls: list[list[str]] = []
    selected: list[UUID] = []
    current_window = ["@7"]
    second_selection = asyncio.Event()
    release_read = threading.Event()

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args == ["tmux", "has-session", "-t", "web_terminal_view__7"]:
            return ""
        if args == ["tmux", "display-message", "-p", "-t", "web_terminal_view__7", "#{window_id}"]:
            return current_window[0]
        return ""

    class FakeProcess:
        returncode = None

        def terminate(self) -> None:
            self.returncode = -15

        async def wait(self) -> int:
            return self.returncode or 0

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    def fake_read(fd: int, size: int) -> bytes:
        release_read.wait(timeout=5)
        raise OSError

    async def sender(_data: bytes) -> None:
        return None

    async def selection_sender(window_id: UUID) -> None:
        selected.append(window_id)
        if window_id == OTHER_WINDOW_ID:
            second_selection.set()

    monkeypatch.setattr(client_terminal.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(client_terminal, "_configure_pty_slave", lambda fd: None)
    monkeypatch.setattr(client_terminal.os, "close", lambda fd: None)
    monkeypatch.setattr(client_terminal.os, "read", fake_read)
    monkeypatch.setattr(client_terminal, "SELECTION_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(client_terminal.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")
    multiplexer.register_window(OTHER_WINDOW_ID, "client_pool", "@8")

    await multiplexer.attach_with_selection(WINDOW_ID, sender, selection_sender=selection_sender)
    try:
        await asyncio.sleep(0.05)
        current_window[0] = "@8"
        await asyncio.wait_for(second_selection.wait(), timeout=2)
    finally:
        release_read.set()
        await multiplexer.detach(WINDOW_ID)

    assert selected == [OTHER_WINDOW_ID]
    assert not any(call[:2] == ["tmux", "pipe-pane"] for call in calls)
    assert ["tmux", "display-message", "-p", "-t", "web_terminal_view__7", "#{window_id}"] in calls


@pytest.mark.asyncio
async def test_pipe_output_keeps_draining_pty_when_sender_back_pressures(monkeypatch) -> None:
    """The PTY reader must keep emptying the master fd even while the sender is
    stalled, so that a busy tmux pane never blocks user input through the same
    PTY. Regression for the case where typing into one terminal froze whenever
    another terminal was streaming heavy codex output.
    """

    pending_chunks = [b"chunk-1", b"chunk-2", b"chunk-3", b"chunk-4"]
    chunks_read_event = asyncio.Event()
    release_sender = asyncio.Event()
    sender_started_event = asyncio.Event()
    sender_calls: list[bytes] = []

    def fake_read(fd: int, size: int) -> bytes:
        if pending_chunks:
            return pending_chunks.pop(0)
        chunks_read_event.set()
        raise OSError

    async def slow_sender(data: bytes) -> None:
        sender_calls.append(data)
        sender_started_event.set()
        await release_sender.wait()

    async def fake_run(args: list[str]) -> str:
        if args == ["tmux", "has-session", "-t", "web_terminal_view__7"]:
            return ""
        return ""

    class FakeProcess:
        returncode = None

        def terminate(self) -> None:
            self.returncode = -15

        async def wait(self) -> int:
            return self.returncode or 0

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(client_terminal.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(client_terminal, "_configure_pty_slave", lambda fd: None)
    monkeypatch.setattr(client_terminal.os, "close", lambda fd: None)
    monkeypatch.setattr(client_terminal.os, "read", fake_read)
    monkeypatch.setattr(client_terminal.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")
    await multiplexer.attach(WINDOW_ID, slow_sender)

    # First chunk reaches the sender, which then blocks.
    await asyncio.wait_for(sender_started_event.wait(), timeout=1)
    assert sender_calls == [b"chunk-1"]

    # While the sender is stalled the PTY reader must keep draining; it should
    # consume every remaining read (including the OSError that terminates it).
    await asyncio.wait_for(chunks_read_event.wait(), timeout=1)
    assert pending_chunks == []

    # Release the sender. The remaining bytes were coalesced inside the in-memory
    # buffer while the sender was stuck and must be delivered in order.
    release_sender.set()

    deadline = asyncio.get_event_loop().time() + 1
    while b"".join(sender_calls) != b"chunk-1chunk-2chunk-3chunk-4":
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                f"sender did not receive coalesced remainder: {sender_calls!r}"
            )
        await asyncio.sleep(0.01)

    await multiplexer.detach(WINDOW_ID)
    assert b"".join(sender_calls) == b"chunk-1chunk-2chunk-3chunk-4"


@pytest.mark.asyncio
async def test_pipe_output_drops_oldest_bytes_when_buffer_overflows(monkeypatch) -> None:
    """When the downstream is so slow that the in-memory buffer would grow past
    the configured cap, the reader must drop the oldest bytes instead of
    blocking the PTY. This keeps tmux responsive even under pathological
    back-pressure.
    """

    chunk_size = 64 * 1024
    # Produce twice the buffer cap so dropping must happen somewhere.
    num_chunks = (PTY_DRAIN_BUFFER_MAX_BYTES * 2) // chunk_size
    pending_chunks = [b"A" * chunk_size for _ in range(num_chunks)]
    # Sentinel is the very last chunk; it must survive at the tail.
    sentinel = b"Z" * 1024
    pending_chunks.append(sentinel)
    release_sender = asyncio.Event()
    sender_started_event = asyncio.Event()
    sender_calls: list[bytes] = []

    def fake_read(fd: int, size: int) -> bytes:
        if pending_chunks:
            return pending_chunks.pop(0)
        raise OSError

    async def stalled_sender(data: bytes) -> None:
        sender_calls.append(data)
        sender_started_event.set()
        await release_sender.wait()

    async def fake_run(args: list[str]) -> str:
        return ""

    class FakeProcess:
        returncode = None

        def terminate(self) -> None:
            self.returncode = -15

        async def wait(self) -> int:
            return self.returncode or 0

    async def fake_create_subprocess_exec(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(client_terminal.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(client_terminal, "_configure_pty_slave", lambda fd: None)
    monkeypatch.setattr(client_terminal.os, "close", lambda fd: None)
    monkeypatch.setattr(client_terminal.os, "read", fake_read)
    monkeypatch.setattr(client_terminal.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")
    await multiplexer.attach(WINDOW_ID, stalled_sender)

    await asyncio.wait_for(sender_started_event.wait(), timeout=2)
    # While the sender is stuck on the first chunk, the reader keeps draining
    # the (mocked) PTY into the in-memory buffer. Poll the shared list (no
    # asyncio.Event.set across threads) until the reader has emptied every
    # pending chunk, proving it never stalled.
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5
    while pending_chunks:
        if loop.time() > deadline:
            raise AssertionError(
                f"reader did not finish draining pending chunks (left={len(pending_chunks)})"
            )
        await asyncio.sleep(0.02)

    # Release the sender so the drainer can deliver the coalesced remainder.
    release_sender.set()

    # The coalesced remainder must end with the sentinel; the reader keeps the
    # most recent bytes when it drops on overflow.
    deadline = loop.time() + 5
    while True:
        combined = b"".join(sender_calls)
        if combined.endswith(sentinel):
            break
        if loop.time() > deadline:
            raise AssertionError(
                f"sender did not receive sentinel; got {len(combined)} bytes, "
                f"tail={combined[-200:]!r}"
            )
        await asyncio.sleep(0.02)

    await multiplexer.detach(WINDOW_ID)

    combined = b"".join(sender_calls)
    total_produced = num_chunks * chunk_size + len(sentinel)
    # Drops must have happened: total delivered is strictly less than total
    # produced (otherwise no back-pressure mitigation occurred).
    assert len(combined) < total_produced
    assert combined.endswith(sentinel)
