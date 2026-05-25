from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import struct
import termios
import tty
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app.services.tmux_manager import TmuxAttachTarget, build_attach_command

logger = logging.getLogger(__name__)
TerminalOutputCallback = Callable[[bytes], Awaitable[None]]


@dataclass(frozen=True)
class ResizeControl:
    cols: int
    rows: int


def parse_text_input(text: str) -> bytes | ResizeControl | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text.encode()

    if not isinstance(payload, dict):
        return text.encode()
    if payload.get("type") != "resize":
        return None

    cols = payload.get("cols")
    rows = payload.get("rows")
    if not isinstance(cols, int) or not isinstance(rows, int) or cols <= 0 or rows <= 0:
        return None
    return ResizeControl(cols=cols, rows=rows)


def apply_pty_resize(master_fd: int, resize: ResizeControl) -> None:
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", resize.rows, resize.cols, 0, 0))


def configure_pty_slave(slave_fd: int) -> None:
    tty.setraw(slave_fd, termios.TCSANOW)


def attach_process_environment() -> dict[str, str]:
    return {**os.environ, "TERM": os.environ.get("TERM") or "xterm-256color"}


def _is_expected_websocket_close_error(exc: BaseException) -> bool:
    if isinstance(exc, WebSocketDisconnect | OSError):
        return True
    message = str(exc).lower()
    return "disconnect" in message or "close" in message or "websocket" in message


class TerminalBridge:
    def __init__(
        self,
        websocket: WebSocket,
        target: TmuxAttachTarget,
        output_callback: TerminalOutputCallback | None = None,
    ) -> None:
        self.websocket = websocket
        self.target = target
        self.output_callback = output_callback

    async def run(self) -> None:
        await self.websocket.accept()
        master_fd, slave_fd = pty.openpty()
        process: asyncio.subprocess.Process | None = None
        tasks: set[asyncio.Task[object]] = set()
        try:
            configure_pty_slave(slave_fd)
            process = await asyncio.create_subprocess_exec(
                *build_attach_command(self.target),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                env=attach_process_environment(),
            )
            os.close(slave_fd)
            slave_fd = -1

            pty_to_websocket = asyncio.create_task(self._pipe_pty_to_websocket(master_fd))
            websocket_to_pty = asyncio.create_task(self._pipe_websocket_to_pty(master_fd))
            process_wait = asyncio.create_task(process.wait())
            tasks = {pty_to_websocket, websocket_to_pty, process_wait}

            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not process_wait:
                    task.result()
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            tasks.clear()
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if slave_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(slave_fd)
            with contextlib.suppress(OSError):
                os.close(master_fd)
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=2)
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()

    async def _pipe_pty_to_websocket(self, master_fd: int) -> None:
        while True:
            try:
                data = await asyncio.to_thread(os.read, master_fd, 4096)
            except OSError:
                return
            if not data:
                return
            if self.output_callback is not None:
                try:
                    await self.output_callback(data)
                except Exception:
                    logger.exception("terminal output callback failed")
            try:
                await self.websocket.send_bytes(data)
            except (WebSocketDisconnect, RuntimeError, OSError) as exc:
                if _is_expected_websocket_close_error(exc):
                    return
                raise

    async def _pipe_websocket_to_pty(self, master_fd: int) -> None:
        while True:
            try:
                message = await self.websocket.receive()
            except (WebSocketDisconnect, RuntimeError, OSError) as exc:
                if _is_expected_websocket_close_error(exc):
                    return
                raise
            if message.get("type") == "websocket.disconnect":
                return
            try:
                if message.get("bytes") is not None:
                    await asyncio.to_thread(os.write, master_fd, message["bytes"])
                elif message.get("text") is not None:
                    action = parse_text_input(message["text"])
                    if isinstance(action, ResizeControl):
                        apply_pty_resize(master_fd, action)
                    elif isinstance(action, bytes):
                        await asyncio.to_thread(os.write, master_fd, action)
            except OSError:
                return
