import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import errno
import os
import pty
import select
import shlex
import shutil
import subprocess
import threading
import time
from uuid import UUID

import pytest

import app.client_agent.terminal as client_terminal
from app.client_agent.terminal import (
    PTY_DRAIN_BUFFER_MAX_BYTES,
    PTY_OUTPUT_SEND_CHUNK_BYTES,
    ClientTerminalMultiplexer,
    _AttachedTerminal,
)
from app.client_agent.shell_hook import build_managed_shell_command


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
    multiplexer._attached[str(WINDOW_ID)] = _AttachedTerminal(
        master_fd=123,
        process=object(),
        shadow_session="web_terminal_view__7",
        task=keepalive,
    )
    try:
        await multiplexer.send_input(WINDOW_ID, b"hello terminal\r")
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert writes == [(123, b"hello terminal\r")]


@pytest.mark.asyncio
async def test_send_input_is_not_blocked_by_default_executor_starvation(monkeypatch) -> None:
    writes: list[tuple[int, bytes]] = []
    keepalive = asyncio.create_task(asyncio.sleep(10))
    default_executor = ThreadPoolExecutor(max_workers=1)
    default_worker_started = threading.Event()
    release_default_worker = threading.Event()

    def occupy_default_executor() -> None:
        default_worker_started.set()
        release_default_worker.wait(timeout=5)

    def fake_write(fd: int, data: bytes) -> int:
        writes.append((fd, data))
        return len(data)

    monkeypatch.setattr(client_terminal.os, "write", fake_write)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(default_executor)
    default_worker_task = loop.run_in_executor(None, occupy_default_executor)

    multiplexer = ClientTerminalMultiplexer()
    multiplexer._attached[str(WINDOW_ID)] = _AttachedTerminal(
        master_fd=123,
        process=object(),
        shadow_session="web_terminal_view__7",
        task=keepalive,
    )
    try:
        deadline = loop.time() + 1
        while not default_worker_started.is_set():
            if loop.time() > deadline:
                raise AssertionError("default executor worker did not start")
            await asyncio.sleep(0.01)

        await asyncio.wait_for(
            multiplexer.send_input(WINDOW_ID, b"hello terminal\r"),
            timeout=0.5,
        )
    finally:
        release_default_worker.set()
        with contextlib.suppress(asyncio.CancelledError):
            await default_worker_task
        default_executor.shutdown(wait=True)
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert writes == [(123, b"hello terminal\r")]


@pytest.mark.asyncio
async def test_small_send_input_uses_immediate_writable_pty_fast_path(monkeypatch) -> None:
    writes: list[tuple[int, bytes]] = []
    control_calls: list[tuple[object, ...]] = []
    keepalive = asyncio.create_task(asyncio.sleep(10))

    def fake_select(read_list, write_list, error_list, timeout):
        assert read_list == []
        assert write_list == [123]
        assert error_list == []
        assert timeout == 0
        return [], [123], []

    def fake_write(fd: int, data: bytes) -> int:
        writes.append((fd, bytes(data)))
        return len(data)

    async def fake_run_pty_control(*args) -> None:
        control_calls.append(args)

    monkeypatch.setattr(client_terminal.select, "select", fake_select)
    monkeypatch.setattr(client_terminal.os, "write", fake_write)
    monkeypatch.setattr(client_terminal, "_run_pty_control", fake_run_pty_control)

    multiplexer = ClientTerminalMultiplexer()
    multiplexer._attached[str(WINDOW_ID)] = _AttachedTerminal(
        master_fd=123,
        process=object(),
        shadow_session="web_terminal_view__7",
        task=keepalive,
    )
    try:
        await multiplexer.send_input(WINDOW_ID, b"x")
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert writes == [(123, b"x")]
    assert control_calls == []


