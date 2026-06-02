from __future__ import annotations

import asyncio
import contextlib
import logging
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
from app.config import get_settings
from app.models import VirtualWindow
from app.services.runtime.client_connections import (
    ClientConnectionClosed,
    ClientConnectionRegistry,
)
from app.services.runtime.protocol import AgentMessage
from app.services.terminal_bridge import (
    ResizeControl,
    apply_pty_resize,
    attach_process_environment,
    configure_pty_slave,
)
from app.services.tmux_manager import TmuxCommandError, get_tmux_manager

logger = logging.getLogger(__name__)

AuxTerminalSender = Callable[[bytes], Awaitable[None]]

PTY_READ_CHUNK_BYTES = 65536
PTY_FAST_INPUT_MAX_BYTES = 256
PTY_CONTROL_EXECUTOR_MAX_WORKERS = 8
PTY_CONTROL_EXECUTOR = ThreadPoolExecutor(
    max_workers=PTY_CONTROL_EXECUTOR_MAX_WORKERS,
    thread_name_prefix="web-terminal-aux-pty-control",
)


@dataclass(frozen=True)
class AuxTerminalRuntime:
    session_id: str
    cwd: str
    shell_command: str | None


@dataclass
class LocalAuxTerminalAttachment:
    master_fd: int
    process: asyncio.subprocess.Process
    output_task: asyncio.Task[None] | None = None
    size: tuple[int, int] | None = None


class AuxTerminalUnavailable(RuntimeError):
    pass


class AuxTerminalRegistry:
    def __init__(self) -> None:
        self._local: dict[tuple[UUID, UUID], AuxTerminalRuntime] = {}
        self._lock = asyncio.Lock()

    async def local_runtime(
        self,
        client_id: UUID,
        parent_window_id: UUID,
        *,
        cwd: str | None,
        shell_command: str | None,
    ) -> AuxTerminalRuntime:
        key = (client_id, parent_window_id)
        async with self._lock:
            existing = self._local.get(key)
            if existing is not None and await _has_tmux_session(existing.session_id):
                return existing
            runtime = await _create_local_aux_terminal(
                parent_window_id,
                cwd=cwd,
                shell_command=shell_command,
            )
            self._local[key] = runtime
            return runtime

    async def remove(self, client_id: UUID, parent_window_id: UUID) -> None:
        key = (client_id, parent_window_id)
        async with self._lock:
            runtime = self._local.pop(key, None)
        await _kill_tmux_session(
            runtime.session_id if runtime is not None else aux_terminal_session_name(parent_window_id)
        )


async def attach_local_aux_terminal(
    runtime: AuxTerminalRuntime,
    sender: AuxTerminalSender,
) -> LocalAuxTerminalAttachment:
    master_fd, slave_fd = pty.openpty()
    try:
        try:
            configure_pty_slave(slave_fd)
            process = await asyncio.create_subprocess_exec(
                "tmux",
                "attach-session",
                "-t",
                runtime.session_id,
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

    attachment = LocalAuxTerminalAttachment(master_fd=master_fd, process=process)
    attachment.output_task = asyncio.create_task(_pipe_local_aux_output(attachment, sender))
    return attachment


async def detach_local_aux_terminal(attachment: LocalAuxTerminalAttachment) -> None:
    task = attachment.output_task
    if task is not None and not task.done():
        task.cancel()
    with contextlib.suppress(OSError):
        os.close(attachment.master_fd)
    if task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    process = attachment.process
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2)
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def send_local_aux_input(attachment: LocalAuxTerminalAttachment, data: bytes) -> None:
    written = _try_write_pty_input_immediately(attachment.master_fd, data)
    if written >= len(data):
        return
    await _run_pty_control(_write_all_pty_input, attachment.master_fd, data[written:])


async def resize_local_aux_terminal(
    attachment: LocalAuxTerminalAttachment,
    resize: ResizeControl,
) -> None:
    size = (resize.cols, resize.rows)
    if attachment.size == size:
        return
    await _run_pty_control(apply_pty_resize, attachment.master_fd, resize)
    attachment.size = size


