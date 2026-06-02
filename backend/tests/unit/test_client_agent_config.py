import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest

import app.client_agent.runner as client_agent_runner
from app.client_agent.__main__ import client_agent_lock
from app.client_agent.config import ClientAgentConfig, default_user_shell
from app.client_agent.tmux_runtime import ClientRuntimeWindow
from app.services.runtime.protocol import AgentMessage, TerminalPayload, encode_agent_message
from app.version import __version__


def _write_config(path: Path, *, server_url: str) -> None:
    path.write_text(
        json.dumps(
            {
                "client_id": "12345678-1234-5678-1234-567812345678",
                "token": "secret-token",
                "server_url": server_url,
                "name": "edge-client",
                "install_path": "/opt/web-terminal-acp-client",
            }
        ),
        encoding="utf-8",
    )


class _CollectingControlWriter:
    def __init__(self, sent_messages: list[dict[str, object]]) -> None:
        self.sent_messages = sent_messages

    async def send(self, message: AgentMessage) -> None:
        self.sent_messages.append(json.loads(encode_agent_message(message)))


class _CollectingBulkWriter:
    def __init__(self, sent_messages: list[dict[str, object]]) -> None:
        self.sent_messages = sent_messages

    async def send_terminal_output(self, message: AgentMessage) -> None:
        self.sent_messages.append(json.loads(encode_agent_message(message)))

    async def send_ai_event(self, message: AgentMessage) -> None:
        self.sent_messages.append(json.loads(encode_agent_message(message)))


class _NoopIdleSupervisor:
    def attach_view(self, view_id: UUID, window_id: UUID) -> None:
        return None

    def detach_view(self, view_id: UUID) -> None:
        return None

    def remove_window(self, window_id: UUID) -> None:
        return None

    def register_window(self, window_id: UUID, project_path: str | None) -> None:
        return None

    async def resume_window(self, window_id: UUID, *, allow_latest_session: bool = False) -> None:
        return None


class _NoopAgentToolWatcher:
    def watch_window(self, window_id: UUID, project_path: str | None) -> None:
        return None

    def remove_window(self, window_id: UUID) -> None:
        return None


class _NoopAuxTerminal:
    async def ensure_terminal(self, *args, **kwargs):
        return None

    async def attach(self, *args, **kwargs) -> None:
        return None

    async def detach(self, *args, **kwargs) -> None:
        return None

    async def send_input(self, *args, **kwargs) -> None:
        return None

    async def resize(self, *args, **kwargs) -> None:
        return None


class _ExistingRuntime:
    async def has_window(
        self,
        remote_window_id: str,
        *,
        remote_session_id: str | None = None,
    ) -> bool:
        return True

    async def recreate_window(
        self,
        window_id: UUID,
        *,
        cwd: str | None = None,
        shell_command: str | None = None,
    ) -> ClientRuntimeWindow:
        raise AssertionError("existing runtime window should not be recreated")


def test_load_populates_required_fields_defaults_and_https_websocket_url(tmp_path: Path) -> None:
    config_path = tmp_path / "client-agent.json"
    _write_config(config_path, server_url="https://control.example.com/")

    config = ClientAgentConfig.load(config_path)

    assert config.client_id == UUID("12345678-1234-5678-1234-567812345678")
    assert config.token == "secret-token"
    assert config.name == "edge-client"
    assert config.install_path == Path("/opt/web-terminal-acp-client")
    assert config.tmux_pool_session == "web_terminal_acp_pool"
    assert config.client_daemon_session == "web_terminal_acp_client"
    assert config.reconnect_initial_delay_seconds == 1
    assert config.reconnect_max_delay_seconds == 30
    assert config.websocket_ping_interval_seconds == 10
    assert config.websocket_ping_timeout_seconds == 10
    assert config.default_shell == default_user_shell()
    assert config.websocket_url == "wss://control.example.com/api/client-agent/ws"


def test_http_server_url_maps_to_ws_websocket_url(tmp_path: Path) -> None:
    config_path = tmp_path / "client-agent.json"
    _write_config(config_path, server_url="http://localhost:8000")

    config = ClientAgentConfig.load(config_path)

    assert config.websocket_url == "ws://localhost:8000/api/client-agent/ws"


