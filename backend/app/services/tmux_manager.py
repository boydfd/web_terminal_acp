from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol
from uuid import UUID

from app.client_agent.shell_hook import build_managed_shell_command
from app.config import get_settings
from app.models import LOCAL_CLIENT_ID

Runner = Callable[[list[str]], Awaitable[str]]


@dataclass(frozen=True)
class TmuxTarget:
    session: str
    window_id: str
    cwd: str | None = None
    shell_command: str | None = None


@dataclass(frozen=True)
class TmuxAttachTarget:
    session: str


class TmuxWindowTarget(Protocol):
    window_id: str


def shadow_session_name(window_id: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", window_id)
    return f"web_terminal_view_{sanitized}"


def build_attach_command(target: TmuxAttachTarget) -> list[str]:
    return ["tmux", "attach-session", "-t", target.session]


def get_tmux_manager() -> "TmuxManager":
    return TmuxManager()


class TmuxCommandError(RuntimeError):
    def __init__(self, args: list[str], returncode: int, stderr: str):
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"tmux command failed ({returncode}): {' '.join(args)}: {stderr.strip()}")


class TmuxManager:
    _session_locks: ClassVar[dict[str, asyncio.Lock]] = {}

    def __init__(
        self,
        pool_session: str | None = None,
        default_shell: str | None = None,
        server_url: str | None = None,
        runner: Runner | None = None,
    ) -> None:
        settings = get_settings()
        self.pool_session = pool_session or settings.tmux_pool_session
        self.default_shell = default_shell or settings.default_shell
        self.server_url = server_url or f"http://{settings.app_host}:{settings.app_port}"
        self._runner = runner
        self._clipboard_configured = False
        self._clipboard_lock = asyncio.Lock()

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
            raise TmuxCommandError(args, process.returncode, stderr.decode(errors="replace"))
        return stdout.decode(errors="replace")

    @classmethod
    def _lock_for_session(cls, session_name: str) -> asyncio.Lock:
        lock = cls._session_locks.get(session_name)
        if lock is None:
            lock = asyncio.Lock()
            cls._session_locks[session_name] = lock
        return lock

    async def _has_session(self, session_name: str) -> bool:
        try:
            await self._run(["tmux", "has-session", "-t", session_name])
        except TmuxCommandError:
            return False
        return True

    async def _create_session_idempotently(
        self, session_name: str, command: list[str]
    ) -> None:
        if await self._has_session(session_name):
            return

        async with self._lock_for_session(session_name):
            if await self._has_session(session_name):
                return
            try:
                await self._run(command)
            except TmuxCommandError:
                if await self._has_session(session_name):
                    return
                raise

    async def ensure_pool(self) -> None:
        await self._create_session_idempotently(
            self.pool_session,
            ["tmux", "new-session", "-d", "-s", self.pool_session, self.default_shell],
        )
        await self._ensure_manual_window_size(self.pool_session)
        await self._ensure_clipboard_support()

    async def _ensure_manual_window_size(self, session_name: str) -> None:
        with contextlib.suppress(TmuxCommandError):
            await self._run(["tmux", "set-option", "-t", session_name, "window-size", "manual"])

    async def _ensure_clipboard_support(self) -> None:
        if self._clipboard_configured:
            return

        async with self._clipboard_lock:
            if self._clipboard_configured:
                return

            with contextlib.suppress(TmuxCommandError):
                await self._run(["tmux", "set-option", "-s", "set-clipboard", "external"])
            terminal_features = ""
            with contextlib.suppress(TmuxCommandError):
                terminal_features = await self._run(["tmux", "show-options", "-s", "terminal-features"])
            if "clipboard" not in terminal_features:
                with contextlib.suppress(TmuxCommandError):
                    await self._run(
                        ["tmux", "set-option", "-as", "terminal-features", ",xterm*:clipboard"]
                    )
            self._clipboard_configured = True

    async def create_window(
        self,
        cwd: str | None,
        shell_command: str | None,
        *,
        client_id: UUID | str = LOCAL_CLIENT_ID,
        window_id: UUID | str | None = None,
    ) -> TmuxTarget:
        await self.ensure_pool()
        effective_cwd = cwd or os.getcwd()
        effective_shell = shell_command or self.default_shell
        command = [
            "tmux",
            "new-window",
            "-P",
            "-F",
            "#{window_id}",
            "-t",
            self.pool_session,
        ]
        command.extend(["-c", effective_cwd])
        shell = effective_shell
        if window_id is not None:
            shell = build_managed_shell_command(
                shell=effective_shell,
                client_id=client_id,
                window_id=window_id,
                server_url=self.server_url,
                project_path=effective_cwd,
            ).command
        command.append(shell)
        tmux_window_id = (await self._run(command)).strip()
        return TmuxTarget(
            session=self.pool_session,
            window_id=tmux_window_id,
            cwd=effective_cwd,
            shell_command=effective_shell,
        )

    async def ensure_shadow_session(self, target: TmuxTarget) -> TmuxAttachTarget:
        await self.ensure_pool()
        shadow_session = shadow_session_name(target.window_id)
        await self._create_session_idempotently(
            shadow_session,
            ["tmux", "new-session", "-d", "-t", target.session, "-s", shadow_session],
        )
        await self._ensure_manual_window_size(shadow_session)
        await self._run(["tmux", "select-window", "-t", f"{shadow_session}:{target.window_id}"])
        return TmuxAttachTarget(session=shadow_session)

    async def resize_shadow_window(self, target: TmuxWindowTarget, *, cols: int, rows: int) -> None:
        await self._run([
            "tmux",
            "resize-window",
            "-t",
            f"{shadow_session_name(target.window_id)}:{target.window_id}",
            "-x",
            str(cols),
            "-y",
            str(rows),
        ])

    async def current_window_id(self, session_name: str) -> str:
        return (
            await self._run(
                ["tmux", "display-message", "-p", "-t", session_name, "#{window_id}"]
            )
        ).strip()

    async def has_window(self, target: TmuxTarget) -> bool:
        try:
            await self._run([
                "tmux",
                "display-message",
                "-p",
                "-t",
                f"{target.session}:{target.window_id}",
                "#{window_id}",
            ])
        except TmuxCommandError:
            return False
        return True

    async def kill_window(self, target: TmuxTarget) -> None:
        await self._run(["tmux", "kill-window", "-t", f"{target.session}:{target.window_id}"])

    async def capture_window(self, target: TmuxTarget) -> str:
        return await self._run([
            "tmux",
            "capture-pane",
            "-p",
            "-t",
            f"{target.session}:{target.window_id}",
        ])