async def ensure_remote_aux_terminal(
    *,
    client_id: UUID,
    parent_window_id: UUID,
    cwd: str | None,
    shell_command: str | None,
    registry: ClientConnectionRegistry,
    timeout: float = 10.0,
) -> str:
    connection = registry.get(client_id)
    if connection is None or getattr(connection, "closed", False):
        raise AuxTerminalUnavailable(f"remote client unavailable: {client_id}")
    remote_id = aux_terminal_remote_id(parent_window_id)
    try:
        response = await connection.request(
            AgentMessage(
                type="aux_terminal_ensure",
                client_id=client_id,
                window_id=parent_window_id,
                request_id=_request_id(),
                payload={
                    "aux_terminal_id": remote_id,
                    "cwd": cwd,
                    "shell_command": shell_command,
                },
            ),
            timeout=timeout,
        )
    except (ClientConnectionClosed, asyncio.TimeoutError) as exc:
        raise AuxTerminalUnavailable(f"remote aux terminal unavailable: {client_id}") from exc
    if response.type == "terminal_error":
        message = response.payload.get("message")
        raise AuxTerminalUnavailable(str(message) if isinstance(message, str) else "remote aux terminal error")
    return remote_id


async def attach_remote_aux_terminal(
    *,
    client_id: UUID,
    parent_window_id: UUID,
    aux_terminal_id: str,
    view_id: UUID,
    registry: ClientConnectionRegistry,
    timeout: float = 10.0,
) -> None:
    connection = registry.get(client_id)
    if connection is None or getattr(connection, "closed", False):
        raise AuxTerminalUnavailable(f"remote client unavailable: {client_id}")
    try:
        response = await connection.request(
            AgentMessage(
                type="aux_terminal_attach",
                client_id=client_id,
                window_id=parent_window_id,
                request_id=_request_id(),
                payload={"aux_terminal_id": aux_terminal_id, "view_id": str(view_id)},
            ),
            timeout=timeout,
        )
    except (ClientConnectionClosed, asyncio.TimeoutError) as exc:
        raise AuxTerminalUnavailable(f"remote aux terminal unavailable: {client_id}") from exc
    if response.type == "terminal_error":
        message = response.payload.get("message")
        raise AuxTerminalUnavailable(str(message) if isinstance(message, str) else "remote aux terminal error")


async def detach_remote_aux_terminal(
    *,
    client_id: UUID,
    parent_window_id: UUID,
    aux_terminal_id: str,
    view_id: UUID,
    registry: ClientConnectionRegistry,
) -> None:
    connection = registry.get(client_id)
    if connection is None or getattr(connection, "closed", False):
        return
    with contextlib.suppress(ClientConnectionClosed):
        await connection.send(
            AgentMessage(
                type="aux_terminal_detach",
                client_id=client_id,
                window_id=parent_window_id,
                payload={"aux_terminal_id": aux_terminal_id, "view_id": str(view_id)},
            )
        )


async def kill_remote_aux_terminal(
    *,
    client_id: UUID,
    parent_window_id: UUID,
    registry: ClientConnectionRegistry,
) -> None:
    connection = registry.get(client_id)
    if connection is None or getattr(connection, "closed", False):
        return
    with contextlib.suppress(ClientConnectionClosed):
        await connection.send(
            AgentMessage(
                type="aux_terminal_kill",
                client_id=client_id,
                window_id=parent_window_id,
                payload={"aux_terminal_id": aux_terminal_remote_id(parent_window_id)},
            )
        )


async def send_remote_aux_input(
    *,
    client_id: UUID,
    parent_window_id: UUID,
    aux_terminal_id: str,
    view_id: UUID,
    data: bytes,
    registry: ClientConnectionRegistry,
) -> None:
    connection = registry.get(client_id)
    if connection is None or getattr(connection, "closed", False):
        raise AuxTerminalUnavailable(f"remote client unavailable: {client_id}")
    try:
        await connection.send(
            AgentMessage(
                type="aux_terminal_input",
                client_id=client_id,
                window_id=parent_window_id,
                payload={
                    "aux_terminal_id": aux_terminal_id,
                    "view_id": str(view_id),
                    "data": data.hex(),
                },
            )
        )
    except ClientConnectionClosed as exc:
        raise AuxTerminalUnavailable(f"remote client unavailable: {client_id}") from exc


