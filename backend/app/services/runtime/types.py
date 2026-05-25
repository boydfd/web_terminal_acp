from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

TerminalSender = Callable[[bytes], Awaitable[None]]


@dataclass(frozen=True)
class RuntimeWindow:
    session_id: str
    window_id: str
    cwd: str | None = None
    shell_command: str | None = None


TerminalSelectionCallback = Callable[[RuntimeWindow], Awaitable[None]]


class TerminalRuntime(Protocol):
    async def create_window(
        self,
        cwd: str | None = None,
        shell_command: str | None = None,
        *,
        window_id: object | None = None,
    ) -> RuntimeWindow:
        """Create a terminal window in this runtime."""

    async def attach(
        self,
        window: RuntimeWindow,
        sender: TerminalSender,
        *,
        local_window_id: object | None = None,
        selection_callback: TerminalSelectionCallback | None = None,
    ) -> None:
        """Attach runtime output for a window to a sender."""

    async def detach(
        self,
        window: RuntimeWindow,
        *,
        local_window_id: object | None = None,
    ) -> None:
        """Detach runtime output and clean up resources for a window."""

    async def send_input(
        self,
        window: RuntimeWindow,
        data: bytes,
        *,
        local_window_id: object | None = None,
    ) -> None:
        """Send terminal input bytes to a runtime window."""

    async def resize(
        self,
        window: RuntimeWindow,
        *,
        cols: int,
        rows: int,
        local_window_id: object | None = None,
    ) -> None:
        """Resize a runtime window."""