def test_https_server_url_maps_to_bulk_websocket_url(tmp_path: Path) -> None:
    config_path = tmp_path / "client-agent.json"
    _write_config(config_path, server_url="https://control.example.com/")

    config = ClientAgentConfig.load(config_path)

    assert config.bulk_websocket_url == "wss://control.example.com/api/client-agent/bulk-ws"


def test_http_server_url_maps_to_bulk_websocket_url(tmp_path: Path) -> None:
    config_path = tmp_path / "client-agent.json"
    _write_config(config_path, server_url="http://localhost:8000")

    config = ClientAgentConfig.load(config_path)

    assert config.bulk_websocket_url == "ws://localhost:8000/api/client-agent/bulk-ws"


def test_explicit_bulk_websocket_url_is_preserved(tmp_path: Path) -> None:
    config_path = tmp_path / "client-agent.json"
    _write_config(config_path, server_url="ws://localhost:8000/api/client-agent/bulk-ws")

    config = ClientAgentConfig.load(config_path)

    assert config.bulk_websocket_url == "ws://localhost:8000/api/client-agent/bulk-ws"


def test_client_agent_lock_rejects_second_process(tmp_path: Path) -> None:
    config = ClientAgentConfig(
        client_id=UUID("12345678-1234-5678-1234-567812345678"),
        token="secret-token",
        server_url="http://control.example.com",
        name="edge-client",
        install_path=tmp_path,
    )

    with client_agent_lock(config):
        try:
            with client_agent_lock(config):
                raise AssertionError("second lock unexpectedly acquired")
        except SystemExit as exc:
            assert exc.code == 2


