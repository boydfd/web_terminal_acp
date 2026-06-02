from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import re
import select
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from uuid import UUID

from app.client_agent.agent_commands import agent_command_for_interactive_shell
from app.client_agent.config import default_user_shell
from app.client_agent.terminal import (
    PTY_READ_CHUNK_BYTES,
    _apply_pty_resize,
    _attach_process_environment,
    _configure_pty_slave,
)

Runner = Callable[[list[str]], Awaitable[str]]
TerminalSender = Callable[[bytes], Awaitable[None]]

PTY_FAST_INPUT_MAX_BYTES = 256
PTY_CONTROL_EXECUTOR = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="web-terminal-client-aux-pty-control",
)


@dataclass(frozen=True)
class AuxTerminalTarget:
    aux_terminal_id: str
    session_id: str
    cwd: str
    shell_command: str


@dataclass
class AttachedAuxTerminal:
    master_fd: int
    process: asyncio.subprocess.Process
    task: asyncio.Task[None] | None = None
    size: tuple[int, int] | None = None


class ClientAuxTerminalManager:
    def __init__(self, *, default_shell: str | None = None, runner: Runner | None = None) -> None:
        self._default_shell = default_shell or default_user_shell()
        self._runner = runner
        self._targets: dict[str, AuxTerminalTarget] = {}
        self._attached: dict[str, AttachedAuxTerminal] = {}
        self._lock = asyncio.Lock()

    async def ensure_terminal(
        self,
        aux_terminal_id: str,
        *,
        cwd: str | None = None,
        shell_command: str | None = None,
    ) -> AuxTerminalTarget:
        safe_id = _safe_identifier(aux_terminal_id)
        session_id = f"web_terminal_aux_{safe_id}"
        effective_cwd = cwd or os.getcwd()
        effective_shell = shell_command or self._default_shell
        async with self._lock:
            existing = self._targets.get(aux_terminal_id)
            if existing is not None and await self._has_session(existing.session_id):
                return existing
            if not await self._has_session(session_id):
                session_shell = (
                    self._default_shell
                    if agent_command_for_interactive_shell(effective_shell) is not None
                    else effective_shell
                )
                try:
                    await self._run([
                        "tmux",
                        "new-session",
                        "-d",
                        "-s",
                        session_id,
                        "-c",
                        effective_cwd,
                        session_shell,
                    ])
                except RuntimeError:
                    if not await self._has_session(session_id):
                        raise
            await self._ensure_session_options(session_id)
            target = AuxTerminalTarget(
                aux_terminal_id=aux_terminal_id,
                session_id=session_id,
                cwd=effective_cwd,
                shell_command=effective_shell,
            )
            self._targets[aux_terminal_id] = target
            return target

    async def attach(
        self,
        aux_terminal_id: str,
        sender: TerminalSender,
        *,
        view_id: UUID | str,
    ) -> None:
        key = _attachment_key(aux_terminal_id, view_id)
        target = self._target_for(aux_terminal_id)
        async with self._lock:
            existing = self._attached.get(key)
            if existing is not None and existing.task is not None and not existing.task.done():
                return
            self._attached.pop(key, None)
            master_fd, slave_fd = pty.openpty()
            try:
                try:
                    _configure_pty_slave(slave_fd)
                    process = await asyncio.create_subprocess_exec(
                        "tmux",
                        "attach-session",
                        "-t",
                        target.session_id,
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
            attached = AttachedAuxTerminal(master_fd=master_fd, process=process)
            attached.task = asyncio.create_task(self._pipe_output(key, attached, sender))
            self._attached[key] = attached

    async def detach(self, aux_terminal_id: str, *, view_id: UUID | str) -> None:
        key = _attachment_key(aux_terminal_id, view_id)
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

    async def kill(self, aux_terminal_id: str) -> None:
        target = self._targets.pop(aux_terminal_id, None)
        for key in tuple(self._attached):
            if key.startswith(f"{aux_terminal_id}:"):
                _aux_terminal_id, view_id = key.split(":", 1)
                await self.detach(aux_terminal_id, view_id=view_id)
        session_id = target.session_id if target is not None else f"web_terminal_aux_{_safe_identifier(aux_terminal_id)}"
        with contextlib.suppress(RuntimeError):
            await self._run(["tmux", "kill-session", "-t", session_id])

    async def send_input(self, aux_terminal_id: str, data: bytes, *, view_id: UUID | str) -> None:
        attached = self._attached_for(aux_terminal_id, view_id=view_id)
        written = _try_write_pty_input_immediately(attached.master_fd, data)
        if written >= len(data):
            return
        await _run_pty_control(_write_all_pty_input, attached.master_fd, data[written:])

    async def resize(
        self,
        aux_terminal_id: str,
        *,
        cols: int,
        rows: int,
        view_id: UUID | str,
    ) -> None:
        attached = self._attached_for(aux_terminal_id, view_id=view_id)
        size = (cols, rows)
        if attached.size == size:
            return
        await _run_pty_control(_apply_pty_resize, attached.master_fd, cols=cols, rows=rows)
        attached.size = size

    async def close(self) -> None:
        for aux_terminal_id in tuple(self._targets):
            await self.kill(aux_terminal_id)
        for key in tuple(self._attached):
            aux_terminal_id, view_id = key.split(":", 1)
            await self.detach(aux_terminal_id, view_id=view_id)

    async def _pipe_output(
        self,
        key: str,
        attached: AttachedAuxTerminal,
        sender: TerminalSender,
    ) -> None:
        try:
            while True:
                try:
                    data = await asyncio.to_thread(os.read, attached.master_fd, PTY_READ_CHUNK_BYTES)
                except OSError:
                    return
                if not data:
                    return
                await sender(data)
        finally:
            await self._cleanup_attachment(key, attached)

    async def _cleanup_attachment(self, key: str, attached: AttachedAuxTerminal) -> None:
        async with self._lock:
            if self._attached.get(key) is attached:
                self._attached.pop(key, None)
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

    async def _has_session(self, session_id: str) -> bool:
        try:
            await self._run(["tmux", "has-session", "-t", session_id])
        except RuntimeError:
            return False
        return True

    async def _ensure_session_options(self, session_id: str) -> None:
        with contextlib.suppress(RuntimeError):
            await self._run(["tmux", "set-option", "-t", session_id, "window-size", "manual"])
        with contextlib.suppress(RuntimeError):
            await self._run(["tmux", "set-option", "-t", session_id, "mouse", "on"])

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

    def _target_for(self, aux_terminal_id: str) -> AuxTerminalTarget:
        target = self._targets.get(aux_terminal_id)
        if target is None:
            raise RuntimeError(f"aux terminal is not registered: {aux_terminal_id}")
        return target

    def _attached_for(self, aux_terminal_id: str, *, view_id: UUID | str) -> AttachedAuxTerminal:
        key = _attachment_key(aux_terminal_id, view_id)
        attached = self._attached.get(key)
        if attached is None or attached.task is None or attached.task.done():
            raise RuntimeError(f"aux terminal is not attached: {aux_terminal_id}")
        return attached


async def _run_pty_control(func, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(PTY_CONTROL_EXECUTOR, partial(func, *args, **kwargs))


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


def _attachment_key(aux_terminal_id: str, view_id: UUID | str) -> str:
    return f"{aux_terminal_id}:{view_id}"


def _safe_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)
