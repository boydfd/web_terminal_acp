import asyncio
import contextlib
import os
import fcntl
import struct
import termios

import pytest

from app.services.runtime.local import LocalTerminalRuntime, _LocalTerminalSession
from app.services.runtime.types import RuntimeWindow
from app.services.terminal_bridge import (
    ResizeControl,
    apply_pty_resize,
    attach_process_environment,
    configure_pty_slave,
    parse_text_input,
)


def test_parse_text_input_returns_resize_control_for_resize_json():
    action = parse_text_input('{"type":"resize","cols":120,"rows":40}')

    assert action == ResizeControl(cols=120, rows=40)


def test_parse_text_input_keeps_ordinary_text_as_bytes():
    assert parse_text_input("ls -la\n") == b"ls -la\n"


def test_parse_text_input_suppresses_unknown_json_control_message():
    assert parse_text_input('{"type":"unknown","value":"should not reach shell"}') is None


def test_attach_process_environment_defaults_term(monkeypatch):
    monkeypatch.delenv("TERM", raising=False)

    env = attach_process_environment()

    assert env["TERM"] == "xterm-256color"


def test_apply_pty_resize_uses_tiocswinsz(monkeypatch):
    calls = []

    def fake_ioctl(fd, request, data):
        calls.append((fd, request, data))
        return 0

    monkeypatch.setattr(fcntl, "ioctl", fake_ioctl)

    apply_pty_resize(7, ResizeControl(cols=120, rows=40))

    assert calls == [(7, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))]


def test_configure_pty_slave_sets_raw_mode(monkeypatch):
    calls = []

    def fake_setraw(fd, when):
        calls.append((fd, when))

    monkeypatch.setattr("app.services.terminal_bridge.tty.setraw", fake_setraw)

    configure_pty_slave(11)

    assert calls == [(11, termios.TCSANOW)]


class FakeTmuxManager:
    def __init__(self) -> None:
        self.resizes: list[tuple[RuntimeWindow, int, int]] = []

    async def create_window(self, cwd, shell_command, *, window_id=None):
        assert cwd == "/workspace"
        assert shell_command == "bash"
        assert window_id is None
        return RuntimeWindow(session_id="web-terminal", window_id="@8")

    async def resize_shadow_window(self, window: RuntimeWindow, *, cols: int, rows: int) -> None:
        self.resizes.append((window, cols, rows))


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = 0

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9

    async def wait(self) -> int | None:
        self.waited += 1
        return self.returncode


@pytest.mark.asyncio
async def test_local_terminal_runtime_create_window_returns_runtime_window() -> None:
    runtime = LocalTerminalRuntime(FakeTmuxManager())

    window = await runtime.create_window(cwd="/workspace", shell_command="bash")

    assert window == RuntimeWindow(session_id="web-terminal", window_id="@8")


@pytest.mark.asyncio
async def test_local_terminal_runtime_resize_updates_pty_and_shadow_tmux_window(monkeypatch) -> None:
    resizes: list[tuple[int, ResizeControl]] = []
    tmux_manager = FakeTmuxManager()
    runtime = LocalTerminalRuntime(tmux_manager)
    window = RuntimeWindow(session_id="web-terminal", window_id="@9")
    keepalive = asyncio.create_task(asyncio.sleep(10))

    async def fake_to_thread(func, *args):
        func(*args)

    def fake_resize(fd: int, resize: ResizeControl) -> None:
        resizes.append((fd, resize))

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr("app.services.runtime.local.apply_pty_resize", fake_resize)
    runtime._sessions[window] = _LocalTerminalSession(master_fd=123, process=object(), task=keepalive)
    try:
        await runtime.resize(window, cols=41, rows=44)
    finally:
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive

    assert resizes == [(123, ResizeControl(cols=41, rows=44))]
    assert tmux_manager.resizes == [(window, 41, 44)]


@pytest.mark.asyncio
async def test_local_terminal_runtime_detach_cancels_pipe_task_and_stops_process() -> None:
    runtime = LocalTerminalRuntime(FakeTmuxManager())
    window = RuntimeWindow(session_id="web-terminal", window_id="@9")
    read_fd, write_fd = os.pipe()
    process = FakeProcess()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def pipe_task() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(pipe_task())
    await started.wait()
    runtime._sessions[window] = _LocalTerminalSession(
        master_fd=read_fd,
        process=process,
        task=task,
    )

    try:
        await runtime.detach(window)
    except Exception:
        with contextlib.suppress(OSError):
            os.close(read_fd)
        with contextlib.suppress(OSError):
            os.close(write_fd)
        raise
    with contextlib.suppress(OSError):
        os.close(write_fd)

    with pytest.raises(OSError):
        os.close(read_fd)
    assert cancelled.is_set()
    assert task.cancelled()
    assert process.terminated == 1
    assert process.waited == 1
    assert process.killed == 0
    assert window not in runtime._sessions