@pytest.mark.asyncio
async def test_send_input_falls_back_to_executor_when_pty_is_not_immediately_writable(monkeypatch) -> None:
    writes: list[tuple[int, bytes]] = []
    keepalive = asyncio.create_task(asyncio.sleep(10))

    def fake_select(_read_list, _write_list, _error_list, _timeout):
        return [], [], []

    def fake_write(fd: int, data: bytes) -> int:
        writes.append((fd, bytes(data)))
        return len(data)

    monkeypatch.setattr(client_terminal.select, "select", fake_select)
    monkeypatch.setattr(client_terminal.os, "write", fake_write)

    multiplexer = ClientTerminalMultiplexer()
    multiplexer._attached[str(WINDOW_ID)] = _AttachedTerminal(
        master_fd=123,
        process=object(),
        shadow_session="web_terminal_view__7",
        task=keepalive,
    )
    try:
        await multiplexer.send_input(WINDOW_ID, b"x")
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert writes == [(123, b"x")]


@pytest.mark.asyncio
async def test_resize_applies_dimensions_to_attached_pty_and_shadow_tmux_window(monkeypatch) -> None:
    resizes: list[tuple[int, int, int]] = []
    signals: list[int] = []
    calls: list[list[str]] = []
    keepalive = asyncio.create_task(asyncio.sleep(10))

    class FakeProcess:
        returncode = None

        def send_signal(self, signal_number: int) -> None:
            signals.append(signal_number)

    def fake_resize(fd: int, *, cols: int, rows: int) -> None:
        resizes.append((fd, cols, rows))

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(client_terminal, "_apply_pty_resize", fake_resize)
    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")
    multiplexer._attached[str(WINDOW_ID)] = _AttachedTerminal(
        master_fd=123,
        process=FakeProcess(),
        shadow_session="web_terminal_view__7",
        task=keepalive,
    )
    try:
        await multiplexer.resize(WINDOW_ID, cols=41, rows=44)
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert resizes == [(123, 41, 44)]
    assert signals == [client_terminal.signal.SIGWINCH]
    assert calls == [["tmux", "resize-window", "-t", "web_terminal_view__7:@7", "-x", "41", "-y", "44"]]


@pytest.mark.asyncio
async def test_resize_ignores_repeated_dimensions(monkeypatch) -> None:
    resizes: list[tuple[int, int, int]] = []
    signals: list[int] = []
    calls: list[list[str]] = []
    keepalive = asyncio.create_task(asyncio.sleep(10))

    class FakeProcess:
        returncode = None

        def send_signal(self, signal_number: int) -> None:
            signals.append(signal_number)

    def fake_resize(fd: int, *, cols: int, rows: int) -> None:
        resizes.append((fd, cols, rows))

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(client_terminal, "_apply_pty_resize", fake_resize)
    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")
    multiplexer._attached[str(WINDOW_ID)] = _AttachedTerminal(
        master_fd=123,
        process=FakeProcess(),
        shadow_session="web_terminal_view__7",
        task=keepalive,
    )
    try:
        await multiplexer.resize(WINDOW_ID, cols=41, rows=44)
        await multiplexer.resize(WINDOW_ID, cols=41, rows=44)
        await multiplexer.resize(WINDOW_ID, cols=42, rows=44)
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert resizes == [(123, 41, 44), (123, 42, 44)]
    assert signals == [client_terminal.signal.SIGWINCH, client_terminal.signal.SIGWINCH]
    assert calls == [
        ["tmux", "resize-window", "-t", "web_terminal_view__7:@7", "-x", "41", "-y", "44"],
        ["tmux", "resize-window", "-t", "web_terminal_view__7:@7", "-x", "42", "-y", "44"],
    ]


