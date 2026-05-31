import asyncio
import contextlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import status
from fastapi.websockets import WebSocketDisconnect

from app.models import LOCAL_CLIENT_ID, VirtualWindow, WindowStatus
from app.routers import terminal
from app.routers.terminal import mark_window_active, mark_window_disconnected, mark_window_error
from app.services.runtime.types import RuntimeWindow


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def commit(self) -> None:
        return None


class FakeWebSocket:
    def __init__(self, messages=None) -> None:
        self.messages = list(messages or [])
        self.accepted = False
        self.closed: list[int] = []
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.app = SimpleNamespace(state=SimpleNamespace(es_indexes_ready=False, es_client=None))

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def receive(self):
        if not self.messages:
            raise WebSocketDisconnect(code=1000)
        return self.messages.pop(0)


class ExistingTmuxManager:
    async def has_window(self, target) -> bool:
        return True


class FakeBroker:
    def __init__(self) -> None:
        self.subscriptions = []
        self.unsubscriptions = []
        self.attachments = []
        self.inputs = []
        self.resizes = []
        self.registered = []
        self.published_output = []

    def register_runtime(self, client_id, runtime) -> None:
        self.registered.append((client_id, runtime))

    async def subscribe(self, client_id, window_id, sender, status_sender=None) -> None:
        self.subscriptions.append((client_id, window_id, sender, status_sender))

    async def unsubscribe(self, client_id, window_id, sender, status_sender=None) -> None:
        self.unsubscriptions.append((client_id, window_id, sender, status_sender))

    async def attach(
        self,
        client_id,
        window_id,
        runtime_window,
        output_callback=None,
        selection_callback=None,
        view_id=None,
    ):
        self.attachments.append(
            (client_id, window_id, runtime_window, output_callback, selection_callback, view_id)
        )
        return runtime_window

    async def send_input(self, client_id, window_id, runtime_window, data: bytes, view_id=None) -> None:
        self.inputs.append((client_id, window_id, runtime_window, data, view_id))

    async def resize(self, client_id, window_id, runtime_window, *, cols: int, rows: int, view_id=None) -> None:
        self.resizes.append((client_id, window_id, runtime_window, cols, rows, view_id))

    async def publish_output(self, client_id, window_id, data: bytes) -> None:
        self.published_output.append((client_id, window_id, data))

    async def publish_view_output(self, client_id, view_id, data: bytes) -> None:
        self.published_output.append((client_id, view_id, data))

    async def acknowledge_output(self, client_id, window_id, sender, bytes_acked=None) -> None:
        return None

    async def select_window(
        self,
        client_id,
        view_id,
        current_window_id,
        current_runtime_window,
        next_window_id,
        next_runtime_window,
    ):
        return next_runtime_window

    async def clear_client(self, client_id, *, status_message=None) -> None:
        return None


def test_mark_window_error_updates_window_status():
    window = VirtualWindow(title="Terminal", status=WindowStatus.active)

    mark_window_error(window)

    assert window.status is WindowStatus.error


def test_mark_window_active_updates_window_status():
    window = VirtualWindow(title="Terminal", status=WindowStatus.error)

    mark_window_active(window)

    assert window.status is WindowStatus.active


def test_mark_window_disconnected_updates_window_status():
    window = VirtualWindow(title="Terminal", status=WindowStatus.active)

    mark_window_disconnected(window)

    assert window.status is WindowStatus.disconnected


@pytest.mark.asyncio
async def test_scoped_terminal_route_rejects_window_for_different_client(monkeypatch) -> None:
    client_id = uuid4()
    window_id = uuid4()
    calls = []

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        calls.append((requested_client_id, requested_window_id))
        return None

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    websocket = FakeWebSocket()

    await terminal.terminal_websocket(websocket, client_id, window_id, tmux_manager=object())

    assert calls == [(client_id, window_id)]
    assert websocket.accepted is False
    assert websocket.closed == [status.WS_1008_POLICY_VIOLATION]


@pytest.mark.asyncio
async def test_scoped_terminal_route_reports_disconnected_local_window(monkeypatch) -> None:
    client_id = LOCAL_CLIENT_ID
    window_id = uuid4()
    window = VirtualWindow(
        id=window_id,
        client_id=client_id,
        title="Terminal",
        status=WindowStatus.disconnected,
        tmux_session="web-terminal",
        tmux_window_id="@7",
    )

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        assert requested_client_id == client_id
        assert requested_window_id == window_id
        return window

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    websocket = FakeWebSocket()

    await terminal.terminal_websocket(websocket, client_id, window_id, tmux_manager=object())

    assert websocket.accepted is True
    assert websocket.sent_text == [
        '{"type":"terminal_status","status":"unavailable","reason":"client_offline",'
        '"retry_after_ms":5000}'
    ]
    assert websocket.closed == [1000]