async def test_run_client_agent_rejects_unexpected_bulk_ack(monkeypatch) -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")

    control_incoming_messages = [
        AgentMessage(type="hello_ack", client_id=client_id),
        AgentMessage(type="shutdown", client_id=client_id),
    ]
    bulk_incoming_messages = [AgentMessage(type="not_bulk_ack", client_id=client_id)]

    class FakeRuntime:
        def __init__(
            self,
            *,
            client_id: UUID,
            server_url: str,
            pool_session: str,
            default_shell: str,
        ) -> None:
            self.client_id = client_id
            self.server_url = server_url
            self.pool_session = pool_session
            self.default_shell = default_shell

        async def list_windows(self) -> list[ClientRuntimeWindow]:
            return []

    class FakeTerminalMultiplexer:
        async def close(self) -> None:
            return None

    class FakeWebSocket:
        def __init__(self, incoming_messages: list[AgentMessage]) -> None:
            self.incoming_messages = incoming_messages

        async def send(self, message: str) -> None:
            return None

        async def recv(self) -> str:
            return encode_agent_message(self.incoming_messages.pop(0))

    class FakeConnection:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, *args: object) -> None:
            return None

    def fake_connect(
        uri: str,
        *,
        extra_headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> FakeConnection:
        if uri.endswith("/api/client-agent/bulk-ws"):
            return FakeConnection(FakeWebSocket(bulk_incoming_messages))
        return FakeConnection(FakeWebSocket(control_incoming_messages))

    monkeypatch.setattr(client_agent_runner.websockets, "connect", fake_connect)
    monkeypatch.setattr(client_agent_runner, "ClientTmuxRuntime", FakeRuntime, raising=False)
    monkeypatch.setattr(
        client_agent_runner,
        "ClientTerminalMultiplexer",
        FakeTerminalMultiplexer,
        raising=False,
    )

    config = ClientAgentConfig(
        client_id=client_id,
        token="secret-token",
        server_url="http://control.example.com",
        name="edge-client",
        install_path=Path("/opt/web-terminal-acp-client"),
    )

    with pytest.raises(RuntimeError, match="unexpected bulk websocket ack"):
        await client_agent_runner._run_client_agent_once(config)


async def test_run_client_agent_handles_inventory_and_tmux_commands(monkeypatch) -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")
    control_sent_messages: list[dict[str, object]] = []
    bulk_sent_messages: list[dict[str, object]] = []
    connect_calls: list[tuple[str, dict[str, str] | None, dict[str, object]]] = []
    registered_windows: list[tuple[UUID, str, str]] = []
    inputs: list[tuple[UUID, bytes]] = []
    resizes: list[tuple[UUID, int, int]] = []

    incoming_messages = [
        AgentMessage(type="hello_ack", client_id=client_id),
        AgentMessage(
            type="create_window",
            client_id=client_id,
            window_id=window_id,
            request_id="request-1",
        ),
        AgentMessage(
            type="terminal_input",
            client_id=client_id,
            window_id=window_id,
            payload=TerminalPayload.from_bytes(window_id, b"echo hi\n").model_dump(mode="json"),
        ),
        AgentMessage(
            type="terminal_resize",
            client_id=client_id,
            window_id=window_id,
            payload={"cols": 120, "rows": 36},
        ),
        AgentMessage(type="shutdown", client_id=client_id),
    ]
    bulk_hello_ack = AgentMessage(type="bulk_hello_ack", client_id=client_id)

    class FakeRuntime:
        def __init__(
            self,
            *,
            client_id: UUID,
            server_url: str,
            pool_session: str,
            default_shell: str,
        ) -> None:
            self.client_id = client_id
            self.server_url = server_url
            self.pool_session = pool_session
            self.default_shell = default_shell

        async def list_windows(self) -> list[ClientRuntimeWindow]:
            return [ClientRuntimeWindow(remote_session_id="client_pool", remote_window_id="@1", local_window_id=window_id)]

        async def create_window(
            self,
            requested_window_id: UUID,
            cwd: str | None = None,
            shell_command: str | None = None,
        ) -> ClientRuntimeWindow:
            assert requested_window_id == window_id
            assert cwd is None
            assert shell_command is None
            return ClientRuntimeWindow(
                remote_session_id="client_pool",
                remote_window_id="@9",
                local_window_id=window_id,
                managed_agent_tools=True,
            )

    class FakeTerminalMultiplexer:
        def register_window(
            self,
            registered_window_id: UUID,
            remote_session_id: str,
            remote_window_id: str,
        ) -> None:
            registered_windows.append((registered_window_id, remote_session_id, remote_window_id))

        async def send_input(self, input_window_id: UUID, data: bytes, *, view_id=None) -> None:
            inputs.append((input_window_id, data))

        async def resize(self, resize_window_id: UUID, *, cols: int, rows: int, view_id=None) -> None:
            resizes.append((resize_window_id, cols, rows))

        async def close(self) -> None:
            return None

    class FakeWebSocket:
        def __init__(
            self,
            incoming: list[AgentMessage],
            sent_messages: list[dict[str, object]],
        ) -> None:
            self._incoming = incoming
            self._sent_messages = sent_messages

        async def send(self, message: str) -> None:
            self._sent_messages.append(json.loads(message))

        async def recv(self) -> str:
            await asyncio.sleep(0)
            return encode_agent_message(self._incoming.pop(0))

    class FakeConnection:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, *args: object) -> None:
            return None

    def fake_connect(
        uri: str,
        *,
        extra_headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> FakeConnection:
        connect_calls.append((uri, extra_headers, kwargs))
        if uri.endswith("/api/client-agent/bulk-ws"):
            return FakeConnection(FakeWebSocket([bulk_hello_ack], bulk_sent_messages))
        return FakeConnection(FakeWebSocket(incoming_messages, control_sent_messages))

    monkeypatch.setattr(client_agent_runner.websockets, "connect", fake_connect)
    monkeypatch.setattr(client_agent_runner, "ClientTmuxRuntime", FakeRuntime, raising=False)
    monkeypatch.setattr(
        client_agent_runner,
        "ClientTerminalMultiplexer",
        FakeTerminalMultiplexer,
        raising=False,
    )

    config = ClientAgentConfig(
        client_id=client_id,
        token="secret-token",
        server_url="http://control.example.com",
        name="edge-client",
        install_path=Path("/opt/web-terminal-acp-client"),
        tmux_pool_session="client_pool",
    )

    await asyncio.wait_for(client_agent_runner.run_client_agent(config), timeout=1)

    expected_headers = {
        "Authorization": "Bearer secret-token",
        "X-Client-Id": "12345678-1234-5678-1234-567812345678",
    }
    assert connect_calls == [
        (
            "ws://control.example.com/api/client-agent/ws",
            expected_headers,
            {"ping_interval": 10, "ping_timeout": 10},
        ),
        (
            "ws://control.example.com/api/client-agent/bulk-ws",
            expected_headers,
            {"ping_interval": 10, "ping_timeout": 10},
        ),
    ]
    assert control_sent_messages[0] == {
        "type": "hello",
        "client_id": str(client_id),
        "window_id": None,
        "request_id": None,
        "payload": {
            "hostname": control_sent_messages[0]["payload"]["hostname"],
            "name": "edge-client",
            "version": __version__,
        },
    }
    assert bulk_sent_messages[0] == {
        "type": "bulk_hello",
        "client_id": str(client_id),
        "window_id": None,
        "request_id": None,
        "payload": {"version": __version__},
    }
    assert control_sent_messages[1] == {
        "type": "inventory",
        "client_id": str(client_id),
        "window_id": None,
        "request_id": None,
        "payload": {
            "windows": [
                {
                    "remote_session_id": "client_pool",
                    "remote_window_id": "@1",
                    "local_window_id": str(window_id),
                    "cwd": None,
                    "shell_command": None,
                    "managed_agent_tools": False,
                }
            ]
        },
    }
    assert {
        "type": "create_window_result",
        "client_id": str(client_id),
        "window_id": str(window_id),
        "request_id": "request-1",
        "payload": {
            "remote_session_id": "client_pool",
            "remote_window_id": "@9",
            "local_window_id": str(window_id),
            "cwd": None,
            "shell_command": None,
            "managed_agent_tools": True,
        },
    } in control_sent_messages
    assert registered_windows == [(window_id, "client_pool", "@1"), (window_id, "client_pool", "@9")]
    assert inputs == [(window_id, b"echo hi\n")]
    assert resizes == [(window_id, 120, 36)]


async def test_run_client_agent_reconnects_after_connection_loss(monkeypatch) -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")
    connect_calls: list[str] = []
    control_sent_messages_by_connection: list[list[dict[str, object]]] = []

    class FakeRuntime:
        def __init__(
            self,
            *,
            client_id: UUID,
            server_url: str,
            pool_session: str,
            default_shell: str,
        ) -> None:
            self.client_id = client_id
            self.server_url = server_url
            self.pool_session = pool_session
            self.default_shell = default_shell

        async def list_windows(self) -> list[ClientRuntimeWindow]:
            return [
                ClientRuntimeWindow(
                    remote_session_id="client_pool",
                    remote_window_id="@1",
                    local_window_id=window_id,
                )
            ]

    class FakeTerminalMultiplexer:
        def register_window(
            self,
            registered_window_id: UUID,
            remote_session_id: str,
            remote_window_id: str,
        ) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeWebSocket:
        def __init__(self, incoming_messages: list[AgentMessage], *, record_sent: bool) -> None:
            self.incoming_messages = incoming_messages
            self.sent_messages: list[dict[str, object]] = []
            if record_sent:
                control_sent_messages_by_connection.append(self.sent_messages)

        async def send(self, message: str) -> None:
            self.sent_messages.append(json.loads(message))

        async def recv(self) -> str:
            if not self.incoming_messages:
                raise OSError("server restarted")
            return encode_agent_message(self.incoming_messages.pop(0))

    class FakeConnection:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, *args: object) -> None:
            return None

    control_connection_messages = [
        [AgentMessage(type="hello_ack", client_id=client_id)],
        [
            AgentMessage(type="hello_ack", client_id=client_id),
            AgentMessage(type="shutdown", client_id=client_id),
        ],
    ]
    bulk_connection_messages = [
        [AgentMessage(type="bulk_hello_ack", client_id=client_id)],
        [AgentMessage(type="bulk_hello_ack", client_id=client_id)],
    ]

    def fake_connect(
        uri: str,
        *,
        extra_headers: dict[str, str] | None = None,
        **kwargs: object,
    ) -> FakeConnection:
        connect_calls.append(uri)
        assert extra_headers is not None
        if uri.endswith("/api/client-agent/bulk-ws"):
            return FakeConnection(FakeWebSocket(bulk_connection_messages.pop(0), record_sent=False))
        return FakeConnection(FakeWebSocket(control_connection_messages.pop(0), record_sent=True))

    monkeypatch.setattr(client_agent_runner.websockets, "connect", fake_connect)
    monkeypatch.setattr(client_agent_runner, "ClientTmuxRuntime", FakeRuntime, raising=False)
    monkeypatch.setattr(
        client_agent_runner,
        "ClientTerminalMultiplexer",
        FakeTerminalMultiplexer,
        raising=False,
    )

    config = ClientAgentConfig(
        client_id=client_id,
        token="secret-token",
        server_url="http://control.example.com",
        name="edge-client",
        install_path=Path("/opt/web-terminal-acp-client"),
        tmux_pool_session="client_pool",
        reconnect_initial_delay_seconds=0,
        reconnect_max_delay_seconds=1,
    )

    await asyncio.wait_for(client_agent_runner.run_client_agent(config), timeout=1)

    assert connect_calls == [
        "ws://control.example.com/api/client-agent/ws",
        "ws://control.example.com/api/client-agent/bulk-ws",
        "ws://control.example.com/api/client-agent/ws",
        "ws://control.example.com/api/client-agent/bulk-ws",
    ]
    assert [messages[1]["type"] for messages in control_sent_messages_by_connection] == [
        "inventory",
        "inventory",
    ]