@pytest.mark.asyncio
async def test_resize_returns_before_shadow_tmux_resize_completes(monkeypatch) -> None:
    writes: list[tuple[int, bytes]] = []
    resizes: list[tuple[int, int, int]] = []
    shadow_resize_started = asyncio.Event()
    release_shadow_resize = asyncio.Event()
    keepalive = asyncio.create_task(asyncio.sleep(10))

    class FakeProcess:
        returncode = None

        def send_signal(self, signal_number: int) -> None:
            return None

    def fake_resize(fd: int, *, cols: int, rows: int) -> None:
        resizes.append((fd, cols, rows))

    def fake_write(fd: int, data: bytes) -> int:
        writes.append((fd, data))
        return len(data)

    async def fake_run(args: list[str]) -> str:
        if args[:2] == ["tmux", "resize-window"]:
            shadow_resize_started.set()
            await release_shadow_resize.wait()
        return ""

    monkeypatch.setattr(client_terminal, "_apply_pty_resize", fake_resize)
    monkeypatch.setattr(client_terminal.os, "write", fake_write)
    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")
    multiplexer._attached[str(WINDOW_ID)] = _AttachedTerminal(
        master_fd=123,
        process=FakeProcess(),
        shadow_session="web_terminal_view__7",
        task=keepalive,
    )
    try:
        resize_task = asyncio.create_task(multiplexer.resize(WINDOW_ID, cols=100, rows=30))
        await asyncio.wait_for(shadow_resize_started.wait(), timeout=1)

        assert resize_task.done(), "resize must not block input behind shadow tmux resize"
        await multiplexer.send_input(WINDOW_ID, b"x")
    finally:
        release_shadow_resize.set()
        await asyncio.wait_for(resize_task, timeout=1)
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert resizes == [(123, 100, 30)]
    assert writes == [(123, b"x")]


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
        if args == ["tmux", "display-message", "-p", "-t", "client_pool:@7", "#{window_id}"]:
            return "@7\n"
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
        ["tmux", "display-message", "-p", "-t", "client_pool:@7", "#{window_id}"],
        ["tmux", "has-session", "-t", "web_terminal_view__7"],
        ["tmux", "new-session", "-d", "-t", "client_pool", "-s", "web_terminal_view__7"],
        ["tmux", "set-option", "-t", "web_terminal_view__7", "window-size", "manual"],
        ["tmux", "set-option", "-t", "web_terminal_view__7", "mouse", "on"],
        ["tmux", "select-window", "-t", "web_terminal_view__7:@7"],
        ["tmux", "set-option", "-p", "-t", "web_terminal_view__7:@7", "allow-passthrough", "on"],
        ["tmux", "kill-session", "-t", "web_terminal_view__7"],
    ]
    assert not any(call[:2] == ["tmux", "pipe-pane"] for call in calls)
    assert subprocess_calls[0][0][:4] == ("tmux", "attach-session", "-t", "web_terminal_view__7")
    assert raw_configured == [11]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.asyncio