@pytest.mark.asyncio
async def test_scoped_terminal_route_reports_runtime_starting_window(monkeypatch) -> None:
    client_id = uuid4()
    window_id = uuid4()
    window = VirtualWindow(
        id=window_id,
        client_id=client_id,
        title="Terminal",
        status=WindowStatus.active,
    )

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        assert requested_client_id == client_id
        assert requested_window_id == window_id
        return window

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    websocket = FakeWebSocket()

    await terminal.terminal_websocket(websocket, client_id, window_id, tmux_manager=object())

    assert websocket.accepted is True
    assert websocket.sent_text == [
        '{"type":"terminal_status","status":"reconnecting","reason":"runtime_starting",'
        '"retry_after_ms":500}'
    ]
    assert websocket.closed == [1013]


@pytest.mark.asyncio
async def test_scoped_terminal_route_attaches_even_when_local_tmux_window_was_missing(monkeypatch) -> None:
    window_id = uuid4()
    window = VirtualWindow(
        id=window_id,
        client_id=LOCAL_CLIENT_ID,
        title="Terminal",
        status=WindowStatus.active,
        tmux_session="web-terminal",
        tmux_window_id="@7",
    )
    broker = FakeBroker()

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        assert requested_client_id == LOCAL_CLIENT_ID
        assert requested_window_id == window_id
        return window

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    monkeypatch.setattr(terminal, "_terminal_broker", lambda websocket, tmux_manager: broker)
    websocket = FakeWebSocket()

    await terminal.terminal_websocket(websocket, LOCAL_CLIENT_ID, window_id, tmux_manager=object())

    assert websocket.accepted is True
    assert websocket.sent_text == ['{"type":"terminal_status","status":"connected"}']
    assert websocket.closed == []
    assert window.status is WindowStatus.active
    assert broker.attachments == [
        (
            LOCAL_CLIENT_ID,
            window_id,
            RuntimeWindow(session_id="web-terminal", window_id="@7"),
            broker.attachments[0][3],
            broker.attachments[0][4],
            window_id,
        )
    ]


@pytest.mark.asyncio
async def test_scoped_terminal_route_recovers_disconnected_remote_window(monkeypatch) -> None:
    client_id = uuid4()
    window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="remote-session", window_id="@131")
    window = VirtualWindow(
        id=window_id,
        client_id=client_id,
        title="Terminal",
        status=WindowStatus.disconnected,
        remote_session_id=runtime_window.session_id,
        remote_window_id=runtime_window.window_id,
    )
    broker = FakeBroker()

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        assert requested_client_id == client_id
        assert requested_window_id == window_id
        return window

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    monkeypatch.setattr(terminal, "_terminal_broker", lambda websocket, tmux_manager: broker)
    websocket = FakeWebSocket()
    websocket.app.state.client_connections = SimpleNamespace(get=lambda requested_client_id: object())

    await terminal.terminal_websocket(websocket, client_id, window_id, tmux_manager=object())

    assert websocket.accepted is True
    assert websocket.sent_text == ['{"type":"terminal_status","status":"connected"}']
    assert window.status is WindowStatus.active
    assert broker.attachments == [(client_id, window_id, runtime_window, None, None, window_id)]
    assert websocket.closed == []


@pytest.mark.asyncio
async def test_scoped_terminal_route_attaches_local_runtime_and_routes_input(monkeypatch) -> None:
    window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="web-terminal", window_id="@7")
    window = VirtualWindow(
        id=window_id,
        client_id=LOCAL_CLIENT_ID,
        title="Terminal",
        status=WindowStatus.active,
        tmux_session=runtime_window.session_id,
        tmux_window_id=runtime_window.window_id,
    )
    broker = FakeBroker()

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        assert requested_client_id == LOCAL_CLIENT_ID
        assert requested_window_id == window_id
        return window

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    monkeypatch.setattr(terminal, "_terminal_broker", lambda websocket, tmux_manager: broker)
    websocket = FakeWebSocket(
        [
            {"bytes": b"whoami\n"},
            {"text": '{"type":"resize","cols":120,"rows":40}'},
            {"text": "pwd\n"},
        ]
    )

    await terminal.terminal_websocket(websocket, LOCAL_CLIENT_ID, window_id, tmux_manager=ExistingTmuxManager())

    assert websocket.accepted is True
    assert broker.subscriptions == [(LOCAL_CLIENT_ID, window_id, websocket.send_bytes, websocket.send_text)]
    assert broker.attachments == [
        (
            LOCAL_CLIENT_ID,
            window_id,
            runtime_window,
            broker.attachments[0][3],
            broker.attachments[0][4],
            window_id,
        )
    ]
    assert broker.attachments[0][4] is not None
    assert broker.inputs == [
        (LOCAL_CLIENT_ID, window_id, runtime_window, b"whoami\n", window_id),
        (LOCAL_CLIENT_ID, window_id, runtime_window, b"pwd\n", window_id),
    ]
    assert broker.resizes == [(LOCAL_CLIENT_ID, window_id, runtime_window, 120, 40, window_id)]
    assert websocket.sent_text == ['{"type":"terminal_status","status":"connected"}']
    assert broker.unsubscriptions == [(LOCAL_CLIENT_ID, window_id, websocket.send_bytes, websocket.send_text)]