async def test_send_terminal_output_preserves_raw_bytes() -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")
    sent_messages: list[dict[str, object]] = []

    writer = _CollectingBulkWriter(sent_messages)

    await client_agent_runner._send_terminal_output(
        writer,
        client_id,
        window_id,
        b"\x1b[31mtmux\x1b[0m",
    )

    payloads = [TerminalPayload.model_validate(message["payload"]) for message in sent_messages]
    assert [payload.to_bytes() for payload in payloads] == [b"\x1b[31mtmux\x1b[0m"]


async def test_send_terminal_output_can_mark_attach_snapshot() -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")
    sent_messages: list[dict[str, object]] = []

    writer = _CollectingBulkWriter(sent_messages)

    await client_agent_runner._send_terminal_output(
        writer,
        client_id,
        window_id,
        b"prompt$ ",
        is_snapshot=True,
    )

    assert sent_messages[0]["payload"]["is_snapshot"] is True
    payload = TerminalPayload.model_validate(sent_messages[0]["payload"])
    assert payload.to_bytes() == b"prompt$ "


async def test_terminal_attach_marks_initial_pty_output_as_snapshot() -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")
    sent_messages: list[dict[str, object]] = []

    class FakeTerminalMultiplexer:
        def register_window(
            self,
            registered_window_id: UUID,
            remote_session_id: str,
            remote_window_id: str,
        ) -> None:
            return None

        async def attach_with_selection(self, attached_window_id: UUID, sender, selection_sender=None, view_id=None) -> None:
            await sender(b"prompt$ ")

    control_writer = _CollectingControlWriter([])
    bulk_writer = _CollectingBulkWriter(sent_messages)

    await client_agent_runner._handle_agent_message(
        control_writer,
        bulk_writer,
        ClientAgentConfig(
            client_id=client_id,
            token="secret-token",
            server_url="http://control.example.com",
            name="edge-client",
            install_path=Path("/opt/web-terminal-acp-client"),
        ),
        _ExistingRuntime(),
        FakeTerminalMultiplexer(),
        _NoopIdleSupervisor(),
        _NoopAgentToolWatcher(),
        _NoopAuxTerminal(),
        {},
        set(),
        asyncio.Semaphore(1),
        {},
        AgentMessage(
            type="terminal_attach",
            client_id=client_id,
            window_id=window_id,
            payload={"remote_session_id": "client_pool", "remote_window_id": "@1"},
        ),
    )

    output_messages = [message for message in sent_messages if message["type"] == "terminal_output"]
    assert output_messages[0]["payload"]["is_snapshot"] is True
    payload = TerminalPayload.model_validate(output_messages[0]["payload"])
    assert payload.to_bytes() == b"prompt$ "


