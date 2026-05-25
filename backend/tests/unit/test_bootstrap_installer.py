import json
import traceback
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import Client
from app.schemas import BootstrapClientIn
from app.services.bootstrap.installer import (
    BootstrapConnectionError,
    BootstrapDependencyError,
    BootstrapSecretRedactor,
    build_client_config,
    bootstrap_client,
    client_app_file_contents,
    dependency_check_script,
)


PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-key-body\n-----END OPENSSH PRIVATE KEY-----"
PASSPHRASE = "correct horse battery staple"
TOKEN = "plain-client-token"


def _formatted_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


@pytest.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield session_factory

    await engine.dispose()


def test_redactor_removes_private_key_passphrase_and_token_from_messages():
    redactor = BootstrapSecretRedactor([PRIVATE_KEY, PASSPHRASE, TOKEN])

    message = f"failed with {PRIVATE_KEY} / {PASSPHRASE} / {TOKEN}"

    redacted = redactor.redact(message)

    assert PRIVATE_KEY not in redacted
    assert PASSPHRASE not in redacted
    assert TOKEN not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_dependency_check_script_checks_required_bins_without_sudo():
    script = dependency_check_script()

    assert "command -v python3" in script
    assert "command -v tmux" in script
    assert "command -v bash" in script
    assert "import venv" in script
    assert "sudo" not in script.lower()


def test_build_client_config_contains_token_only_in_target_config():
    client_id = uuid4()
    config_text = build_client_config(
        SimpleNamespace(id=client_id, name="Remote Dev"),
        token=TOKEN,
        server_url="https://control.example.com/",
        install_path="~/.web-terminal-acp",
    )

    config = json.loads(config_text)

    assert config["client_id"] == str(client_id)
    assert config["token"] == TOKEN
    assert config["server_url"] == "https://control.example.com/"
    assert config["name"] == "Remote Dev"
    assert PRIVATE_KEY not in config_text
    assert PASSPHRASE not in config_text


def test_client_app_file_contents_packages_agent_tool_watchers():
    files = client_app_file_contents()

    assert "client_agent/git_worktree.py" in files
    assert "client_agent/agent_tool_watchers.py" in files
    assert "client_agent/agent_work_presence.py" in files
    assert "client_agent/cursor_watcher.py" in files
    assert "client_agent/outbound.py" in files
    watcher_source = files["client_agent/agent_tool_watchers.py"]
    presence_source = files["client_agent/agent_work_presence.py"]
    outbound_source = files["client_agent/outbound.py"]
    assert "def watch_agent_tool_events" in watcher_source
    assert "from app.client_agent.agent_work_presence import" in watcher_source
    assert "def detect_agent_work_presence" in presence_source
    assert "app.agent_tools" not in presence_source
    assert "class BulkUploadWriter" in outbound_source


class FakeSsh:
    def __init__(self, *, existing_config: str | None = None, missing_dependency: str | None = None):
        self.existing_config = existing_config
        self.missing_dependency = missing_dependency
        self.uploads: dict[str, str] = {}
        self.commands: list[str] = []

    def run(self, command: str) -> str:
        self.commands.append(command)
        if "command -v" in command and self.missing_dependency is not None:
            raise BootstrapDependencyError(f"missing dependency: {self.missing_dependency}")
        if "cat ~/.web-terminal-acp/config.json" in command:
            if self.existing_config is None:
                raise FileNotFoundError("missing config")
            return self.existing_config
        return ""

    def upload_text(self, path: str, text: str, mode: int = 0o600) -> None:
        self.uploads[path] = text


@pytest.mark.asyncio
async def test_bootstrap_client_creates_client_and_uploads_config(db_session_factory):
    ssh = FakeSsh()
    payload = BootstrapClientIn(
        name="Remote Dev",
        host="dev.example.com",
        port=22,
        username="alice",
        private_key=PRIVATE_KEY,
        passphrase=PASSPHRASE,
        server_url="https://control.example.com",
    )

    async with db_session_factory() as session:
        result = await bootstrap_client(session, payload, ssh_client_factory=lambda _info: ssh)
        db_client = await session.get(Client, result.client_id)
        await session.commit()

    assert result.reused is False
    assert result.name == "Remote Dev"
    assert result.status == "OFFLINE"
    assert db_client is not None
    assert db_client.last_update_at is not None
    uploaded_config = ssh.uploads["~/.web-terminal-acp/config.json"]
    config = json.loads(uploaded_config)
    assert config["client_id"] == str(result.client_id)
    assert config["token"]
    assert PRIVATE_KEY not in uploaded_config
    assert PASSPHRASE not in uploaded_config
    assert "~/.web-terminal-acp/requirements.txt" in ssh.uploads
    assert "app.client_agent.agent_tool_watchers" in ssh.uploads["~/.web-terminal-acp/app/app/client_agent/runner.py"]
    assert "~/.web-terminal-acp/app/app/client_agent/agent_tool_watchers.py" in ssh.uploads
    assert "~/.web-terminal-acp/app/app/client_agent/codex_watcher.py" in ssh.uploads
    assert "~/.web-terminal-acp/app/app/client_agent/cursor_watcher.py" in ssh.uploads
    assert any("python3 -m venv ~/.web-terminal-acp/venv" in command for command in ssh.commands)
    assert any("tmux" in command and "web_terminal_acp_client" in command for command in ssh.commands)
    assert any("pgrep -f" in command for command in ssh.commands)
    assert any("~/.web-terminal-acp/venv/bin/python -m app.client_agent" in command for command in ssh.commands)


