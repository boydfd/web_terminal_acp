from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.client_agent.agent_commands import agent_command_for_interactive_shell
from app.client_agent.config import default_user_shell
from app.client_agent.shell_hook import build_managed_shell_command

Runner = Callable[[list[str]], Awaitable[str]]

_WINDOW_ID_OPTION = "@web-terminal-window-id"
_MANAGED_AGENT_TOOLS_OPTION = "@web-terminal-managed-agent-tools"

@dataclass(frozen=True)
class ClientRuntimeWindow:
    remote_session_id: str
    remote_window_id: str
    local_window_id: UUID | None = None
    cwd: str | None = None
    shell_command: str | None = None
    managed_agent_tools: bool = False


class ClientTmuxRuntime:
    def __init__(
        self,
        *,
        client_id: UUID | str,
        server_url: str,
        pool_session: str,
        default_shell: str | None = None,
        launcher_dir: Path | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.client_id = str(client_id)
        self.server_url = server_url
        self.pool_session = pool_session
        self.default_shell = default_shell or default_user_shell()
        self.launcher_dir = launcher_dir or Path.home() / ".web-terminal-acp" / "launchers"
        self._runner = runner
        self._clipboard_configured = False
        self._pool_lock = asyncio.Lock()
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
            error_text = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"tmux command failed ({process.returncode}): {' '.join(args)}: {error_text}")
        return stdout.decode(errors="replace")

    async def ensure_pool(self) -> None:
        async with self._pool_lock:
            try:
                await self._run(["tmux", "has-session", "-t", self.pool_session])
            except RuntimeError:
                try:
                    await self._run(["tmux", "new-session", "-d", "-s", self.pool_session, self.default_shell])
                except RuntimeError:
                    await self._run(["tmux", "has-session", "-t", self.pool_session])
            await self._ensure_terminal_session_options(self.pool_session)
            await self._ensure_clipboard_support()

    async def _ensure_terminal_session_options(self, session_name: str) -> None:
        with contextlib.suppress(RuntimeError):
            await self._run(["tmux", "set-option", "-t", session_name, "window-size", "manual"])
        with contextlib.suppress(RuntimeError):
            await self._run(["tmux", "set-option", "-t", session_name, "mouse", "on"])

    async def _ensure_pane_passthrough(self, tmux_target: str) -> None:
        with contextlib.suppress(RuntimeError):
            await self._run(["tmux", "set-option", "-p", "-t", tmux_target, "allow-passthrough", "on"])

    async def _ensure_clipboard_support(self) -> None:
        if self._clipboard_configured:
            return

        async with self._clipboard_lock:
            if self._clipboard_configured:
                return

            with contextlib.suppress(RuntimeError):
                await self._run(["tmux", "set-option", "-s", "set-clipboard", "external"])
            terminal_features = ""
            with contextlib.suppress(RuntimeError):
                terminal_features = await self._run(["tmux", "show-options", "-s", "terminal-features"])
            if "clipboard" not in terminal_features:
                with contextlib.suppress(RuntimeError):
                    await self._run(
                        ["tmux", "set-option", "-as", "terminal-features", ",xterm*:clipboard"]
                    )
            self._clipboard_configured = True

    def managed_shell_command(
        self,
        window_id: UUID | str,
        shell_command: str | None = None,
        project_path: str | None = None,
    ) -> str:
        return build_managed_shell_command(
            shell=shell_command or self.default_shell,
            client_id=self.client_id,
            window_id=window_id,
            server_url=self.server_url,
            project_path=project_path,
        ).command

    def managed_shell_launcher_command(
        self,
        window_id: UUID | str,
        shell_command: str | None = None,
        project_path: str | None = None,
    ) -> str:
        launcher_path = self._write_managed_shell_launcher(
            window_id,
            shell_command=shell_command,
            project_path=project_path,
        )
        return f"exec {shlex.quote(str(launcher_path))}"

    def _write_managed_shell_launcher(
        self,
        window_id: UUID | str,
        *,
        shell_command: str | None = None,
        project_path: str | None = None,
    ) -> Path:
        local_window_id = UUID(str(window_id))
        self.launcher_dir.mkdir(parents=True, exist_ok=True)
        launcher_path = self.launcher_dir / f"{local_window_id}.sh"
        temp_path = self.launcher_dir / f".{local_window_id}.sh.tmp"
        managed_command = self.managed_shell_command(
            local_window_id,
            shell_command=shell_command,
            project_path=project_path,
        )
        temp_path.write_text(f"#!/bin/sh\n{managed_command}\n", encoding="utf-8")
        temp_path.chmod(0o700)
        temp_path.replace(launcher_path)
        return launcher_path

    def _remove_managed_shell_launcher(self, window_id: UUID | str) -> None:
        with contextlib.suppress(OSError, ValueError):
            (self.launcher_dir / f"{UUID(str(window_id))}.sh").unlink()

    async def create_window(
        self,
        window_id: UUID | str,
        cwd: str | None = None,
        shell_command: str | None = None,
    ) -> ClientRuntimeWindow:
        local_window_id = UUID(str(window_id))
        effective_cwd = cwd or os.getcwd()
        effective_shell = shell_command or self.default_shell
        interactive_agent_command = (
            agent_command_for_interactive_shell(shell_command)
            if shell_command is not None
            else None
        )
        window_shell = self.default_shell if interactive_agent_command is not None else effective_shell
        await self.ensure_pool()
        launcher_command = self.managed_shell_launcher_command(
            local_window_id,
            window_shell,
            project_path=effective_cwd,
        )
        remote_window_id = (
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
                    launcher_command,
                ]
            )
        ).strip()
        await self._ensure_pane_passthrough(f"{self.pool_session}:{remote_window_id}")
        try:
            await self.select_window(remote_window_id)
        except RuntimeError as exc:
            if not _is_missing_tmux_window_error(exc, remote_window_id):
                raise
            interactive_agent_command = None
            launcher_command = self.managed_shell_launcher_command(
                local_window_id,
                self.default_shell,
                project_path=effective_cwd,
            )
            remote_window_id = (
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
                        launcher_command,
                    ]
                )
            ).strip()
            await self._ensure_pane_passthrough(f"{self.pool_session}:{remote_window_id}")
            await self.select_window(remote_window_id)
        if interactive_agent_command is not None:
            await self._send_literal_command(
                f"{self.pool_session}:{remote_window_id}", interactive_agent_command
            )
        await self._run(
            [
                "tmux",
                "set-option",
                "-w",
                "-t",
                f"{self.pool_session}:{remote_window_id}",
                _WINDOW_ID_OPTION,
                str(local_window_id),
            ]
        )
        await self._run(
            [
                "tmux",
                "set-option",
                "-w",
                "-t",
                f"{self.pool_session}:{remote_window_id}",
                _MANAGED_AGENT_TOOLS_OPTION,
                "1",
            ]
        )
        return ClientRuntimeWindow(
            remote_session_id=self.pool_session,
            remote_window_id=remote_window_id,
            local_window_id=local_window_id,
            cwd=effective_cwd,
            shell_command=effective_shell,
            managed_agent_tools=True,
        )

    async def _send_literal_command(self, tmux_target: str, command: str) -> None:
        await self._run(["tmux", "send-keys", "-l", "-t", tmux_target, "--", command])
        await self._run(["tmux", "send-keys", "-t", tmux_target, "Enter"])

    async def recreate_window(
        self,
        window_id: UUID | str,
        *,
        cwd: str | None = None,
        shell_command: str | None = None,
    ) -> ClientRuntimeWindow:
        return await self.create_window(window_id, cwd=cwd, shell_command=shell_command)

    async def select_window(self, remote_window_id: str) -> None:
        await self._run(["tmux", "select-window", "-t", f"{self.pool_session}:{remote_window_id}"])

    async def has_window(
        self,
        remote_window_id: str,
        *,
        remote_session_id: str | None = None,
    ) -> bool:
        session_id = remote_session_id or self.pool_session
        try:
            window_id = (
                await self._run(
                    [
                        "tmux",
                        "display-message",
                        "-p",
                        "-t",
                        f"{session_id}:{remote_window_id}",
                        "#{window_id}",
                    ]
                )
            ).strip()
        except RuntimeError:
            return False
        return window_id == remote_window_id

    async def kill_window(self, window_id: UUID | str) -> None:
        local_window_id = UUID(str(window_id))
        for runtime_window in await self.list_windows():
            if runtime_window.local_window_id != local_window_id:
                continue
            if not await self.has_window(
                runtime_window.remote_window_id,
                remote_session_id=runtime_window.remote_session_id,
            ):
                return
            await self._run(
                [
                    "tmux",
                    "kill-window",
                    "-t",
                    f"{runtime_window.remote_session_id}:{runtime_window.remote_window_id}",
                ]
            )
            self._remove_managed_shell_launcher(local_window_id)
            return

    async def list_windows(self) -> list[ClientRuntimeWindow]:
        await self.ensure_pool()
        output = await self._run(
            [
                "tmux",
                "list-windows",
                "-t",
                self.pool_session,
                "-F",
                f"#{{window_id}}\t#{{{_WINDOW_ID_OPTION}}}\t#{{pane_current_path}}\t#{{{_MANAGED_AGENT_TOOLS_OPTION}}}",
            ]
        )
        windows: list[ClientRuntimeWindow] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            remote_window_id, _, remainder = line.partition("\t")
            local_window_id_text, _, remainder = remainder.partition("\t")
            cwd_text, _, managed_agent_tools_text = remainder.partition("\t")
            local_window_id = _parse_uuid(local_window_id_text.strip())
            cwd = cwd_text or None
            managed_agent_tools = managed_agent_tools_text.strip() == "1"
            windows.append(
                ClientRuntimeWindow(
                    remote_session_id=self.pool_session,
                    remote_window_id=remote_window_id.strip(),
                    local_window_id=local_window_id,
                    cwd=cwd,
                    managed_agent_tools=managed_agent_tools,
                )
            )
        return windows


def _parse_uuid(value: str) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _is_missing_tmux_window_error(exc: BaseException, remote_window_id: str) -> bool:
    message = str(exc)
    return (
        f"can't find window: {remote_window_id}" in message
        or re.search(r"can't find window: @\d+", message) is not None
    )