async def test_terminal_attach_keeps_later_pty_output_recordable() -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")
    sent_messages: list[dict[str, object]] = []

    class FakeTerminalMultiplexer:
        def register_window(
            self,
            registered_window_id: UUID,
            remote_session_id: str,
            remote_window_id: str,
        ) -> None:
            return None

        async def attach_with_selection(self, attached_window_id: UUID, sender, selection_sender=None, view_id=None) -> None:
            await sender(b"prompt$ ")
            await sender(b"real output\n")

    control_writer = _CollectingControlWriter([])
    bulk_writer = _CollectingBulkWriter(sent_messages)

    await client_agent_runner._handle_agent_message(
        control_writer,
        bulk_writer,
        ClientAgentConfig(
            client_id=client_id,
            token="secret-token",
            server_url="http://control.example.com",
            name="edge-client",
            install_path=Path("/opt/web-terminal-acp-client"),
        ),
        _ExistingRuntime(),
        FakeTerminalMultiplexer(),
        _NoopIdleSupervisor(),
        _NoopAgentToolWatcher(),
        _NoopAuxTerminal(),
        {},
        set(),
        asyncio.Semaphore(1),
        {},
        AgentMessage(
            type="terminal_attach",
            client_id=client_id,
            window_id=window_id,
            payload={"remote_session_id": "client_pool", "remote_window_id": "@1"},
        ),
    )

    output_messages = [message for message in sent_messages if message["type"] == "terminal_output"]
    assert output_messages[0]["payload"]["is_snapshot"] is True
    assert "is_snapshot" not in output_messages[1]["payload"]
    payload = TerminalPayload.model_validate(output_messages[1]["payload"])
    assert payload.to_bytes() == b"real output\n"