@pytest.mark.asyncio
async def test_bootstrap_client_reuses_existing_remote_config(db_session_factory):
    client_id = uuid4()
    existing_config = json.dumps(
        {
            "client_id": str(client_id),
            "token": TOKEN,
            "server_url": "https://control.example.com",
            "name": "Existing Dev",
            "install_path": "~/.web-terminal-acp",
        }
    )
    ssh = FakeSsh(existing_config=existing_config)
    payload = BootstrapClientIn(
        name="Existing Dev",
        host="dev.example.com",
        port=22,
        username="alice",
        private_key=PRIVATE_KEY,
        passphrase=None,
        server_url="https://control.example.com",
    )

    async with db_session_factory() as session:
        result = await bootstrap_client(session, payload, ssh_client_factory=lambda _info: ssh)
        db_client = await session.get(Client, result.client_id)
        await session.commit()

    assert result.reused is True
    assert result.client_id == client_id
    assert db_client is not None
    assert db_client.last_update_at is not None
    assert json.loads(ssh.uploads["~/.web-terminal-acp/config.json"])["token"] == TOKEN


@pytest.mark.asyncio
async def test_bootstrap_client_redacts_connection_errors(db_session_factory):
    def fail_factory(_info):
        raise BootstrapConnectionError(f"auth failed {PRIVATE_KEY} {PASSPHRASE}")

    payload = BootstrapClientIn(
        name="Remote Dev",
        host="dev.example.com",
        port=22,
        username="alice",
        private_key=PRIVATE_KEY,
        passphrase=PASSPHRASE,
        server_url="https://control.example.com",
    )

    async with db_session_factory() as session:
        with pytest.raises(BootstrapConnectionError) as exc_info:
            await bootstrap_client(session, payload, ssh_client_factory=fail_factory)

    message = str(exc_info.value)
    assert PRIVATE_KEY not in message
    assert PASSPHRASE not in message


@pytest.mark.asyncio
async def test_bootstrap_client_traceback_redacts_secret_connection_cause(db_session_factory):
    def fail_factory(_info):
        raise ValueError(f"auth failed {PRIVATE_KEY} {PASSPHRASE}")

    payload = BootstrapClientIn(
        name="Remote Dev",
        host="dev.example.com",
        port=22,
        username="alice",
        private_key=PRIVATE_KEY,
        passphrase=PASSPHRASE,
        server_url="https://control.example.com",
    )

    async with db_session_factory() as session:
        with pytest.raises(BootstrapConnectionError) as exc_info:
            await bootstrap_client(session, payload, ssh_client_factory=fail_factory)

    formatted = _formatted_exception(exc_info.value)
    assert PRIVATE_KEY not in formatted
    assert PASSPHRASE not in formatted


@pytest.mark.asyncio
async def test_bootstrap_client_traceback_redacts_generated_token_cause(db_session_factory):
    class FailingUploadSsh(FakeSsh):
        def __init__(self):
            super().__init__()
            self.attempted_config: str | None = None

        def upload_text(self, path: str, text: str, mode: int = 0o600) -> None:
            if path == "~/.web-terminal-acp/config.json":
                self.attempted_config = text
                token = json.loads(text)["token"]
                raise RuntimeError(f"upload failed with token {token}")
            super().upload_text(path, text, mode=mode)

    ssh = FailingUploadSsh()
    payload = BootstrapClientIn(
        name="Remote Dev",
        host="dev.example.com",
        port=22,
        username="alice",
        private_key=PRIVATE_KEY,
        passphrase=PASSPHRASE,
        server_url="https://control.example.com",
    )

    async with db_session_factory() as session:
        with pytest.raises(BootstrapConnectionError) as exc_info:
            await bootstrap_client(session, payload, ssh_client_factory=lambda _info: ssh)

    assert ssh.attempted_config is not None
    generated_token = json.loads(ssh.attempted_config)["token"]
    formatted = _formatted_exception(exc_info.value)
    assert PRIVATE_KEY not in formatted
    assert PASSPHRASE not in formatted
    assert generated_token not in formatted
