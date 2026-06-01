from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol
from uuid import UUID

from app.client_agent.agent_commands import agent_command_for_interactive_shell
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
    local_window_id: UUID | str | None = None


@dataclass(frozen=True)
class TmuxAttachTarget:
    session: str


class TmuxWindowTarget(Protocol):
    window_id: str


def shadow_session_name(window_id: str, view_id: str | None = None) -> str:
    value = view_id or window_id
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    return f"web_terminal_view_{sanitized}"


def build_attach_command(target: TmuxAttachTarget) -> list[str]:
    return ["tmux", "attach-session", "-t", target.session]


def _mountinfo_bind_path_pairs(lines: Iterable[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in lines:
        parts = line.rstrip("\n").split(" ")
        if " - " not in line:
            continue
        separator_index = parts.index("-")
        if separator_index < 5:
            continue
        root = parts[3].replace("\\040", " ").rstrip("/")
        mount_point = parts[4].replace("\\040", " ")
        if root.startswith("/") and root != "/" and mount_point.startswith("/"):
            pairs.append((root, mount_point.rstrip("/")))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def _docker_bind_mount_path_pairs() -> list[tuple[str, str]]:
    if not os.path.exists("/.dockerenv"):
        return []
    with contextlib.suppress(OSError):
        with open("/proc/self/mountinfo", encoding="utf-8") as mountinfo:
            return _mountinfo_bind_path_pairs(mountinfo)
    return []


def _map_host_path_to_container_path(path: str) -> str:
    for source, mount_point in _docker_bind_mount_path_pairs():
        if path == source:
            return mount_point or "/"
        if path.startswith(f"{source}/"):
            suffix = path[len(source) :].lstrip("/")
            return os.path.join(mount_point or "/", suffix)
    return path


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
        launcher_dir: Path | None = None,
        runner: Runner | None = None,
    ) -> None:
        settings = get_settings()
        self.pool_session = pool_session or settings.tmux_pool_session
        self.default_shell = default_shell or settings.default_shell
        self.server_url = server_url or f"http://{settings.app_host}:{settings.app_port}"
        self.launcher_dir = launcher_dir or Path.home() / ".web-terminal-acp" / "launchers"
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
        await self._ensure_terminal_session_options(self.pool_session)
        await self._ensure_clipboard_support()

    async def _ensure_terminal_session_options(self, session_name: str) -> None:
        with contextlib.suppress(TmuxCommandError):
            await self._run(["tmux", "set-option", "-t", session_name, "window-size", "manual"])
        with contextlib.suppress(TmuxCommandError):
            await self._run(["tmux", "set-option", "-t", session_name, "mouse", "on"])

    async def _ensure_pane_passthrough(self, tmux_target: str) -> None:
        with contextlib.suppress(TmuxCommandError):
            await self._run(["tmux", "set-option", "-p", "-t", tmux_target, "allow-passthrough", "on"])

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
        requested_cwd = cwd or os.getcwd()
        effective_cwd = _map_host_path_to_container_path(requested_cwd)
        effective_shell = shell_command or self.default_shell
        interactive_agent_command = (
            agent_command_for_interactive_shell(shell_command)
            if shell_command is not None
            else None
        )
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
        shell = self.default_shell if interactive_agent_command is not None else effective_shell
        if window_id is not None:
            shell = self.managed_shell_launcher_command(
                client_id=client_id,
                window_id=window_id,
                shell=shell,
                project_path=effective_cwd,
            )
        command.append(shell)
        try:
            tmux_window_id = (await self._run(command)).strip()
        except Exception:
            if window_id is not None:
                self._remove_managed_shell_launcher(window_id)
            raise
        await self._ensure_pane_passthrough(f"{self.pool_session}:{tmux_window_id}")
        try:
            await self.select_window(TmuxTarget(session=self.pool_session, window_id=tmux_window_id))
        except TmuxCommandError as exc:
            if not _is_missing_tmux_window_error(exc, tmux_window_id):
                raise
            interactive_agent_command = None
            recovery_shell = self.default_shell
            if window_id is not None:
                recovery_shell = self.managed_shell_launcher_command(
                    client_id=client_id,
                    window_id=window_id,
                    shell=self.default_shell,
                    project_path=effective_cwd,
                )
            tmux_window_id = (
                await self._run(
                    [
                        "tmux",
                        "new-window",
                        "-P",
                        "-F",
                        "#{window_id}",
                        "-t",
                        self.pool_session,
                        "-c",
                        effective_cwd,
                        recovery_shell,
                    ]
                )
            ).strip()
            await self._ensure_pane_passthrough(f"{self.pool_session}:{tmux_window_id}")
            await self.select_window(TmuxTarget(session=self.pool_session, window_id=tmux_window_id))
        if interactive_agent_command is not None:
            await self._send_literal_command(
                f"{self.pool_session}:{tmux_window_id}", interactive_agent_command
            )
        effective_cwd = await self.window_cwd(
            TmuxTarget(session=self.pool_session, window_id=tmux_window_id),
            fallback=effective_cwd,
        )
        return TmuxTarget(
            session=self.pool_session,
            window_id=tmux_window_id,
            cwd=effective_cwd,
            shell_command=effective_shell,
            local_window_id=window_id,
        )

    def managed_shell_launcher_command(
        self,
        *,
        client_id: UUID | str,
        window_id: UUID | str,
        shell: str,
        project_path: str | None = None,
    ) -> str:
        launcher_path = self._write_managed_shell_launcher(
            client_id=client_id,
            window_id=window_id,
            shell=shell,
            project_path=project_path,
        )
        return f"exec {shlex.quote(str(launcher_path))}"

    def _write_managed_shell_launcher(
        self,
        *,
        client_id: UUID | str,
        window_id: UUID | str,
        shell: str,
        project_path: str | None = None,
    ) -> Path:
        self.launcher_dir.mkdir(parents=True, exist_ok=True)
        launcher_path = self._managed_shell_launcher_path(window_id)
        temp_path = launcher_path.with_name(f".{launcher_path.name}.tmp")
        managed_command = build_managed_shell_command(
            shell=shell,
            client_id=client_id,
            window_id=window_id,
            server_url=self.server_url,
            project_path=project_path,
        ).command
        temp_path.write_text(f"#!/bin/sh\n{managed_command}\n", encoding="utf-8")
        temp_path.chmod(0o700)
        temp_path.replace(launcher_path)
        return launcher_path

    def _managed_shell_launcher_path(self, window_id: UUID | str) -> Path:
        safe_window_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(window_id)).strip("._")
        if not safe_window_id:
            safe_window_id = "window"
        return self.launcher_dir / f"{safe_window_id}.sh"

    def _remove_managed_shell_launcher(self, window_id: UUID | str) -> None:
        with contextlib.suppress(OSError):
            self._managed_shell_launcher_path(window_id).unlink()

    async def _send_literal_command(self, tmux_target: str, command: str) -> None:
        await self._run(["tmux", "send-keys", "-l", "-t", tmux_target, "--", command])
        await self._run(["tmux", "send-keys", "-t", tmux_target, "Enter"])

    async def recreate_window(
        self,
        target: TmuxTarget,
        *,
        local_window_id: UUID | str,
    ) -> TmuxTarget:
        return await self.create_window(
            target.cwd,
            target.shell_command,
            client_id=LOCAL_CLIENT_ID,
            window_id=local_window_id,
        )

    async def select_window(self, target: TmuxWindowTarget) -> None:
        session_name = (
            getattr(target, "session", None)
            or getattr(target, "session_id", None)
            or self.pool_session
        )
        await self._run(["tmux", "select-window", "-t", f"{session_name}:{target.window_id}"])

    async def ensure_shadow_session(
        self,
        target: TmuxTarget,
        *,
        view_id: str | None = None,
    ) -> TmuxAttachTarget:
        await self.ensure_pool()
        shadow_session = shadow_session_name(target.window_id, view_id)
        await self._create_session_idempotently(
            shadow_session,
            ["tmux", "new-session", "-d", "-t", target.session, "-s", shadow_session],
        )
        await self._ensure_terminal_session_options(shadow_session)
        await self._run(["tmux", "select-window", "-t", f"{shadow_session}:{target.window_id}"])
        await self._ensure_pane_passthrough(f"{shadow_session}:{target.window_id}")
        return TmuxAttachTarget(session=shadow_session)

    async def kill_shadow_session(
        self,
        target: TmuxWindowTarget,
        *,
        view_id: str | None = None,
    ) -> None:
        with contextlib.suppress(TmuxCommandError):
            await self._run([
                "tmux",
                "kill-session",
                "-t",
                shadow_session_name(target.window_id, view_id),
            ])

    async def resize_shadow_window(
        self,
        target: TmuxWindowTarget,
        *,
        cols: int,
        rows: int,
        view_id: str | None = None,
    ) -> None:
        await self._run([
            "tmux",
            "resize-window",
            "-t",
            f"{shadow_session_name(target.window_id, view_id)}:{target.window_id}",
            "-x",
            str(cols),
            "-y",
            str(rows),
        ])

    async def select_shadow_window(
        self,
        target: TmuxWindowTarget,
        *,
        view_id: str,
    ) -> None:
        await self._run(
            [
                "tmux",
                "select-window",
                "-t",
                f"{shadow_session_name(target.window_id, view_id)}:{target.window_id}",
            ]
        )

    async def current_window_id(self, session_name: str) -> str:
        return (
            await self._run(
                ["tmux", "display-message", "-p", "-t", session_name, "#{window_id}"]
            )
        ).strip()

    async def has_window(self, target: TmuxTarget) -> bool:
        try:
            window_id = (
                await self._run([
                    "tmux",
                    "display-message",
                    "-p",
                    "-t",
                    f"{target.session}:{target.window_id}",
                    "#{window_id}",
                ])
            ).strip()
        except TmuxCommandError:
            return False
        return window_id == target.window_id

    async def window_cwd(self, target: TmuxTarget, *, fallback: str | None = None) -> str | None:
        try:
            cwd = (
                await self._run([
                    "tmux",
                    "display-message",
                    "-p",
                    "-t",
                    f"{target.session}:{target.window_id}",
                    "#{pane_current_path}",
                ])
            ).strip()
        except TmuxCommandError:
            return fallback
        return cwd or fallback

    async def kill_window(self, target: TmuxTarget) -> None:
        if not await self.has_window(target):
            if target.local_window_id is not None:
                self._remove_managed_shell_launcher(target.local_window_id)
            return
        await self._run([
            "tmux",
            "kill-window",
            "-t",
            f"{target.session}:{target.window_id}",
        ])
        if target.local_window_id is not None:
            self._remove_managed_shell_launcher(target.local_window_id)

    async def capture_window(self, target: TmuxTarget) -> str:
        return await self._run([
            "tmux",
            "capture-pane",
            "-p",
            "-t",
            f"{target.session}:{target.window_id}",
        ])


def _is_missing_tmux_window_error(exc: BaseException, window_id: str) -> bool:
    message = str(exc)
    return f"can't find window: {window_id}" in message or re.search(r"can't find window: @\d+", message) is not None
