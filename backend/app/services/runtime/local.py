from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pty
from dataclasses import dataclass

from app.services.runtime.types import RuntimeWindow, TerminalSelectionCallback, TerminalSender
from app.services.terminal_bridge import (
    ResizeControl,
    apply_pty_resize,
    attach_process_environment,
    configure_pty_slave,
)
from app.services.tmux_manager import TmuxManager, TmuxTarget, build_attach_command

logger = logging.getLogger(__name__)


@dataclass
class _LocalTerminalSession:
    master_fd: int
    process: asyncio.subprocess.Process
    task: asyncio.Task[None] | None = None
    selection_task: asyncio.Task[None] | None = None
    cleanup_started: bool = False
    size: tuple[int, int] | None = None


class LocalTerminalRuntime:
    def __init__(self, tmux_manager: TmuxManager) -> None:
        self._tmux_manager = tmux_manager
        self._sessions: dict[RuntimeWindow, _LocalTerminalSession] = {}
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
    ) -> None:
        async with self._lock:
            existing = self._sessions.get(window)
            if existing is not None and not existing.task.done():
                return
            self._sessions.pop(window, None)

            attach_target = await self._tmux_manager.ensure_shadow_session(
                TmuxTarget(session=window.session_id, window_id=window.window_id)
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

            session = _LocalTerminalSession(master_fd=master_fd, process=process)
            session.task = asyncio.create_task(self._pipe_output(window, session, sender))
            if selection_callback is not None:
                session.selection_task = asyncio.create_task(
                    self._watch_active_window(window, attach_target.session, selection_callback)
                )
            self._sessions[window] = session

    async def detach(
        self,
        window: RuntimeWindow,
        *,
        local_window_id: object | None = None,
    ) -> None:
        async with self._lock:
            session = self._sessions.get(window)
        if session is None:
            return

        task = session.task
        if task is not None and not task.done():
            task.cancel()
        await self._cleanup_session(window, session)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def send_input(
        self,
        window: RuntimeWindow,
        data: bytes,
        *,
        local_window_id: object | None = None,
    ) -> None:
        session = self._session_for(window)
        await asyncio.to_thread(os.write, session.master_fd, data)

    async def resize(
        self,
        window: RuntimeWindow,
        *,
        cols: int,
        rows: int,
        local_window_id: object | None = None,
    ) -> None:
        session = self._session_for(window)
        size = (cols, rows)
        if session.size == size:
            return
        await asyncio.to_thread(
            apply_pty_resize,
            session.master_fd,
            ResizeControl(cols=cols, rows=rows),
        )
        await self._tmux_manager.resize_shadow_window(window, cols=cols, rows=rows)
        session.size = size

    def _session_for(self, window: RuntimeWindow) -> _LocalTerminalSession:
        session = self._sessions.get(window)
        if session is None or session.task is None or session.task.done():
            raise RuntimeError(f"terminal window is not attached: {window.session_id}:{window.window_id}")
        return session

    async def _pipe_output(
        self,
        window: RuntimeWindow,
        session: _LocalTerminalSession,
        sender: TerminalSender,
    ) -> None:
        try:
            while True:
                try:
                    data = await asyncio.to_thread(os.read, session.master_fd, 4096)
                except OSError:
                    return
                if not data:
                    return
                try:
                    await sender(data)
                except Exception:
                    logger.exception("terminal output sender failed")
        finally:
            await self._cleanup_session(window, session)

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

    async def _cleanup_session(
        self,
        window: RuntimeWindow,
        session: _LocalTerminalSession,
    ) -> None:
        async with self._lock:
            if session.cleanup_started:
                return
            session.cleanup_started = True
            current = self._sessions.get(window)
            if current is session:
                self._sessions.pop(window, None)

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