@pytest.mark.asyncio
async def test_scoped_terminal_route_does_not_record_initial_attach_snapshot(monkeypatch) -> None:
    window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="web-terminal", window_id="@7")
    window = VirtualWindow(
        id=window_id,
        client_id=LOCAL_CLIENT_ID,
        title="Terminal",
        status=WindowStatus.active,
        tmux_session=runtime_window.session_id,
        tmux_window_id=runtime_window.window_id,
    )
    broker = FakeBroker()
    recorded_output: list[bytes] = []

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        return window

    async def fake_record_terminal_command_markers(session, client_id, target_window_id, commands):
        return []

    async def fake_record_terminal_output_chunk(session, client_id, target_window_id, data, es_client):
        recorded_output.append(data)

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    monkeypatch.setattr(terminal, "_terminal_broker", lambda websocket, tmux_manager: broker)
    monkeypatch.setattr(terminal, "record_terminal_command_markers", fake_record_terminal_command_markers)
    monkeypatch.setattr(terminal, "record_terminal_output_chunk", fake_record_terminal_output_chunk)
    monkeypatch.setattr(terminal, "ATTACH_SNAPSHOT_GRACE_SECONDS", 0.01)
    websocket = FakeWebSocket()

    await terminal.terminal_websocket(websocket, LOCAL_CLIENT_ID, window_id, tmux_manager=ExistingTmuxManager())

    output_callback = broker.attachments[0][3]
    await output_callback(b"old screen\n")
    await asyncio.sleep(0.02)
    await output_callback(b"live output\n")
    for _ in range(20):
        if recorded_output == [b"live output\n"]:
            break
        await asyncio.sleep(0.01)

    assert recorded_output == [b"live output\n"]
    assert broker.published_output == [
        (LOCAL_CLIENT_ID, window_id, b"old screen\n"),
        (LOCAL_CLIENT_ID, window_id, b"live output\n"),
    ]


@pytest.mark.asyncio
async def test_scoped_terminal_route_does_not_record_multi_chunk_initial_attach_snapshot(monkeypatch) -> None:
    window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="web-terminal", window_id="@7")
    window = VirtualWindow(
        id=window_id,
        client_id=LOCAL_CLIENT_ID,
        title="Terminal",
        status=WindowStatus.active,
        tmux_session=runtime_window.session_id,
        tmux_window_id=runtime_window.window_id,
    )
    broker = FakeBroker()
    recorded_output: list[bytes] = []

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        return window

    async def fake_record_terminal_command_markers(session, client_id, target_window_id, commands):
        return []

    async def fake_record_terminal_output_chunk(session, client_id, target_window_id, data, es_client):
        recorded_output.append(data)

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    monkeypatch.setattr(terminal, "_terminal_broker", lambda websocket, tmux_manager: broker)
    monkeypatch.setattr(terminal, "record_terminal_command_markers", fake_record_terminal_command_markers)
    monkeypatch.setattr(terminal, "record_terminal_output_chunk", fake_record_terminal_output_chunk)
    monkeypatch.setattr(terminal, "ATTACH_SNAPSHOT_GRACE_SECONDS", 0.01, raising=False)
    websocket = FakeWebSocket()

    await terminal.terminal_websocket(websocket, LOCAL_CLIENT_ID, window_id, tmux_manager=ExistingTmuxManager())

    output_callback = broker.attachments[0][3]
    await output_callback(b"\x1b[?25l\x1b[37C")
    await output_callback(b"\xe2\x94\x82\xc2\xb7\xc2\xb7\xc2\xb7")
    await asyncio.sleep(0.02)
    await output_callback(b"live output\n")
    for _ in range(20):
        if recorded_output == [b"live output\n"]:
            break
        await asyncio.sleep(0.01)

    assert recorded_output == [b"live output\n"]
    assert broker.published_output == [
        (LOCAL_CLIENT_ID, window_id, b"\x1b[?25l\x1b[37C"),
        (LOCAL_CLIENT_ID, window_id, b"\xe2\x94\x82\xc2\xb7\xc2\xb7\xc2\xb7"),
        (LOCAL_CLIENT_ID, window_id, b"live output\n"),
    ]