async def test_tmux_attach_stream_preserves_managed_shell_command_markers() -> None:
    session = f"wt_marker_unit_{os.getpid()}_{int(time.time() * 1000)}"
    env = {**os.environ, "TERM": "xterm-256color"}
    managed_shell = build_managed_shell_command(
        shell="/bin/bash",
        client_id="12345678-1234-5678-1234-567812345678",
        window_id=WINDOW_ID,
        server_url="http://127.0.0.1:8000",
        project_path="/tmp",
    ).command

    subprocess.run(["tmux", "kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-x", "80", "-y", "24", managed_shell],
            check=True,
            env=env,
        )
        subprocess.run(
            ["tmux", "set-option", "-t", session, "allow-passthrough", "on"],
            check=True,
            env=env,
        )
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            ["tmux", "attach-session", "-t", session],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=env,
        )
        os.close(slave_fd)
        try:
            output = bytearray()
            sent = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and b"web-terminal-command" not in output:
                readable, _, _ = select.select([master_fd], [], [], 0.05)
                if readable:
                    try:
                        chunk = os.read(master_fd, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    output.extend(chunk)
                    if not sent and (b"$ " in output or b"# " in output):
                        os.write(master_fd, b"echo tmux-marker-test\n")
                        sent = True
                elif not sent and time.monotonic() > deadline - 3:
                    os.write(master_fd, b"echo tmux-marker-test\n")
                    sent = True

            assert sent is True
            assert b"web-terminal-command" in output
        finally:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            os.close(master_fd)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.asyncio
async def test_tmux_literal_send_keys_command_reaches_shell_hook_and_history(tmp_path) -> None:
    session = f"wt_send_keys_unit_{os.getpid()}_{int(time.time() * 1000)}"
    env = {**os.environ, "TERM": "xterm-256color"}
    history_path = tmp_path / "history.txt"
    managed_shell = build_managed_shell_command(
        shell="/bin/bash",
        client_id="12345678-1234-5678-1234-567812345678",
        window_id=WINDOW_ID,
        server_url="http://127.0.0.1:8000",
        project_path="/tmp",
    ).command
    agent_command = "echo WT_SEND_KEYS_HISTORY_TOKEN"
    history_command = f"fc -ln -2 > {shlex.quote(str(history_path))}"

    subprocess.run(["tmux", "kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-x", "80", "-y", "24", managed_shell],
            check=True,
            env=env,
        )
        subprocess.run(
            ["tmux", "set-option", "-t", session, "allow-passthrough", "on"],
            check=True,
            env=env,
        )
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            ["tmux", "attach-session", "-t", session],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=env,
        )
        os.close(slave_fd)
        try:
            output = bytearray()
            sent_agent = False
            sent_history = False
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master_fd], [], [], 0.05)
                if readable:
                    try:
                        chunk = os.read(master_fd, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    output.extend(chunk)
                if not sent_agent and (b"$ " in output or b"# " in output):
                    subprocess.run(["tmux", "send-keys", "-l", "-t", session, "--", agent_command], check=True, env=env)
                    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=True, env=env)
                    sent_agent = True
                if sent_agent and not sent_history and b"WT_SEND_KEYS_HISTORY_TOKEN" in output:
                    subprocess.run(["tmux", "send-keys", "-l", "-t", session, "--", history_command], check=True, env=env)
                    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=True, env=env)
                    sent_history = True
                if sent_history and history_path.exists() and b"web-terminal-command" in output:
                    break

            assert sent_agent is True
            assert sent_history is True
            assert b"web-terminal-command" in output
            assert agent_command in history_path.read_text(encoding="utf-8")
        finally:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            os.close(master_fd)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


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
        if args == ["tmux", "display-message", "-p", "-t", "client_pool:@7", "#{window_id}"]:
            return "@7\n"
        if args == ["tmux", "display-message", "-p", "-t", "client_pool:@8", "#{window_id}"]:
            return "@8\n"
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
async def test_select_window_switches_view_shadow_session_without_new_attach() -> None:
    calls: list[list[str]] = []
    view_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    keepalive = asyncio.create_task(asyncio.sleep(10))

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args == ["tmux", "display-message", "-p", "-t", "client_pool:@8", "#{window_id}"]:
            return "@8\n"
        return ""

    class FakeProcess:
        returncode = None

        def send_signal(self, signal_number: int) -> None:
            return None

    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")
    multiplexer.register_window(OTHER_WINDOW_ID, "client_pool", "@8")
    multiplexer._attached[str(view_id)] = _AttachedTerminal(
        master_fd=123,
        process=FakeProcess(),
        shadow_session="web_terminal_view_view_one",
        task=keepalive,
        size=(120, 40),
    )
    try:
        await multiplexer.select_window(OTHER_WINDOW_ID, view_id=view_id)
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert calls == [
        ["tmux", "display-message", "-p", "-t", "client_pool:@8", "#{window_id}"],
        ["tmux", "select-window", "-t", f"web_terminal_view_{view_id}:@8"],
        ["tmux", "select-window", "-t", "client_pool:@8"],
        ["tmux", "resize-window", "-t", f"web_terminal_view_{view_id}:@8", "-x", "120", "-y", "40"],
    ]


@pytest.mark.asyncio
async def test_attach_fails_before_shadow_attach_when_registered_tmux_window_is_missing() -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args == ["tmux", "display-message", "-p", "-t", "client_pool:@7", "#{window_id}"]:
            raise RuntimeError("missing window")
        return ""

    async def sender(_data: bytes) -> None:
        return None

    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")

    with pytest.raises(RuntimeError, match="tmux window is missing"):
        await multiplexer.attach(WINDOW_ID, sender)

    assert calls == [["tmux", "display-message", "-p", "-t", "client_pool:@7", "#{window_id}"]]