async def test_silent_attach_snapshot_does_not_mark_later_pty_output_as_snapshot() -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")
    sent_messages: list[dict[str, object]] = []
    captured_sender = None

    class FakeTerminalMultiplexer:
        def register_window(
            self,
            registered_window_id: UUID,
            remote_session_id: str,
            remote_window_id: str,
        ) -> None:
            return None

        async def attach_with_selection(self, attached_window_id: UUID, sender, selection_sender=None, view_id=None) -> None:
            nonlocal captured_sender
            captured_sender = sender

        async def capture_output_bytes(self, captured_window_id: UUID, *, view_id=None) -> bytes:
            return b"prompt$ "

    control_writer = _CollectingControlWriter([])
    bulk_writer = _CollectingBulkWriter(sent_messages)

    attach_snapshot_tasks: dict[UUID, asyncio.Task[None]] = {}
    await client_agent_runner._handle_agent_message(
        control_writer,
        bulk_writer,
        ClientAgentConfig(
            client_id=client_id,
            token="secret-token",
            server_url="http://control.example.com",
            name="edge-client",
            install_path=Path("/opt/web-terminal-acp-client"),
        ),
        _ExistingRuntime(),
        FakeTerminalMultiplexer(),
        _NoopIdleSupervisor(),
        _NoopAgentToolWatcher(),
        _NoopAuxTerminal(),
        attach_snapshot_tasks,
        set(),
        asyncio.Semaphore(1),
        {},
        AgentMessage(
            type="terminal_attach",
            client_id=client_id,
            window_id=window_id,
            payload={"remote_session_id": "client_pool", "remote_window_id": "@1"},
        ),
    )

    await attach_snapshot_tasks[window_id]
    assert captured_sender is not None
    await captured_sender(b"real output\n")

    output_messages = [message for message in sent_messages if message["type"] == "terminal_output"]
    assert output_messages[0]["payload"]["is_snapshot"] is True
    assert "is_snapshot" not in output_messages[1]["payload"]
    payload = TerminalPayload.model_validate(output_messages[1]["payload"])
    assert payload.to_bytes() == b"real output\n"