@pytest.mark.asyncio
async def test_local_terminal_batches_rapid_output_recording_without_delaying_display(monkeypatch) -> None:
    window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="web-terminal", window_id="@7")
    window = VirtualWindow(
        id=window_id,
        client_id=LOCAL_CLIENT_ID,
        title="Terminal",
        status=WindowStatus.active,
        tmux_session=runtime_window.session_id,
        tmux_window_id=runtime_window.window_id,
    )
    broker = FakeBroker()
    recorded_output: list[bytes] = []

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        return window

    async def fake_record_terminal_command_markers(session, client_id, target_window_id, commands):
        return []

    async def fake_record_terminal_output_chunk(session, client_id, target_window_id, data, es_client):
        recorded_output.append(data)
        return object()

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    monkeypatch.setattr(terminal, "_terminal_broker", lambda websocket, tmux_manager: broker)
    monkeypatch.setattr(terminal, "record_terminal_command_markers", fake_record_terminal_command_markers)
    monkeypatch.setattr(terminal, "record_terminal_output_chunk", fake_record_terminal_output_chunk)
    monkeypatch.setattr(terminal, "ATTACH_SNAPSHOT_GRACE_SECONDS", -1.0, raising=False)
    websocket = FakeWebSocket()

    await terminal.terminal_websocket(websocket, LOCAL_CLIENT_ID, window_id, tmux_manager=ExistingTmuxManager())
    output_callback = broker.attachments[0][3]

    await output_callback(b"one")
    await output_callback(b"two")

    assert broker.published_output == [
        (LOCAL_CLIENT_ID, window_id, b"one"),
        (LOCAL_CLIENT_ID, window_id, b"two"),
    ]

    for _ in range(20):
        if recorded_output == [b"onetwo"]:
            break
        await asyncio.sleep(0.01)

    assert recorded_output == [b"onetwo"]


@pytest.mark.asyncio
async def test_local_terminal_records_output_before_slow_git_worktree_tracking(monkeypatch) -> None:
    window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="web-terminal", window_id="@7")
    window = VirtualWindow(
        id=window_id,
        client_id=LOCAL_CLIENT_ID,
        title="Terminal",
        status=WindowStatus.active,
        tmux_session=runtime_window.session_id,
        tmux_window_id=runtime_window.window_id,
    )
    broker = FakeBroker()
    ui_calls: list[tuple[str, list[str], str | None]] = []
    recorded_output: list[bytes] = []
    git_started = asyncio.Event()
    git_continue = asyncio.Event()
    git_done = asyncio.Event()

    class FakeMarkerExtractor:
        def feed(self, data: bytes):
            return (
                b"visible output\n",
                [
                    {
                        "window_id": str(window_id),
                        "phase": "started",
                        "command": "echo hi",
                        "sequence": "1",
                    }
                ],
                [
                    {
                        "window_id": str(window_id),
                        "worktree_root": "/tmp/agent-worktree",
                        "main_repo_root": "/tmp/main",
                    }
                ],
            )

    class FakeUiHub:
        async def publish_invalidation(self, resources, *, reason=None, **kwargs):
            ui_calls.append(("invalidate", list(resources), reason))

        async def publish_debounced_invalidation(self, key, resources, *, reason=None, **kwargs):
            ui_calls.append(("debounced", list(resources), reason))

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        return window

    async def fake_record_terminal_command_markers(session, client_id, target_window_id, commands):
        return [object()]

    async def fake_record_terminal_output_chunk(session, client_id, target_window_id, data, es_client):
        recorded_output.append(data)
        return object()

    async def fake_process_worktree_registration(*args, **kwargs):
        git_started.set()
        await git_continue.wait()

    async def fake_process_git_worktree_snapshot_refresh(*args, **kwargs):
        git_done.set()
        return True

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    monkeypatch.setattr(terminal, "_terminal_broker", lambda websocket, tmux_manager: broker)
    monkeypatch.setattr(terminal, "_ui_event_hub", lambda websocket: FakeUiHub())
    monkeypatch.setattr(terminal, "TerminalStreamMarkerExtractor", FakeMarkerExtractor)
    monkeypatch.setattr(terminal, "record_terminal_command_markers", fake_record_terminal_command_markers)
    monkeypatch.setattr(terminal, "record_terminal_output_chunk", fake_record_terminal_output_chunk)
    monkeypatch.setattr(terminal, "process_worktree_registration", fake_process_worktree_registration)
    monkeypatch.setattr(
        terminal,
        "process_git_worktree_snapshot_refresh",
        fake_process_git_worktree_snapshot_refresh,
    )
    monkeypatch.setattr(terminal, "ATTACH_SNAPSHOT_GRACE_SECONDS", -1.0, raising=False)
    websocket = FakeWebSocket()

    await terminal.terminal_websocket(websocket, LOCAL_CLIENT_ID, window_id, tmux_manager=ExistingTmuxManager())
    output_callback = broker.attachments[0][3]

    try:
        await output_callback(b"raw output")
        await asyncio.wait_for(git_started.wait(), timeout=1)

        assert recorded_output == [b"visible output\n"]
        assert (
            "invalidate",
            ["agent_record", "command_history", "window", "search"],
            "terminal_command",
        ) in ui_calls
        assert ("debounced", ["window", "search"], "terminal_output") in ui_calls
        assert git_done.is_set() is False
    finally:
        git_continue.set()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(git_done.wait(), timeout=1)


