from __future__ import annotations

import asyncio
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shlex
from typing import Iterator, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, ClientRuntime, ClientStatus
from app.repositories.clients import create_client, get_client, hash_client_token, verify_client_token
from app.schemas import BootstrapClientIn
from app.services.bootstrap.ssh import SshClient, SshCommandError, SshConnectionInfo

DEFAULT_INSTALL_PATH = "~/.web-terminal-acp"
CONFIG_PATH = f"{DEFAULT_INSTALL_PATH}/config.json"
APP_PATH = f"{DEFAULT_INSTALL_PATH}/app"
VENV_PATH = f"{DEFAULT_INSTALL_PATH}/venv"
REQUIREMENTS_PATH = f"{DEFAULT_INSTALL_PATH}/requirements.txt"
DAEMON_SESSION = "web_terminal_acp_client"
AGENT_REQUIREMENTS = "pydantic>=2.8.0\nwebsockets>=13.1\n"


class BootstrapDependencyError(RuntimeError):
    """The remote target is missing a required bootstrap dependency."""


class BootstrapConnectionError(RuntimeError):
    """The server could not connect to or operate on the remote SSH target."""


@dataclass(frozen=True)
class BootstrapResult:
    client_id: UUID
    name: str
    status: str
    reused: bool


class BootstrapSshClient(Protocol):
    def run(self, command: str) -> str: ...

    def upload_text(self, remote_path: str, text: str, mode: int = 0o600) -> None: ...




class BootstrapSecretRedactor:
    def __init__(self, secrets: list[str | None]) -> None:
        self._secrets = sorted(
            {secret for secret in secrets if secret}, key=lambda secret: len(secret), reverse=True
        )

    def redact(self, value: object) -> str:
        text = str(value)
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            "[REDACTED]",
            text,
            flags=re.DOTALL,
        )
        return re.sub(r"\b\S*token\S*\b", "[REDACTED]", text, flags=re.IGNORECASE)


def dependency_check_script() -> str:
    return """#!/usr/bin/env bash
set -u
missing=""
command -v python3 >/dev/null 2>&1 || missing="$missing python3"
command -v tmux >/dev/null 2>&1 || missing="$missing tmux"
command -v bash >/dev/null 2>&1 || missing="$missing bash"
venv_test="$(mktemp -d 2>/dev/null || true)"
if [ -z "$venv_test" ] || ! python3 -m venv "$venv_test/venv" >/dev/null 2>&1 || ! "$venv_test/venv/bin/python" -m pip --version >/dev/null 2>&1; then
  missing="$missing python3-venv"
fi
rm -rf "$venv_test"
if [ -n "$missing" ]; then
  echo "missing dependencies:$missing" >&2
  exit 42
fi
"""


def build_client_config(client: Client, *, token: str, server_url: str, install_path: str) -> str:
    return json.dumps(
        build_client_config_payload(
            client,
            token=token,
            server_url=server_url,
            install_path=install_path,
        ),
        indent=2,
        sort_keys=True,
    )


def build_client_config_payload(
    client: Client,
    *,
    token: str,
    server_url: str,
    install_path: str,
) -> dict[str, str]:
    return {
        "client_id": str(client.id),
        "token": token,
        "server_url": server_url,
        "name": client.name,
        "install_path": install_path,
    }


async def bootstrap_client(
    session: AsyncSession,
    payload: BootstrapClientIn,
    *,
    ssh_client_factory=SshClient,
) -> BootstrapResult:
    redactor = BootstrapSecretRedactor([payload.private_key, payload.passphrase])
    info = SshConnectionInfo(
        host=payload.host,
        port=payload.port,
        username=payload.username,
        private_key=payload.private_key,
        passphrase=payload.passphrase,
    )

    try:
        existing_config_text = await asyncio.to_thread(
            _check_dependencies_and_read_existing_config,
            info,
            ssh_client_factory,
        )
        client, token, reused = await _resolve_client(session, payload, existing_config_text)
        redactor = BootstrapSecretRedactor([payload.private_key, payload.passphrase, token])
        config_text = build_client_config(
            client,
            token=token,
            server_url=payload.server_url,
            install_path=DEFAULT_INSTALL_PATH,
        )
        await asyncio.to_thread(
            _upload_and_start_client,
            info,
            ssh_client_factory,
            config_text,
        )
        client.last_update_at = datetime.now(UTC)
        await session.flush()
        return BootstrapResult(
            client_id=client.id,
            name=client.name,
            status=client.status.value,
            reused=reused,
        )
    except BootstrapDependencyError as exc:
        raise BootstrapDependencyError(redactor.redact(exc)) from None
    except BootstrapConnectionError as exc:
        raise BootstrapConnectionError(redactor.redact(exc)) from None
    except Exception as exc:
        raise BootstrapConnectionError(redactor.redact(exc)) from None


def _check_dependencies_and_read_existing_config(
    info: SshConnectionInfo,
    ssh_client_factory,
) -> str | None:
    with _connect(info, ssh_client_factory) as ssh:
        try:
            ssh.run(dependency_check_script())
        except BootstrapDependencyError:
            raise
        except SshCommandError as exc:
            if exc.exit_status == 42:
                raise BootstrapDependencyError(exc.stderr.strip() or "missing bootstrap dependency") from None
            raise BootstrapConnectionError(str(exc)) from None

        try:
            return ssh.run(f"cat {CONFIG_PATH}")
        except Exception:
            return None