async def resize_remote_aux_terminal(
    *,
    client_id: UUID,
    parent_window_id: UUID,
    aux_terminal_id: str,
    view_id: UUID,
    resize: ResizeControl,
    registry: ClientConnectionRegistry,
) -> None:
    connection = registry.get(client_id)
    if connection is None or getattr(connection, "closed", False):
        raise AuxTerminalUnavailable(f"remote client unavailable: {client_id}")
    try:
        await connection.send(
            AgentMessage(
                type="aux_terminal_resize",
                client_id=client_id,
                window_id=parent_window_id,
                payload={
                    "aux_terminal_id": aux_terminal_id,
                    "view_id": str(view_id),
                    "cols": resize.cols,
                    "rows": resize.rows,
                },
            )
        )
    except ClientConnectionClosed as exc:
        raise AuxTerminalUnavailable(f"remote client unavailable: {client_id}") from exc


def aux_terminal_registry_from_state(state) -> AuxTerminalRegistry:
    registry = getattr(state, "aux_terminal_registry", None)
    if registry is None:
        registry = AuxTerminalRegistry()
        state.aux_terminal_registry = registry
    return registry


def aux_terminal_session_name(parent_window_id: UUID) -> str:
    return f"web_terminal_aux_{_safe_identifier(str(parent_window_id))}"


def aux_terminal_remote_id(parent_window_id: UUID) -> str:
    return f"aux-{parent_window_id}"


def cwd_for_aux_terminal(window: VirtualWindow) -> str | None:
    return window.cwd


def shell_for_aux_terminal(window: VirtualWindow) -> str | None:
    return window.shell_command


async def _run_tmux(args: list[str]) -> str:
    manager = get_tmux_manager()
    return await manager._run(args)  # Reuse existing tmux error handling.


async def _has_tmux_session(session_name: str) -> bool:
    try:
        await _run_tmux(["tmux", "has-session", "-t", session_name])
    except TmuxCommandError:
        return False
    return True


async def _create_local_aux_terminal(
    parent_window_id: UUID,
    *,
    cwd: str | None,
    shell_command: str | None,
) -> AuxTerminalRuntime:
    session_name = aux_terminal_session_name(parent_window_id)
    effective_cwd = cwd or os.getcwd()
    if not await _has_tmux_session(session_name):
        effective_shell = shell_command or get_settings().default_shell
        session_shell = (
            get_settings().default_shell
            if agent_command_for_interactive_shell(effective_shell) is not None
            else effective_shell
        )
        try:
            await _run_tmux(["tmux", "new-session", "-d", "-s", session_name, "-c", effective_cwd, session_shell])
        except TmuxCommandError:
            if not await _has_tmux_session(session_name):
                raise
    await _ensure_terminal_session_options(session_name)
    return AuxTerminalRuntime(session_id=session_name, cwd=effective_cwd, shell_command=shell_command)


async def _ensure_terminal_session_options(session_name: str) -> None:
    with contextlib.suppress(TmuxCommandError):
        await _run_tmux(["tmux", "set-option", "-t", session_name, "window-size", "manual"])
    with contextlib.suppress(TmuxCommandError):
        await _run_tmux(["tmux", "set-option", "-t", session_name, "mouse", "on"])


async def _kill_tmux_session(session_name: str) -> None:
    with contextlib.suppress(TmuxCommandError):
        await _run_tmux(["tmux", "kill-session", "-t", session_name])


async def _pipe_local_aux_output(
    attachment: LocalAuxTerminalAttachment,
    sender: AuxTerminalSender,
) -> None:
    try:
        while True:
            try:
                data = await asyncio.to_thread(os.read, attachment.master_fd, PTY_READ_CHUNK_BYTES)
            except OSError:
                return
            if not data:
                return
            await sender(data)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("local aux terminal output failed")


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


def _request_id() -> str:
    from uuid import uuid4

    return str(uuid4())


def _safe_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)