@pytest.mark.asyncio
async def test_scoped_terminal_route_marks_error_window_active_after_successful_attach(monkeypatch) -> None:
    window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="web-terminal", window_id="@7")
    window = VirtualWindow(
        id=window_id,
        client_id=LOCAL_CLIENT_ID,
        title="Terminal",
        status=WindowStatus.error,
        tmux_session=runtime_window.session_id,
        tmux_window_id=runtime_window.window_id,
    )
    broker = FakeBroker()

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        assert requested_client_id == LOCAL_CLIENT_ID
        assert requested_window_id == window_id
        return window

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    monkeypatch.setattr(terminal, "_terminal_broker", lambda websocket, tmux_manager: broker)
    websocket = FakeWebSocket()

    await terminal.terminal_websocket(websocket, LOCAL_CLIENT_ID, window_id, tmux_manager=ExistingTmuxManager())

    assert websocket.accepted is True
    assert websocket.sent_text == ['{"type":"terminal_status","status":"connected"}']
    assert window.status is WindowStatus.active
    assert broker.attachments == [
        (
            LOCAL_CLIENT_ID,
            window_id,
            runtime_window,
            broker.attachments[0][3],
            broker.attachments[0][4],
            window_id,
        )
    ]


@pytest.mark.asyncio
async def test_scoped_terminal_route_routes_remote_window_input_and_resize(monkeypatch) -> None:
    client_id = uuid4()
    window_id = uuid4()
    runtime_window = RuntimeWindow(session_id="remote-session", window_id="remote-window")
    window = VirtualWindow(
        id=window_id,
        client_id=client_id,
        title="Terminal",
        status=WindowStatus.active,
        remote_session_id=runtime_window.session_id,
        remote_window_id=runtime_window.window_id,
    )
    broker = FakeBroker()

    async def fake_get_window_for_client(session, requested_client_id, requested_window_id):
        assert requested_client_id == client_id
        assert requested_window_id == window_id
        return window

    monkeypatch.setattr(terminal, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(terminal, "get_window_for_client", fake_get_window_for_client)
    monkeypatch.setattr(terminal, "_terminal_broker", lambda websocket, tmux_manager: broker)
    websocket = FakeWebSocket(
        [
            {"text": "ls\n"},
            {"text": '{"type":"resize","cols":100,"rows":28}'},
        ]
    )
    websocket.app.state.client_connections = SimpleNamespace(get=lambda requested_client_id: object())

    await terminal.terminal_websocket(websocket, client_id, window_id, tmux_manager=object())

    assert websocket.accepted is True
    assert websocket.sent_text == ['{"type":"terminal_status","status":"connected"}']
    assert broker.attachments == [(client_id, window_id, runtime_window, None, None, window_id)]
    assert broker.inputs == [(client_id, window_id, runtime_window, b"ls\n", window_id)]
    assert broker.resizes == [(client_id, window_id, runtime_window, 100, 28, window_id)]
    assert broker.unsubscriptions == [(client_id, window_id, websocket.send_bytes, websocket.send_text)]