def _upload_and_start_client(
    info: SshConnectionInfo,
    ssh_client_factory,
    config_text: str,
) -> None:
    with _connect(info, ssh_client_factory) as ssh:
        ssh.upload_text(CONFIG_PATH, config_text, mode=0o600)
        ssh.upload_text(REQUIREMENTS_PATH, AGENT_REQUIREMENTS, mode=0o644)
        for remote_path, text in _client_app_files().items():
            ssh.upload_text(remote_path, text, mode=0o644)
        ssh.run(_install_agent_dependencies_command())
        ssh.run(_start_daemon_command())


@contextmanager
def _connect(info: SshConnectionInfo, ssh_client_factory) -> Iterator[BootstrapSshClient]:
    try:
        client = ssh_client_factory(info)
        manager = client if hasattr(client, "__enter__") else nullcontext(client)
        with manager as connected:
            yield connected
    except BootstrapDependencyError:
        raise
    except BootstrapConnectionError:
        raise
    except Exception as exc:
        raise BootstrapConnectionError(str(exc)) from None


async def _resolve_client(
    session: AsyncSession,
    payload: BootstrapClientIn,
    existing_config_text: str | None,
) -> tuple[Client, str, bool]:
    existing = _parse_existing_config(existing_config_text)
    if existing is not None:
        client_id, token = existing
        client = await get_client(session, client_id)
        if client is None:
            client = Client(
                id=client_id,
                name=payload.name,
                status=ClientStatus.OFFLINE,
                token_hash=hash_client_token(token),
                hostname=payload.host,
                install_path=DEFAULT_INSTALL_PATH,
                runtime=ClientRuntime.remote,
            )
            session.add(client)
            await session.flush()
            return client, token, True
        if verify_client_token(token, client.token_hash):
            client.name = payload.name
            client.hostname = payload.host
            client.install_path = DEFAULT_INSTALL_PATH
            client.runtime = ClientRuntime.remote
            await session.flush()
            return client, token, True

    client, token = await create_client(
        session,
        name=payload.name,
        hostname=payload.host,
        install_path=DEFAULT_INSTALL_PATH,
        runtime=ClientRuntime.remote,
    )
    return client, token, False


def _parse_existing_config(config_text: str | None) -> tuple[UUID, str] | None:
    if not config_text:
        return None
    try:
        data = json.loads(config_text)
        client_id = UUID(str(data["client_id"]))
        token = str(data["token"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not token:
        return None
    return client_id, token


def _client_app_files() -> dict[str, str]:
    return {
        f"{APP_PATH}/app/{relative_path}": text
        for relative_path, text in client_app_file_contents().items()
    }


def client_app_file_contents() -> dict[str, str]:
    backend_app = Path(__file__).resolve().parents[2]
    source_files = [
        "__init__.py",
        "version.py",
        "client_agent/__init__.py",
        "client_agent/__main__.py",
        "client_agent/config.py",
        "client_agent/runner.py",
        "client_agent/agent_commands.py",
        "client_agent/agent_idle.py",
        "client_agent/git_worktree.py",
        "client_agent/outbound.py",
        "client_agent/agent_tool_watchers.py",
        "client_agent/agent_work_presence.py",
        "client_agent/ai_events.py",
        "client_agent/codex_watcher.py",
        "client_agent/cursor_watcher.py",
        "client_agent/shell_hook.py",
        "client_agent/terminal.py",
        "client_agent/tmux_runtime.py",
        "client_agent/updater.py",
        "services/__init__.py",
        "services/agent_config.py",
        "services/terminal_command_marker.py",
        "services/runtime/__init__.py",
        "services/runtime/protocol.py",
    ]
    return {
        relative_path: (backend_app / relative_path).read_text(encoding="utf-8")
        for relative_path in source_files
    }


def _install_agent_dependencies_command() -> str:
    return (
        f"mkdir -p {DEFAULT_INSTALL_PATH}/npm-global/bin && "
        f"python3 -m venv {VENV_PATH} && "
        f"{VENV_PATH}/bin/python -m pip install --upgrade pip && "
        f"{VENV_PATH}/bin/python -m pip install -r {REQUIREMENTS_PATH}"
    )


def _start_daemon_command() -> str:
    stop_existing_processes = _kill_existing_client_processes_command()
    return (
        f"tmux kill-session -t {DAEMON_SESSION} >/dev/null 2>&1 || true; "
        f"{stop_existing_processes}; "
        f"tmux new-session -d -s {DAEMON_SESSION} "
        "'cd ~/.web-terminal-acp/app && "
        'PATH="$HOME/.web-terminal-acp/npm-global/bin:$PATH" '
        "PYTHONPATH=~/.web-terminal-acp/app "
        f"{VENV_PATH}/bin/python -m app.client_agent --config ~/.web-terminal-acp/config.json'"
    )


def _kill_existing_client_processes_command() -> str:
    pattern = r"python.*-m app[.]client_agent.*--config .*/[.]web-terminal-acp/config[.]json"
    return (
        f"for pid in $(pgrep -f {shlex.quote(pattern)} || true); do "
        'if [ "$pid" != "$$" ]; then kill "$pid" >/dev/null 2>&1 || true; fi; '
        "done; "
        "sleep 1; "
        f"for pid in $(pgrep -f {shlex.quote(pattern)} || true); do "
        'if [ "$pid" != "$$" ]; then kill -9 "$pid" >/dev/null 2>&1 || true; fi; '
        "done"
    )