@pytest.mark.asyncio
async def test_attach_fails_when_registered_tmux_target_resolves_to_different_window() -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        if args == ["tmux", "display-message", "-p", "-t", "client_pool:@7", "#{window_id}"]:
            return "@8\n"
        return ""

    async def sender(_data: bytes) -> None:
        return None

    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")

    with pytest.raises(RuntimeError, match="tmux window is missing"):
        await multiplexer.attach(WINDOW_ID, sender)

    assert calls == [["tmux", "display-message", "-p", "-t", "client_pool:@7", "#{window_id}"]]


@pytest.mark.asyncio
async def test_remove_window_detaches_matching_view_attachment(monkeypatch) -> None:
    closed: list[int] = []
    terminated: list[bool] = []
    view_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    keepalive = asyncio.create_task(asyncio.sleep(10))

    class FakeProcess:
        returncode = None

        def terminate(self) -> None:
            terminated.append(True)
            self.returncode = -15

        async def wait(self) -> int:
            return self.returncode or 0

    monkeypatch.setattr(client_terminal.os, "close", lambda fd: closed.append(fd))

    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> str:
        calls.append(args)
        return ""

    multiplexer = ClientTerminalMultiplexer(runner=fake_run)
    multiplexer.register_window(WINDOW_ID, "client_pool", "@7")
    multiplexer.register_window(OTHER_WINDOW_ID, "client_pool", "@8")
    multiplexer._attached[str(view_id)] = _AttachedTerminal(
        master_fd=123,
        process=FakeProcess(),
        shadow_session="web_terminal_view_view_one",
        task=keepalive,
    )
    multiplexer._attachment_windows[str(view_id)] = str(OTHER_WINDOW_ID)

    await multiplexer.remove_window(OTHER_WINDOW_ID)

    assert closed == [123]
    assert terminated == [True]
    assert str(view_id) not in multiplexer._attached
    assert str(view_id) not in multiplexer._attachment_windows
    assert not multiplexer.is_registered(OTHER_WINDOW_ID)
    assert ["tmux", "kill-session", "-t", "web_terminal_view_view_one"] in calls


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
        if args == ["tmux", "display-message", "-p", "-t", "client_pool:@7", "#{window_id}"]:
            return "@7\n"
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
async def test_pipe_output_sends_large_drained_buffer_in_small_chunks(monkeypatch) -> None:
    oversized_output = b"A" * (PTY_OUTPUT_SEND_CHUNK_BYTES * 2 + 17)
    sender_calls: list[bytes] = []
    first_chunk_sent = asyncio.Event()

    def fake_read(fd: int, size: int) -> bytes:
        if fake_read.pending:
            fake_read.pending = False
            return oversized_output
        raise OSError

    fake_read.pending = True

    async def sender(data: bytes) -> None:
        sender_calls.append(data)
        first_chunk_sent.set()

    async def fake_run(args: list[str]) -> str:
        if args == ["tmux", "display-message", "-p", "-t", "client_pool:@7", "#{window_id}"]:
            return "@7\n"
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
    await multiplexer.attach(WINDOW_ID, sender)
    await asyncio.wait_for(first_chunk_sent.wait(), timeout=1)

    deadline = asyncio.get_event_loop().time() + 1
    while b"".join(sender_calls) != oversized_output:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"sender did not receive all chunks: {list(map(len, sender_calls))}")
        await asyncio.sleep(0.01)

    await multiplexer.detach(WINDOW_ID)
    assert [len(chunk) for chunk in sender_calls] == [
        PTY_OUTPUT_SEND_CHUNK_BYTES,
        PTY_OUTPUT_SEND_CHUNK_BYTES,
        17,
    ]


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
        if args == ["tmux", "display-message", "-p", "-t", "client_pool:@7", "#{window_id}"]:
            return "@7\n"
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
