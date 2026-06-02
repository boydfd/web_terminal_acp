from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pty
import select
import signal
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from uuid import UUID

from app.services.runtime.types import RuntimeWindow, TerminalSelectionCallback, TerminalSender
from app.services.terminal_bridge import (
    ResizeControl,
    apply_pty_resize,
    attach_process_environment,
    configure_pty_slave,
)
from app.services.tmux_manager import TmuxManager, TmuxTarget, build_attach_command

logger = logging.getLogger(__name__)

PTY_READ_CHUNK_BYTES = 65536
PTY_FAST_INPUT_MAX_BYTES = 256
PTY_CONTROL_EXECUTOR_MAX_WORKERS = 8
PTY_DRAIN_BUFFER_MAX_BYTES = 16 * 1024 * 1024
PTY_CONTROL_EXECUTOR = ThreadPoolExecutor(
    max_workers=PTY_CONTROL_EXECUTOR_MAX_WORKERS,
    thread_name_prefix="web-terminal-local-pty-control",
)


async def _run_pty_control(func, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        PTY_CONTROL_EXECUTOR,
        partial(func, *args, **kwargs),
    )


def _try_write_pty_input_immediately(master_fd: int, data: bytes) -> int:
    if not data or len(data) > PTY_FAST_INPUT_MAX_BYTES:
        return 0
    try:
        _, writable, _ = select.select([], [master_fd], [], 0)
    except (OSError, ValueError):
        return 0
    if not writable:
        return 0
    try:
        return os.write(master_fd, data)
    except (BlockingIOError, InterruptedError, OSError):
        return 0