async def test_terminal_detach_cancels_pending_attach_snapshot() -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")

    class FakeTerminalMultiplexer:
        def register_window(
            self,
            registered_window_id: UUID,
            remote_session_id: str,
            remote_window_id: str,
        ) -> None:
            return None

        async def attach_with_selection(self, attached_window_id: UUID, sender, selection_sender=None, view_id=None) -> None:
            return None

        async def detach(self, detached_window_id: UUID, *, view_id=None) -> None:
            return None

        async def capture_output_bytes(self, captured_window_id: UUID, *, view_id=None) -> bytes:
            return b"prompt$ "

    control_writer = _CollectingControlWriter([])
    bulk_writer = _CollectingBulkWriter([])

    attach_snapshot_tasks: dict[UUID, asyncio.Task[None]] = {}
    config = ClientAgentConfig(
        client_id=client_id,
        token="secret-token",
        server_url="http://control.example.com",
        name="edge-client",
        install_path=Path("/opt/web-terminal-acp-client"),
    )
    terminal = FakeTerminalMultiplexer()
    await client_agent_runner._handle_agent_message(
        control_writer,
        bulk_writer,
        config,
        _ExistingRuntime(),
        terminal,
        _NoopIdleSupervisor(),
        _NoopAgentToolWatcher(),
        _NoopAuxTerminal(),
        attach_snapshot_tasks,
        set(),
        asyncio.Semaphore(1),
        {},
        AgentMessage(
            type="terminal_attach",
            client_id=client_id,
            window_id=window_id,
            payload={"remote_session_id": "client_pool", "remote_window_id": "@1"},
        ),
    )

    await client_agent_runner._handle_agent_message(
        control_writer,
        bulk_writer,
        config,
        _ExistingRuntime(),
        terminal,
        _NoopIdleSupervisor(),
        _NoopAgentToolWatcher(),
        _NoopAuxTerminal(),
        attach_snapshot_tasks,
        set(),
        asyncio.Semaphore(1),
        {},
        AgentMessage(type="terminal_detach", client_id=client_id, window_id=window_id),
    )

    assert window_id not in attach_snapshot_tasks


async def test_send_terminal_selection_uses_terminal_selection_message() -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")
    sent_messages: list[dict[str, object]] = []

    writer = _CollectingControlWriter(sent_messages)

    await client_agent_runner._send_terminal_selection(writer, client_id, window_id)

    assert sent_messages == [
        {
            "type": "terminal_selection",
            "client_id": str(client_id),
            "window_id": str(window_id),
            "request_id": None,
            "payload": {},
        }
    ]


async def test_send_terminal_selection_includes_view_id_when_present() -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")
    view_id = UUID("11111111-2222-3333-4444-555555555555")
    sent_messages: list[dict[str, object]] = []

    writer = _CollectingControlWriter(sent_messages)

    await client_agent_runner._send_terminal_selection(
        writer,
        client_id,
        window_id,
        view_id=view_id,
    )

    assert sent_messages[0]["payload"] == {"view_id": str(view_id)}


async def test_send_terminal_attach_result_uses_request_id() -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")
    sent_messages: list[dict[str, object]] = []

    writer = _CollectingControlWriter(sent_messages)

    await client_agent_runner._send_terminal_attach_result(
        writer,
        client_id,
        window_id,
        request_id="attach-1",
    )

    assert sent_messages == [
        {
            "type": "terminal_attach_result",
            "client_id": str(client_id),
            "window_id": str(window_id),
            "request_id": "attach-1",
            "payload": {"ok": True},
        }
    ]


async def test_send_terminal_error_uses_request_id_and_message() -> None:
    client_id = UUID("12345678-1234-5678-1234-567812345678")
    window_id = UUID("87654321-4321-8765-4321-876543218765")
    sent_messages: list[dict[str, object]] = []

    writer = _CollectingControlWriter(sent_messages)

    await client_agent_runner._send_terminal_error(
        writer,
        client_id,
        window_id,
        request_id="attach-1",
        message="tmux attach failed",
    )

    assert sent_messages == [
        {
            "type": "terminal_error",
            "client_id": str(client_id),
            "window_id": str(window_id),
            "request_id": "attach-1",
            "payload": {"message": "tmux attach failed"},
        }
    ]
