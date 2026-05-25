from uuid import UUID

import pytest

from app.client_agent.config import ClientAgentConfig
from app.client_agent.updater import package_checksum, start_self_update, validate_update_payload
from app.services.client_update import build_client_update_package, start_client_update
from app.services.runtime.protocol import AgentMessage, TerminalPayload


CLIENT_ID = UUID("12345678-1234-5678-1234-567812345678")


def test_build_client_update_package_contains_checksum_and_updater_file():
    package = build_client_update_package("job-1")
    files = package["files"]

    assert isinstance(files, dict)
    assert "client_agent/updater.py" in files
    assert package["checksum"] == package_checksum(files, package["requirements"])


def test_validate_update_payload_rejects_checksum_mismatch():
    payload = {
        "job_id": "job-1",
        "files": {"__init__.py": ""},
        "requirements": "pydantic\n",
        "checksum": "wrong",
    }

    with pytest.raises(ValueError, match="checksum"):
        validate_update_payload(payload)


@pytest.mark.asyncio
async def test_start_self_update_stages_files_and_launches_tmux(tmp_path):
    calls: list[list[str]] = []

    async def fake_runner(args: list[str]) -> str:
        calls.append(args)
        return ""

    files = {"__init__.py": "", "client_agent/__init__.py": ""}
    payload = {
        "job_id": "job-1",
        "files": files,
        "requirements": "pydantic\n",
        "checksum": package_checksum(files, "pydantic\n"),
    }
    config = ClientAgentConfig(
        client_id=CLIENT_ID,
        token="token",
        server_url="https://control.example.com",
        name="remote",
        install_path=tmp_path,
    )

    result = await start_self_update(config, payload, runner=fake_runner)

    assert result["job_id"] == "job-1"
    assert (tmp_path / "updates/job-1/app/app/__init__.py").exists()
    update_script = tmp_path / "updates/job-1/run-update.sh"
    assert update_script.exists()
    update_script_text = update_script.read_text(encoding="utf-8")
    assert "pgrep -f" in update_script_text
    assert "/update/complete" in update_script_text
    assert calls == [
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            "web_terminal_acp_update_job_1",
            str(tmp_path / "updates/job-1/run-update.sh"),
        ]
    ]


class FakeRegistry:
    def __init__(self, connection) -> None:
        self.connection = connection

    def get(self, client_id):
        assert client_id == CLIENT_ID
        return self.connection


class LegacyConnection:
    closed = False

    def __init__(self) -> None:
        self.requests: list[AgentMessage] = []
        self.sent: list[AgentMessage] = []

    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        self.requests.append(message)
        if message.type == "self_update_prepare":
            raise TimeoutError()
        if message.type == "create_window":
            return AgentMessage(
                type="create_window_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload={"remote_session_id": "pool", "remote_window_id": "@9"},
            )
        if message.type == "terminal_attach":
            return AgentMessage(
                type="terminal_attach_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload={"ok": True},
            )
        raise AssertionError(message.type)

    async def send(self, message: AgentMessage) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_start_client_update_falls_back_to_terminal_bootstrap_for_old_clients():
    connection = LegacyConnection()

    result = await start_client_update(CLIENT_ID, registry=FakeRegistry(connection))

    assert result.method == "terminal_bootstrap"
    assert [message.type for message in connection.requests] == [
        "self_update_prepare",
        "create_window",
        "terminal_attach",
    ]
    assert connection.sent[0].type == "terminal_input"
    payload = TerminalPayload.model_validate(connection.sent[0].payload)
    script = payload.to_bytes().decode("utf-8")
    assert "WEB_TERMINAL_UPDATE_PY" in script
    assert "pgrep -f" in script
    assert "/update/complete" in script
