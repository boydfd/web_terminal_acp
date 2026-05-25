from uuid import uuid4
import asyncio

import pytest

from app.services.runtime.client_connections import ClientConnectionClosed
from app.services.runtime.protocol import AgentMessage, TerminalPayload
from app.services.runtime.remote import RemoteClientUnavailable, RemoteRuntime, RemoteTerminalError
from app.services.runtime.types import RuntimeWindow


class FakeRegistry:
    def __init__(self, connection=None) -> None:
        self.connection = connection
        self.requested_client_ids = []

    def get(self, client_id):
        self.requested_client_ids.append(client_id)
        return self.connection


class FakeConnection:
    def __init__(self, response: AgentMessage | None = None) -> None:
        self.response = response
        self.requests: list[tuple[AgentMessage, float]] = []
        self.sent: list[AgentMessage] = []

    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        self.requests.append((message, timeout))
        assert self.response is not None
        return self.response

    async def send(self, message: AgentMessage) -> None:
        self.sent.append(message)


class ClosedConnection(FakeConnection):
    closed = True


class TimeoutConnection(FakeConnection):
    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        raise asyncio.TimeoutError


class ClosingConnection(FakeConnection):
    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        raise ClientConnectionClosed("closed")


@pytest.mark.asyncio
async def test_create_window_sends_request_and_returns_remote_runtime_window() -> None:
    client_id = uuid4()
    window_id = uuid4()
    response = AgentMessage(
        type="create_window_result",
        client_id=client_id,
        window_id=window_id,
        request_id="response-request",
        payload={
            "remote_session_id": "session-1",
            "remote_window_id": "window-2",
            "cwd": "/remote/project",
            "shell_command": "/bin/zsh",
        },
    )
    connection = FakeConnection(response)
    runtime = RemoteRuntime(client_id=client_id, registry=FakeRegistry(connection), request_timeout=7.5)

    runtime_window = await runtime.create_window(cwd="/ignored", shell_command="/bin/bash", window_id=window_id)

    assert runtime_window == RuntimeWindow(
        session_id="session-1",
        window_id="window-2",
        cwd="/remote/project",
        shell_command="/bin/zsh",
    )
    assert len(connection.requests) == 1
    message, timeout = connection.requests[0]
    assert timeout == 7.5
    assert message.type == "create_window"
    assert message.client_id == client_id
    assert message.window_id == window_id
    assert message.request_id is not None
    assert message.payload == {"cwd": "/ignored", "shell_command": "/bin/bash"}


@pytest.mark.asyncio
async def test_create_window_raises_when_remote_client_is_unavailable() -> None:
    client_id = uuid4()
    runtime = RemoteRuntime(client_id=client_id, registry=FakeRegistry(None))

    with pytest.raises(RemoteClientUnavailable) as exc_info:
        await runtime.create_window(cwd=None, window_id=uuid4())
    assert exc_info.value.reason == "no_connection"


@pytest.mark.asyncio
async def test_create_window_raises_with_connection_closed_reason() -> None:
    client_id = uuid4()
    runtime = RemoteRuntime(client_id=client_id, registry=FakeRegistry(ClosedConnection()))

    with pytest.raises(RemoteClientUnavailable) as exc_info:
        await runtime.create_window(cwd=None, window_id=uuid4())
    assert exc_info.value.reason == "connection_closed"


@pytest.mark.asyncio
async def test_create_window_raises_with_request_timeout_reason() -> None:
    client_id = uuid4()
    runtime = RemoteRuntime(client_id=client_id, registry=FakeRegistry(TimeoutConnection()))

    with pytest.raises(RemoteClientUnavailable) as exc_info:
        await runtime.create_window(cwd=None, window_id=uuid4())
    assert exc_info.value.reason == "request_timeout"


@pytest.mark.asyncio
async def test_create_window_raises_with_request_closed_reason() -> None:
    client_id = uuid4()
    runtime = RemoteRuntime(client_id=client_id, registry=FakeRegistry(ClosingConnection()))

    with pytest.raises(RemoteClientUnavailable) as exc_info:
        await runtime.create_window(cwd=None, window_id=uuid4())
    assert exc_info.value.reason == "connection_closed"