def _write_all_pty_input(master_fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(master_fd, remaining)
        if written <= 0:
            raise BlockingIOError("PTY input write made no progress")
        remaining = remaining[written:]


def _notify_process_window_change(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(signal.SIGWINCH)


@dataclass
class _LocalTerminalSession:
    master_fd: int
    process: asyncio.subprocess.Process
    shadow_window_id: str | None = None
    shadow_view_id: str | None = None
    task: asyncio.Task[None] | None = None
    selection_task: asyncio.Task[None] | None = None
    cleanup_started: bool = False
    size: tuple[int, int] | None = None
    output_buffer: bytearray = field(default_factory=bytearray)
    output_event: asyncio.Event = field(default_factory=asyncio.Event)
    output_eof: bool = False
    reader_task: asyncio.Task[None] | None = None
    resize_task: asyncio.Task[None] | None = None


class LocalTerminalRuntime:
    def __init__(self, tmux_manager: TmuxManager) -> None:
        self._tmux_manager = tmux_manager
        self._sessions: dict[tuple[str, str], _LocalTerminalSession] = {}
        self._lock = asyncio.Lock()

    async def create_window(
        self,
        cwd: str | None = None,
        shell_command: str | None = None,
        *,
        window_id: object | None = None,
    ) -> RuntimeWindow:
        target = await self._tmux_manager.create_window(cwd, shell_command, window_id=window_id)
        if isinstance(target, RuntimeWindow):
            return target
        return RuntimeWindow(
            session_id=target.session,
            window_id=target.window_id,
            cwd=target.cwd,
            shell_command=target.shell_command,
        )

    async def attach(
        self,
        window: RuntimeWindow,
        sender: TerminalSender,
        *,
        local_window_id: object | None = None,
        selection_callback: TerminalSelectionCallback | None = None,
        view_id: UUID | str | None = None,
    ) -> RuntimeWindow | None:
        window = await self._ensure_runtime_window(window, local_window_id=local_window_id)
        key = _attachment_key(window, view_id)
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and not existing.task.done():
                return window
            self._sessions.pop(key, None)

            attach_target = await self._tmux_manager.ensure_shadow_session(
                TmuxTarget(
                    session=window.session_id,
                    window_id=window.window_id,
                    cwd=window.cwd,
                    shell_command=window.shell_command,
                ),
                view_id=str(view_id) if view_id is not None else None,
            )
            master_fd, slave_fd = pty.openpty()
            try:
                try:
                    configure_pty_slave(slave_fd)
                    process = await asyncio.create_subprocess_exec(
                        *build_attach_command(attach_target),
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        close_fds=True,
                        env=attach_process_environment(),
                    )
                except Exception:
                    with contextlib.suppress(OSError):
                        os.close(master_fd)
                    raise
            finally:
                with contextlib.suppress(OSError):
                    os.close(slave_fd)

            session = _LocalTerminalSession(
                master_fd=master_fd,
                process=process,
                shadow_window_id=window.window_id,
                shadow_view_id=str(view_id) if view_id is not None else None,
            )
            session.task = asyncio.create_task(self._pipe_output(key, session, sender))
            if selection_callback is not None:
                session.selection_task = asyncio.create_task(
                    self._watch_active_window(window, attach_target.session, selection_callback)
                )
            self._sessions[key] = session
        return window

    async def detach(
        self,
        window: RuntimeWindow,
        *,
        local_window_id: object | None = None,
        view_id: UUID | str | None = None,
    ) -> None:
        key = _attachment_key(window, view_id)
        async with self._lock:
            session = self._sessions.get(key)
        if session is None:
            return

        task = session.task
        if task is not None and not task.done():
            task.cancel()
        await self._cleanup_session(key, session)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def send_input(
        self,
        window: RuntimeWindow,
        data: bytes,
        *,
        local_window_id: object | None = None,
        view_id: UUID | str | None = None,
    ) -> None:
        session = self._session_for(window, view_id=view_id)
        written = _try_write_pty_input_immediately(session.master_fd, data)
        if written >= len(data):
            return
        await _run_pty_control(_write_all_pty_input, session.master_fd, data[written:])

    async def resize(
        self,
        window: RuntimeWindow,
        *,
        cols: int,
        rows: int,
        local_window_id: object | None = None,
        view_id: UUID | str | None = None,
    ) -> None:
        session = self._session_for(window, view_id=view_id)
        size = (cols, rows)
        if session.size == size:
            return
        await _run_pty_control(
            apply_pty_resize,
            session.master_fd,
            ResizeControl(cols=cols, rows=rows),
        )
        _notify_process_window_change(session.process)
        session.size = size
        previous_resize_task = session.resize_task
        if previous_resize_task is not None and not previous_resize_task.done():
            previous_resize_task.cancel()
        session.resize_task = asyncio.create_task(self._sync_shadow_window_size(
            window,
            cols=cols,
            rows=rows,
            view_id=str(view_id) if view_id is not None else None,
        ))

    async def select_window(
        self,
        current_window: RuntimeWindow,
        next_window: RuntimeWindow,
        *,
        local_window_id: object,
        view_id: UUID | str | None = None,
    ) -> RuntimeWindow | None:
        next_window = await self._ensure_runtime_window(
            next_window,
            local_window_id=local_window_id,
        )
        session = self._session_for(current_window, view_id=view_id)
        await self._tmux_manager.select_shadow_window(
            next_window,
            view_id=str(view_id) if view_id is not None else next_window.window_id,
        )
        await self._tmux_manager.select_window(next_window)
        size = session.size
        if size is not None:
            await self._tmux_manager.resize_shadow_window(
                next_window,
                cols=size[0],
                rows=size[1],
                view_id=str(view_id) if view_id is not None else None,
            )
        return next_window

    async def _ensure_runtime_window(
        self,
        window: RuntimeWindow,
        *,
        local_window_id: object | None,
    ) -> RuntimeWindow:
        target = TmuxTarget(
            session=window.session_id,
            window_id=window.window_id,
            cwd=window.cwd,
            shell_command=window.shell_command,
        )
        if await self._tmux_manager.has_window(target):
            return window
        if local_window_id is None:
            raise RuntimeError(f"terminal window is missing: {window.session_id}:{window.window_id}")

        recreated = await self._tmux_manager.recreate_window(
            target,
            local_window_id=local_window_id,
        )
        return RuntimeWindow(
            session_id=recreated.session,
            window_id=recreated.window_id,
            cwd=recreated.cwd,
            shell_command=recreated.shell_command,
        )

    def _session_for(
        self,
        window: RuntimeWindow,
        *,
        view_id: UUID | str | None = None,
    ) -> _LocalTerminalSession:
        key = _attachment_key(window, view_id)
        session = self._sessions.get(key)
        if session is None or session.task is None or session.task.done():
            raise RuntimeError(f"terminal window is not attached: {window.session_id}:{window.window_id}")
        return session

    async def _pipe_output(
        self,
        key: tuple[str, str],
        session: _LocalTerminalSession,
        sender: TerminalSender,
    ) -> None:
        session.output_eof = False
        session.output_event.clear()
        session.output_buffer.clear()
        loop = asyncio.get_running_loop()
        reader_installed = False

        def read_ready() -> None:
            while True:
                try:
                    data = os.read(session.master_fd, PTY_READ_CHUNK_BYTES)
                except BlockingIOError:
                    return
                except OSError:
                    session.output_eof = True
                    session.output_event.set()
                    return
                if not data:
                    session.output_eof = True
                    session.output_event.set()
                    return
                session.output_buffer.extend(data)
                if len(session.output_buffer) > PTY_DRAIN_BUFFER_MAX_BYTES:
                    overflow = len(session.output_buffer) - PTY_DRAIN_BUFFER_MAX_BYTES
                    del session.output_buffer[:overflow]
                    logger.warning(
                        "local terminal PTY output buffer overflowed; dropped oldest bytes",
                        extra={
                            "session_id": key[0],
                            "view_id": key[1],
                            "dropped_bytes": overflow,
                            "buffer_bytes": len(session.output_buffer),
                        },
                    )
                session.output_event.set()

        try:
            os.set_blocking(session.master_fd, False)
            loop.add_reader(session.master_fd, read_ready)
            reader_installed = True
        except (AttributeError, NotImplementedError, OSError):
            with contextlib.suppress(OSError):
                os.set_blocking(session.master_fd, True)
            reader_installed = False

        async def reader_loop() -> None:
            try:
                while True:
                    try:
                        data = await asyncio.to_thread(
                            os.read,
                            session.master_fd,
                            PTY_READ_CHUNK_BYTES,
                        )
                    except OSError:
                        return
                    if not data:
                        return
                    session.output_buffer.extend(data)
                    if len(session.output_buffer) > PTY_DRAIN_BUFFER_MAX_BYTES:
                        overflow = len(session.output_buffer) - PTY_DRAIN_BUFFER_MAX_BYTES
                        del session.output_buffer[:overflow]
                        logger.warning(
                            "local terminal PTY output buffer overflowed; dropped oldest bytes",
                            extra={
                                "session_id": key[0],
                                "view_id": key[1],
                                "dropped_bytes": overflow,
                                "buffer_bytes": len(session.output_buffer),
                            },
                        )
                    session.output_event.set()
            finally:
                session.output_eof = True
                session.output_event.set()

        reader_task = None if reader_installed else asyncio.create_task(reader_loop())
        session.reader_task = reader_task
        try:
            while True:
                if session.output_buffer:
                    chunk = bytes(session.output_buffer)
                    session.output_buffer.clear()
                    try:
                        await sender(chunk)
                    except Exception:
                        logger.exception("terminal output sender failed")
                        return
                    continue
                if session.output_eof:
                    return
                session.output_event.clear()
                if session.output_buffer or session.output_eof:
                    continue
                await session.output_event.wait()
        finally:
            if reader_installed:
                loop.remove_reader(session.master_fd)
            if reader_task is not None and not reader_task.done():
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader_task
            session.reader_task = None
            await self._cleanup_session(key, session)

    async def _watch_active_window(
        self,
        window: RuntimeWindow,
        shadow_session: str,
        selection_callback: TerminalSelectionCallback,
    ) -> None:
        last_window_id = window.window_id
        while True:
            await asyncio.sleep(0.25)
            try:
                active_window_id = await self._tmux_manager.current_window_id(shadow_session)
            except Exception:
                return
            if active_window_id == last_window_id:
                continue
            last_window_id = active_window_id
            await selection_callback(
                RuntimeWindow(session_id=window.session_id, window_id=active_window_id)
            )

    async def _sync_shadow_window_size(
        self,
        window: RuntimeWindow,
        *,
        cols: int,
        rows: int,
        view_id: str | None,
    ) -> None:
        try:
            await self._tmux_manager.resize_shadow_window(
                window,
                cols=cols,
                rows=rows,
                view_id=view_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "failed to resize local shadow tmux window",
                extra={
                    "session_id": window.session_id,
                    "window_id": window.window_id,
                    "view_id": view_id,
                    "cols": cols,
                    "rows": rows,
                },
            )

    async def _cleanup_session(
        self,
        key: tuple[str, str],
        session: _LocalTerminalSession,
    ) -> None:
        async with self._lock:
            if session.cleanup_started:
                return
            session.cleanup_started = True
            current = self._sessions.get(key)
            if current is session:
                self._sessions.pop(key, None)

        with contextlib.suppress(OSError):
            os.close(session.master_fd)
        if session.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                session.process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(session.process.wait(), timeout=2)
            if session.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    session.process.kill()
                await session.process.wait()
        selection_task = session.selection_task
        if selection_task is not None and selection_task is not asyncio.current_task():
            selection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await selection_task
        resize_task = session.resize_task
        if resize_task is not None and resize_task is not asyncio.current_task():
            resize_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await resize_task
        if session.shadow_window_id is not None:
            with contextlib.suppress(Exception):
                await self._tmux_manager.kill_shadow_session(
                    RuntimeWindow(session_id=key[0], window_id=session.shadow_window_id),
                    view_id=session.shadow_view_id,
                )


def _attachment_key(window: RuntimeWindow, view_id: UUID | str | None) -> tuple[str, str]:
    return (window.session_id, str(view_id) if view_id is not None else window.window_id)
