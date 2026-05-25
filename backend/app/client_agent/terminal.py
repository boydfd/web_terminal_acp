from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import re
import struct
import termios
import tty
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from app.services.runtime.protocol import TerminalPayload

Runner = Callable[[list[str]], Awaitable[str]]
TerminalSender = Callable[[bytes], Awaitable[None]]
SelectionSender = Callable[[UUID], Awaitable[None]]

logger = logging.getLogger(__name__)

PTY_READ_CHUNK_BYTES = 65536
SELECTION_POLL_INTERVAL_SECONDS = 0.25
# Maximum size of the in-memory coalescing buffer that decouples PTY reads from
# the bulk-writer sender. When the downstream send path back-pressures (slow
# server, slow bulk WebSocket, browser stall, ...), bytes accumulate here. The
# PTY itself is always drained so tmux keeps making forward progress and user
# input remains responsive. Beyond this limit the oldest bytes are dropped so a
# pathological burst cannot grow memory without bound; in practice this only
# triggers when the downstream is many MB behind, at which point the user can
# refresh the window to recover the live frame from tmux.
PTY_DRAIN_BUFFER_MAX_BYTES = 16 * 1024 * 1024


def _shadow_session_name(window_id: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", window_id)
    return f"web_terminal_view_{sanitized}"


def _apply_pty_resize(master_fd: int, *, cols: int, rows: int) -> None:
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _configure_pty_slave(slave_fd: int) -> None:
    tty.setraw(slave_fd, termios.TCSANOW)


def _attach_process_environment() -> dict[str, str]:
    return {**os.environ, "TERM": "xterm-256color"}


@dataclass(frozen=True)
class _RemoteTarget:
    remote_session_id: str
    remote_window_id: str

    @property
    def tmux_target(self) -> str:
        return f"{self.remote_session_id}:{self.remote_window_id}"

    @property
    def shadow_session(self) -> str:
        return _shadow_session_name(self.remote_window_id)


@dataclass
class _AttachedTerminal:
    master_fd: int
    process: asyncio.subprocess.Process
    task: asyncio.Task[None] | None = None
    selection_task: asyncio.Task[None] | None = None
    cleanup_started: bool = False
    size: tuple[int, int] | None = None
    # Coalescing buffer that decouples the PTY read loop from the downstream
    # sender. The reader appends here without ever awaiting on the sender; the
    # drainer task copies the contents out (atomically, between awaits) and
    # forwards them via the bulk-writer. See `_pipe_output` for details.
    output_buffer: bytearray = field(default_factory=bytearray)
    output_event: asyncio.Event = field(default_factory=asyncio.Event)
    output_eof: bool = False
    reader_task: asyncio.Task[None] | None = None


class ClientTerminalMultiplexer:
    def __init__(self, *, runner: Runner | None = None) -> None:
        self._runner = runner
        self._windows: dict[str, _RemoteTarget] = {}
        self._attached: dict[str, _AttachedTerminal] = {}
        self._lock = asyncio.Lock()

    def is_registered(self, window_id: UUID | str) -> bool:
        return str(window_id) in self._windows

    def tmux_target_for(self, window_id: UUID | str) -> str | None:
        target = self._windows.get(str(window_id))
        if target is None:
            return None
        return target.tmux_target

    def register_window(
        self,
        window_id: UUID | str,
        remote_session_id: str,
        remote_window_id: str,
    ) -> None:
        self._windows[str(window_id)] = _RemoteTarget(
            remote_session_id=remote_session_id,
            remote_window_id=remote_window_id,
        )

    async def send_input(self, window_id: UUID | str, data: bytes) -> None:
        attached = self._attached_terminal_for(window_id)
        # PTY master read and write use independent kernel buffers, so writes here
        # remain responsive even while output drain is back-pressured by the bulk
        # writer queue.
        await asyncio.to_thread(os.write, attached.master_fd, data)

    async def resize(self, window_id: UUID | str, *, cols: int, rows: int) -> None:
        attached = self._attached_terminal_for(window_id)
        size = (cols, rows)
        if attached.size == size:
            return
        target = self._target_for(window_id)
        await asyncio.to_thread(_apply_pty_resize, attached.master_fd, cols=cols, rows=rows)
        await self._run([
            "tmux",
            "resize-window",
            "-t",
            f"{target.shadow_session}:{target.remote_window_id}",
            "-x",
            str(cols),
            "-y",
            str(rows),
        ])
        attached.size = size

    async def attach(self, window_id: UUID | str, sender: TerminalSender) -> None:
        await self.attach_with_selection(window_id, sender)

    async def attach_with_selection(
        self,
        window_id: UUID | str,
        sender: TerminalSender,
        selection_sender: SelectionSender | None = None,
    ) -> None:
        key = str(window_id)
        target = self._target_for(key)
        async with self._lock:
            existing = self._attached.get(key)
            if existing is not None and existing.task is not None and not existing.task.done():
                return
            self._attached.pop(key, None)

            await self._ensure_shadow_session(target)
            master_fd, slave_fd = pty.openpty()
            try:
                try:
                    _configure_pty_slave(slave_fd)
                    process = await asyncio.create_subprocess_exec(
                        "tmux",
                        "attach-session",
                        "-t",
                        target.shadow_session,
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        close_fds=True,
                        env=_attach_process_environment(),
                    )
                except Exception:
                    with contextlib.suppress(OSError):
                        os.close(master_fd)
                    raise
            finally:
                with contextlib.suppress(OSError):
                    os.close(slave_fd)

            attached = _AttachedTerminal(master_fd=master_fd, process=process)
            attached.task = asyncio.create_task(self._pipe_output(key, attached, sender))
            if selection_sender is not None:
                attached.selection_task = asyncio.create_task(
                    self._watch_active_window(target, selection_sender)
                )
            self._attached[key] = attached

    async def remove_window(self, window_id: UUID | str) -> None:
        await self.detach(window_id)
        self._windows.pop(str(window_id), None)

    async def detach(self, window_id: UUID | str) -> None:
        key = str(window_id)
        async with self._lock:
            attached = self._attached.get(key)
        if attached is None:
            return

        task = attached.task
        if task is not None and not task.done():
            task.cancel()
        await self._cleanup_attachment(key, attached)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def close(self) -> None:
        for window_id in tuple(self._attached):
            await self.detach(window_id)

    async def capture_output(self, window_id: UUID | str) -> TerminalPayload:
        output = await self.capture_output_bytes(window_id)
        return TerminalPayload.from_bytes(UUID(str(window_id)), output)

    async def capture_output_bytes(self, window_id: UUID | str) -> bytes:
        target = self._target_for(window_id)
        output = await self._run(["tmux", "capture-pane", "-p", "-t", target.tmux_target])
        return output.encode("utf-8", errors="surrogateescape")

    async def _ensure_shadow_session(self, target: _RemoteTarget) -> None:
        try:
            await self._run(["tmux", "has-session", "-t", target.shadow_session])
        except RuntimeError:
            await self._run(
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-t",
                    target.remote_session_id,
                    "-s",
                    target.shadow_session,
                ]
            )
        with contextlib.suppress(RuntimeError):
            await self._run(["tmux", "set-option", "-t", target.shadow_session, "window-size", "manual"])
        await self._run(["tmux", "select-window", "-t", f"{target.shadow_session}:{target.remote_window_id}"])

    async def _current_window_id(self, shadow_session: str) -> str:
        return (
            await self._run(
                ["tmux", "display-message", "-p", "-t", shadow_session, "#{window_id}"]
            )
        ).strip()

    def _local_window_id_for_remote(
        self,
        remote_session_id: str,
        remote_window_id: str,
    ) -> UUID | None:
        for local_window_id, target in self._windows.items():
            if (
                target.remote_session_id == remote_session_id
                and target.remote_window_id == remote_window_id
            ):
                return UUID(local_window_id)
        return None

    async def _run(self, args: list[str]) -> str:
        if self._runner is not None:
            return await self._runner(args)

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            error_text = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"tmux command failed ({process.returncode}): {' '.join(args)}: {error_text}")
        return stdout.decode(errors="replace")

    def _target_for(self, window_id: UUID | str) -> _RemoteTarget:
        key = str(window_id)
        try:
            return self._windows[key]
        except KeyError as exc:
            raise KeyError(f"window is not registered with tmux multiplexer: {key}") from exc

    def _attached_terminal_for(self, window_id: UUID | str) -> _AttachedTerminal:
        key = str(window_id)
        attached = self._attached.get(key)
        if attached is None or attached.task is None or attached.task.done():
            raise RuntimeError(f"terminal window is not attached: {key}")
        return attached

    async def _pipe_output(
        self,
        window_id: str,
        attached: _AttachedTerminal,
        sender: TerminalSender,
    ) -> None:
        # Streams the fully-rendered tmux output (status bar, pane borders,
        # popups, copy-mode overlays, etc.) from the attached PTY master back to
        # the server, while keeping the PTY itself drained at all times.
        #
        # A naive `read -> await sender(data)` loop blocks the PTY whenever the
        # downstream is slow (saturated bulk writer queue, slow server, ...).
        # That stalls tmux's event loop because the PTY master's output buffer
        # fills up; tmux can then no longer process input from the same PTY, so
        # `os.write(master_fd, ...)` for the user's keystrokes starts blocking
        # too. Because the control WebSocket recv loop awaits `send_input`, the
        # whole client agent freezes for as long as the bulk path is congested.
        #
        # To avoid that, we split the work between two cooperative tasks:
        #
        #   * The reader sub-task is a tight loop that copies PTY bytes into an
        #     in-memory coalescing buffer. It never awaits the sender, so tmux
        #     always has a consumer for its output and stays responsive.
        #   * The drainer (this coroutine) atomically swaps the buffer out and
        #     forwards it through the bulk-writer sender. If the sender stalls,
        #     only the drainer waits; the reader keeps emptying the PTY into
        #     the buffer. If the buffer ever exceeds the configured cap, the
        #     reader drops the oldest bytes (logging a warning) instead of
        #     blocking tmux.
        attached.output_eof = False
        attached.output_event.clear()
        attached.output_buffer.clear()

        async def reader_loop() -> None:
            try:
                while True:
                    try:
                        data = await asyncio.to_thread(
                            os.read, attached.master_fd, PTY_READ_CHUNK_BYTES
                        )
                    except OSError:
                        return
                    if not data:
                        return
                    attached.output_buffer.extend(data)
                    if len(attached.output_buffer) > PTY_DRAIN_BUFFER_MAX_BYTES:
                        overflow = (
                            len(attached.output_buffer) - PTY_DRAIN_BUFFER_MAX_BYTES
                        )
                        del attached.output_buffer[:overflow]
                        logger.warning(
                            "client-agent PTY output buffer overflowed; "
                            "dropped oldest bytes to keep tmux responsive",
                            extra={
                                "window_id": window_id,
                                "dropped_bytes": overflow,
                                "buffer_bytes": len(attached.output_buffer),
                            },
                        )
                    attached.output_event.set()
            finally:
                attached.output_eof = True
                attached.output_event.set()

        reader_task = asyncio.create_task(reader_loop())
        attached.reader_task = reader_task
        try:
            while True:
                # `bytes(buffer)` followed by `buffer.clear()` runs as a single
                # synchronous block (no awaits in between), so the reader task,
                # which lives on the same event loop, cannot append concurrently
                # and no bytes can be lost during the swap.
                if attached.output_buffer:
                    chunk = bytes(attached.output_buffer)
                    attached.output_buffer.clear()
                    try:
                        await sender(chunk)
                    except Exception:
                        return
                    continue
                if attached.output_eof:
                    return
                # Clear the event before waiting so that any subsequent reader
                # append (and matching `set()`) is observed by `wait()`. Because
                # both reader and drainer execute on the same event loop, no
                # event/buffer state can change between this `clear` and the
                # next `await`.
                attached.output_event.clear()
                if attached.output_buffer or attached.output_eof:
                    continue
                await attached.output_event.wait()
        finally:
            if not reader_task.done():
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader_task
            attached.reader_task = None
            await self._cleanup_attachment(window_id, attached)

    async def _watch_active_window(
        self,
        target: _RemoteTarget,
        selection_sender: SelectionSender,
    ) -> None:
        last_window_id = target.remote_window_id
        while True:
            await asyncio.sleep(SELECTION_POLL_INTERVAL_SECONDS)
            try:
                active_window_id = await self._current_window_id(target.shadow_session)
            except Exception:
                return
            if active_window_id == last_window_id:
                continue
            last_window_id = active_window_id
            local_window_id = self._local_window_id_for_remote(
                target.remote_session_id,
                active_window_id,
            )
            if local_window_id is not None:
                await selection_sender(local_window_id)

    async def _cleanup_attachment(self, window_id: str, attached: _AttachedTerminal) -> None:
        async with self._lock:
            if attached.cleanup_started:
                return
            attached.cleanup_started = True
            if self._attached.get(window_id) is attached:
                self._attached.pop(window_id, None)

        with contextlib.suppress(OSError):
            os.close(attached.master_fd)
        if attached.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                attached.process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(attached.process.wait(), timeout=2)
            if attached.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    attached.process.kill()
                await attached.process.wait()
        selection_task = attached.selection_task
        if selection_task is not None and selection_task is not asyncio.current_task():
            selection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await selection_task