@pytest.mark.asyncio
async def test_attach_sends_remote_terminal_attach_request() -> None:
    client_id = uuid4()
    window_id = uuid4()
    connection = FakeConnection(
        AgentMessage(
            type="terminal_attach_result",
            client_id=client_id,
            window_id=window_id,
            request_id="attach-response",
            payload={"ok": True},
        )
    )
    runtime = RemoteRuntime(client_id=client_id, registry=FakeRegistry(connection))
    runtime_window = RuntimeWindow(session_id="remote-session", window_id="remote-window")

    async def ignored_sender(data: bytes) -> None:
        raise AssertionError("remote runtime does not call local sender directly")

    await runtime.attach(runtime_window, ignored_sender, local_window_id=window_id)

    assert len(connection.requests) == 1
    message, timeout = connection.requests[0]
    assert timeout == 10.0
    assert message.type == "terminal_attach"
    assert message.client_id == client_id
    assert message.window_id == window_id
    assert message.request_id is not None
    assert message.payload == {
        "remote_session_id": "remote-session",
        "remote_window_id": "remote-window",
    }


@pytest.mark.asyncio
async def test_attach_raises_remote_terminal_error_when_client_reports_failure() -> None:
    client_id = uuid4()
    window_id = uuid4()
    connection = FakeConnection(
        AgentMessage(
            type="terminal_error",
            client_id=client_id,
            window_id=window_id,
            request_id="attach-response",
            payload={"message": "tmux attach failed"},
        )
    )
    runtime = RemoteRuntime(client_id=client_id, registry=FakeRegistry(connection))
    runtime_window = RuntimeWindow(session_id="remote-session", window_id="remote-window")

    async def ignored_sender(data: bytes) -> None:
        raise AssertionError("remote runtime does not call local sender directly")

    with pytest.raises(RemoteTerminalError, match="tmux attach failed"):
        await runtime.attach(runtime_window, ignored_sender, local_window_id=window_id)


@pytest.mark.asyncio
async def test_detach_sends_remote_terminal_detach_request() -> None:
    client_id = uuid4()
    window_id = uuid4()
    connection = FakeConnection()
    runtime = RemoteRuntime(client_id=client_id, registry=FakeRegistry(connection))
    runtime_window = RuntimeWindow(session_id="remote-session", window_id="remote-window")

    await runtime.detach(runtime_window, local_window_id=window_id)

    assert len(connection.sent) == 1
    message = connection.sent[0]
    assert message.type == "terminal_detach"
    assert message.client_id == client_id
    assert message.window_id == window_id
    assert message.payload == {
        "remote_session_id": "remote-session",
        "remote_window_id": "remote-window",
    }


@pytest.mark.asyncio
async def test_send_input_sends_base64_terminal_payload() -> None:
    client_id = uuid4()
    window_id = uuid4()
    connection = FakeConnection()
    runtime = RemoteRuntime(client_id=client_id, registry=FakeRegistry(connection))
    runtime_window = RuntimeWindow(session_id="remote-session", window_id="remote-window")

    await runtime.send_input(runtime_window, b"whoami\n", local_window_id=window_id)

    assert len(connection.sent) == 1
    message = connection.sent[0]
    assert message.type == "terminal_input"
    assert message.client_id == client_id
    assert message.window_id == window_id
    payload = TerminalPayload.model_validate(message.payload)
    assert payload.window_id == window_id
    assert payload.to_bytes() == b"whoami\n"


@pytest.mark.asyncio
async def test_resize_sends_resize_message_with_browser_window_id() -> None:
    client_id = uuid4()
    window_id = uuid4()
    connection = FakeConnection()
    runtime = RemoteRuntime(client_id=client_id, registry=FakeRegistry(connection))
    runtime_window = RuntimeWindow(session_id="remote-session", window_id="remote-window")

    await runtime.resize(runtime_window, cols=120, rows=40, local_window_id=window_id)

    assert len(connection.sent) == 1
    message = connection.sent[0]
    assert message.type == "terminal_resize"
    assert message.client_id == client_id
    assert message.window_id == window_id
    assert message.payload == {"cols": 120, "rows": 40}


@pytest.mark.asyncio
async def test_resize_ignores_repeated_dimensions_for_same_window() -> None:
    client_id = uuid4()
    window_id = uuid4()
    connection = FakeConnection()
    runtime = RemoteRuntime(client_id=client_id, registry=FakeRegistry(connection))
    runtime_window = RuntimeWindow(session_id="remote-session", window_id="remote-window")

    await runtime.resize(runtime_window, cols=120, rows=40, local_window_id=window_id)
    await runtime.resize(runtime_window, cols=120, rows=40, local_window_id=window_id)
    await runtime.resize(runtime_window, cols=121, rows=40, local_window_id=window_id)

    assert [message.payload for message in connection.sent] == [
        {"cols": 120, "rows": 40},
        {"cols": 121, "rows": 40},
    ]
