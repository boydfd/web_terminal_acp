import asyncio
import base64
from contextlib import contextmanager
import json
import threading
import time
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.websockets import WebSocketDisconnect

from app.db import Base
from app.main import app
from app.routers import client_agent as client_agent_router
from app.models import AiSession, ClientRuntime, ClientStatus, Event, EventSourceType, VirtualWindow, WindowStatus
from app.repositories.clients import create_client, get_client
from app.repositories.windows import create_window
from app.services.runtime.broker import TerminalBroker
from app.services.runtime.client_connections import ClientConnectionRegistry
from app.services.runtime.protocol import AgentMessage, TerminalPayload, encode_agent_message


class ClientAgentDb:
    def __init__(self, session_factory: async_sessionmaker, client_id: UUID, token: str):
        self.session_factory = session_factory
        self.client_id = client_id
        self.token = token


class FakeElasticsearch:
    def __init__(self, is_committed=None) -> None:
        self.indexed_documents = []
        self.is_committed = is_committed

    async def index(self, **kwargs):
        if self.is_committed is not None:
            assert self.is_committed()
        self.indexed_documents.append(kwargs)
        return {"result": "created"}


class FakeTerminalBroker:
    def __init__(self, *, block_publish: threading.Event | None = None) -> None:
        self.published: list[tuple[UUID, UUID, bytes]] = []
        self.cleared_clients: list[tuple[UUID, str | None]] = []
        self.publish_started = threading.Event()
        self._block_publish = block_publish

    async def publish_output(self, client_id: UUID, window_id: UUID, data: bytes) -> None:
        self.publish_started.set()
        if self._block_publish is not None:
            await asyncio.to_thread(self._block_publish.wait)
        self.published.append((client_id, window_id, data))

    async def publish_status(self, client_id: UUID, window_id: UUID, message: str) -> None:
        return None

    async def clear_client(self, client_id: UUID, *, status_message: str | None = None) -> None:
        self.cleared_clients.append((client_id, status_message))


async def create_remote_window(session_factory: async_sessionmaker, client_id: UUID):
    async with session_factory() as session:
        window = await create_window(session, client_id, cwd="/tmp", shell_command="/bin/bash")
        await session.commit()
        return window


async def _event_count(session_factory: async_sessionmaker) -> int:
    async with session_factory() as session:
        return len((await session.execute(select(Event))).scalars().all())


@contextmanager
def connect_client_agent_bulk(test_client: TestClient, client_agent_db: ClientAgentDb):
    with test_client.websocket_connect(
        "/api/client-agent/bulk-ws",
        headers={
            "X-Client-Id": str(client_agent_db.client_id),
            "Authorization": f"Bearer {client_agent_db.token}",
        },
    ) as websocket:
        websocket.send_text(
            encode_agent_message(
                AgentMessage(type="bulk_hello", client_id=client_agent_db.client_id)
            )
        )
        response = websocket.receive_json()
        assert response["type"] == "bulk_hello_ack"
        assert response["client_id"] == str(client_agent_db.client_id)
        assert response.get("request_id") is None
        assert response.get("payload", {}) == {}
        yield websocket


def wait_for_condition(predicate, *, timeout: float = 2.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError("condition was not met before timeout")


def command_marker(window_id: UUID, command: str, *, sequence: int = 1) -> bytes:
    payload = {
        "command": command,
        "shell": "bash",
        "cwd": "/tmp",
        "captured_at": "2026-05-24T00:00:00+00:00",
        "sequence": sequence,
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return (
        f"\x1b]777;web-terminal-command;window_id={window_id};payload={encoded}\x07"
    ).encode("ascii")


@pytest.fixture
def client_agent_db(tmp_path, monkeypatch):
    database_path = tmp_path / "client_agent.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> tuple[UUID, str]:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            client, token = await create_client(
                session,
                name="remote",
                runtime=ClientRuntime.remote,
            )
            await session.commit()
            return client.id, token

    client_id, token = asyncio.run(setup())

    monkeypatch.setattr(client_agent_router, "SessionLocal", session_factory)
    app.state.client_connections = ClientConnectionRegistry()
    app.state.terminal_broker = FakeTerminalBroker()

    async def skip_disconnect_db_write(client_id: UUID) -> bool:
        return True

    monkeypatch.setattr(
        client_agent_router,
        "_mark_client_disconnected_by_id",
        skip_disconnect_db_write,
    )
    try:
        yield ClientAgentDb(session_factory, client_id, token)
    finally:
        asyncio.run(engine.dispose())


def test_client_agent_websocket_accepts_valid_client_and_acks_hello(client_agent_db):
    test_client = TestClient(app)
    try:
        with test_client.websocket_connect(
            "/api/client-agent/ws",
            headers={
                "X-Client-Id": str(client_agent_db.client_id),
                "Authorization": f"Bearer {client_agent_db.token}",
            },
        ) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(type="hello", client_id=client_agent_db.client_id)
                )
            )

            response = websocket.receive_json()
    finally:
        test_client.close()

    assert response["type"] == "hello_ack"
    assert response["client_id"] == str(client_agent_db.client_id)


def test_client_agent_websocket_rejects_invalid_token(client_agent_db):
    test_client = TestClient(app)
    try:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with test_client.websocket_connect(
                "/api/client-agent/ws",
                headers={
                    "X-Client-Id": str(client_agent_db.client_id),
                    "Authorization": "Bearer wrong-token",
                },
            ):
                pass
    finally:
        test_client.close()

    assert exc_info.value.code == 1008


def test_client_agent_bulk_websocket_does_not_start_workers_before_valid_hello(
    client_agent_db, monkeypatch
):
    worker_creations: list[str] = []

    def record_worker_creation(**kwargs):  # noqa: ANN003
        worker_creations.append(kwargs["queue_name"])

        async def parked_worker() -> None:
            await asyncio.sleep(60)

        return parked_worker()

    monkeypatch.setattr(
        client_agent_router,
        "_client_agent_message_worker",
        record_worker_creation,
    )
    test_client = TestClient(app)
    try:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with test_client.websocket_connect(
                "/api/client-agent/bulk-ws",
                headers={
                    "X-Client-Id": str(client_agent_db.client_id),
                    "Authorization": f"Bearer {client_agent_db.token}",
                },
            ) as websocket:
                websocket.send_text(
                    encode_agent_message(
                        AgentMessage(type="hello", client_id=client_agent_db.client_id)
                    )
                )
                websocket.receive_json()
    finally:
        test_client.close()

    assert exc_info.value.code == 1003
    assert worker_creations == []


def test_client_agent_websocket_heartbeat_marks_client_online(client_agent_db):
    test_client = TestClient(app)
    try:
        with test_client.websocket_connect(
            "/api/client-agent/ws",
            headers={
                "X-Client-Id": str(client_agent_db.client_id),
                "Authorization": f"Bearer {client_agent_db.token}",
            },
        ) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(type="heartbeat", client_id=client_agent_db.client_id)
                )
            )
            response = websocket.receive_json()
    finally:
        test_client.close()

    async def load_client():
        async with client_agent_db.session_factory() as session:
            return await get_client(session, client_agent_db.client_id)

    db_client = asyncio.run(load_client())
    assert response["type"] == "heartbeat_ack"
    assert db_client is not None
    assert db_client.status is ClientStatus.ONLINE
    assert db_client.last_seen_at is not None
    assert db_client.connected_at is not None


def test_client_agent_websocket_hello_records_client_version(client_agent_db):
    test_client = TestClient(app)
    try:
        with test_client.websocket_connect(
            "/api/client-agent/ws",
            headers={
                "X-Client-Id": str(client_agent_db.client_id),
                "Authorization": f"Bearer {client_agent_db.token}",
            },
        ) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="hello",
                        client_id=client_agent_db.client_id,
                        payload={"hostname": "edge-host", "version": "0.2.3"},
                    )
                )
            )
            response = websocket.receive_json()
    finally:
        test_client.close()

    async def load_client():
        async with client_agent_db.session_factory() as session:
            return await get_client(session, client_agent_db.client_id)

    db_client = asyncio.run(load_client())
    assert response["type"] == "hello_ack"
    assert db_client is not None
    assert db_client.hostname == "edge-host"
    assert db_client.version == "0.2.3"


def test_client_agent_websocket_reconciles_inventory_and_marks_client_online(client_agent_db):
    async def create_disconnected_window():
        async with client_agent_db.session_factory() as session:
            client = await get_client(session, client_agent_db.client_id)
            assert client is not None
            client.status = ClientStatus.OFFLINE
            client.last_seen_at = None
            window = await create_window(
                session,
                client_agent_db.client_id,
                cwd="/tmp",
                shell_command="/bin/bash",
                remote_session_id="agent-session",
                remote_window_id="@7",
            )
            window.status = WindowStatus.disconnected
            await session.commit()
            return window.id

    window_id = asyncio.run(create_disconnected_window())

    test_client = TestClient(app)
    try:
        with test_client.websocket_connect(
            "/api/client-agent/ws",
            headers={
                "X-Client-Id": str(client_agent_db.client_id),
                "Authorization": f"Bearer {client_agent_db.token}",
            },
        ) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="inventory",
                        client_id=client_agent_db.client_id,
                        payload={
                            "tmux_windows": [
                                {
                                    "local_window_id": str(window_id),
                                    "remote_session_id": "agent-session",
                                    "remote_window_id": "@7",
                                }
                            ]
                        },
                    )
                )
            )
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(type="heartbeat", client_id=client_agent_db.client_id)
                )
            )
            response = websocket.receive_json()
    finally:
        test_client.close()

    async def load_state():
        async with client_agent_db.session_factory() as session:
            client = await get_client(session, client_agent_db.client_id)
            window = await session.get(VirtualWindow, window_id)
            return client, window

    db_client, window = asyncio.run(load_state())
    assert response["type"] == "heartbeat_ack"
    assert db_client is not None
    assert db_client.status is ClientStatus.ONLINE
    assert db_client.last_seen_at is not None
    assert window is not None
    assert window.status is WindowStatus.active


def test_client_agent_websocket_persists_and_indexes_claude_ai_event(client_agent_db, monkeypatch):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    committed_before_index = False

    async def observe_commit(session):  # noqa: ANN001
        nonlocal committed_before_index
        committed_before_index = await _event_count(client_agent_db.session_factory) == 1
        await session.commit()

    monkeypatch.setattr(client_agent_router, "_commit_session", observe_commit)
    es_client = FakeElasticsearch(is_committed=lambda: committed_before_index)
    monkeypatch.setattr(app.state, "es_client", es_client, raising=False)
    monkeypatch.setattr(app.state, "es_indexes_ready", True, raising=False)
    event_payload = {
        "type": "assistant",
        "message": {"content": "managed hello"},
        "sessionId": "claude-managed-session-1",
        "WEB_TERMINAL_CLIENT_ID": str(client_agent_db.client_id),
        "WEB_TERMINAL_WINDOW_ID": str(window.id),
        "WEB_TERMINAL_PROJECT_PATH": "/workspace/claude-project",
    }

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="ai_event",
                        client_id=client_agent_db.client_id,
                        window_id=window.id,
                        request_id="request-claude-1",
                        payload={
                            "provider": "claude",
                            "source_path": "/home/user/.claude/session.jsonl",
                            "offset": 11,
                            "payload": event_payload,
                        },
                    )
                )
            )
            response = websocket.receive_json()
    finally:
        test_client.close()

    async def load_events():
        async with client_agent_db.session_factory() as session:
            rows = (await session.execute(select(Event))).scalars().all()
            ai_sessions = (await session.execute(select(AiSession))).scalars().all()
            return rows, ai_sessions

    rows, ai_sessions = asyncio.run(load_events())
    assert response == {
        "type": "ai_event_ack",
        "client_id": str(client_agent_db.client_id),
        "window_id": str(window.id),
        "request_id": "request-claude-1",
        "payload": {"ok": True},
    }
    assert len(rows) == 1
    assert rows[0].client_id == client_agent_db.client_id
    assert rows[0].virtual_window_id == window.id
    assert rows[0].source_type is EventSourceType.agent_tool_record
    assert rows[0].source_id == "claude-managed-session-1"
    assert rows[0].indexed_at is not None
    assert len(ai_sessions) == 1
    assert ai_sessions[0].provider == "claude_code"
    assert ai_sessions[0].source_id == "claude-managed-session-1"
    assert ai_sessions[0].source_path == "/home/user/.claude/session.jsonl"
    assert ai_sessions[0].project_path == "/workspace/claude-project"
    assert ai_sessions[0].virtual_window_id == window.id
    assert es_client.indexed_documents[0]["document"]["client_id"] == str(client_agent_db.client_id)
    assert es_client.indexed_documents[0]["document"]["virtual_window_id"] == str(window.id)
    assert es_client.indexed_documents[0]["document"]["provider"] == "claude_code"
    assert es_client.indexed_documents[0]["document"]["session_id"] == "claude-managed-session-1"


def test_client_agent_websocket_persists_codex_ai_event(client_agent_db):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    event_payload = {
        "trace_id": "trace-managed-1",
        "span": {"name": "tool_call", "attributes": {"tool": "bash"}},
        "client_id": str(client_agent_db.client_id),
        "virtual_window_id": str(window.id),
        "source_path": "/home/user/.codex/trace.jsonl",
        "project_path": "/workspace/codex-project-from-payload",
    }

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="ai_event",
                        client_id=client_agent_db.client_id,
                        window_id=window.id,
                        request_id="request-codex-1",
                        payload={
                            "provider": "codex",
                            "project_path": "/workspace/codex-project",
                            "payload": event_payload,
                        },
                    )
                )
            )
            response = websocket.receive_json()
    finally:
        test_client.close()

    async def load_events():
        async with client_agent_db.session_factory() as session:
            rows = (await session.execute(select(Event))).scalars().all()
            ai_sessions = (await session.execute(select(AiSession))).scalars().all()
            return rows, ai_sessions

    rows, ai_sessions = asyncio.run(load_events())
    assert response["type"] == "ai_event_ack"
    assert response["request_id"] == "request-codex-1"
    assert response["payload"] == {"ok": True}
    assert len(rows) == 1
    assert rows[0].client_id == client_agent_db.client_id
    assert rows[0].virtual_window_id == window.id
    assert rows[0].source_type is EventSourceType.agent_tool_record
    assert rows[0].source_id == "trace-managed-1"
    assert len(ai_sessions) == 1
    assert ai_sessions[0].provider == "codex"
    assert ai_sessions[0].source_id == "trace-managed-1"
    assert ai_sessions[0].source_path == "/home/user/.codex/trace.jsonl"
    assert ai_sessions[0].project_path == "/workspace/codex-project"
    assert ai_sessions[0].virtual_window_id == window.id


def test_bulk_terminal_output_display_worker_keeps_publishing_while_recording_is_backlogged(
    client_agent_db, monkeypatch
):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    broker = FakeTerminalBroker()
    recording_started = threading.Event()
    release_recording = threading.Event()

    async def slow_record_terminal_output_chunk(*args, **kwargs):
        recording_started.set()
        await asyncio.to_thread(release_recording.wait)
        return None

    monkeypatch.setattr(app.state, "terminal_broker", broker, raising=False)
    monkeypatch.setattr(
        client_agent_router,
        "record_terminal_output_chunk",
        slow_record_terminal_output_chunk,
    )

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            try:
                websocket.send_text(
                    encode_agent_message(
                        AgentMessage(
                            type="terminal_output",
                            client_id=client_agent_db.client_id,
                            window_id=window.id,
                            payload=TerminalPayload.from_bytes(window.id, b"first\n").model_dump(),
                        )
                    )
                )
                wait_for_condition(recording_started.is_set)
                websocket.send_text(
                    encode_agent_message(
                        AgentMessage(
                            type="terminal_output",
                            client_id=client_agent_db.client_id,
                            window_id=window.id,
                            payload=TerminalPayload.from_bytes(window.id, b"second\n").model_dump(),
                        )
                    )
                )
                wait_for_condition(
                    lambda: broker.published
                    == [
                        (client_agent_db.client_id, window.id, b"first\n"),
                        (client_agent_db.client_id, window.id, b"second\n"),
                    ]
                )
                assert not release_recording.is_set()
            finally:
                release_recording.set()
    finally:
        test_client.close()


def test_bulk_terminal_output_publishes_before_recording_completes(client_agent_db, monkeypatch):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    broker = FakeTerminalBroker()
    recording_started = threading.Event()
    release_recording = threading.Event()

    async def slow_record_terminal_output_chunk(*args, **kwargs):
        recording_started.set()
        await asyncio.to_thread(release_recording.wait)
        return None

    monkeypatch.setattr(app.state, "terminal_broker", broker, raising=False)
    monkeypatch.setattr(
        client_agent_router,
        "record_terminal_output_chunk",
        slow_record_terminal_output_chunk,
    )

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            try:
                websocket.send_text(
                    encode_agent_message(
                        AgentMessage(
                            type="terminal_output",
                            client_id=client_agent_db.client_id,
                            window_id=window.id,
                            payload=TerminalPayload.from_bytes(window.id, b"visible first\n").model_dump(),
                        )
                    )
                )
                wait_for_condition(
                    lambda: broker.published == [(client_agent_db.client_id, window.id, b"visible first\n")]
                )
                wait_for_condition(recording_started.is_set)
                assert broker.published == [(client_agent_db.client_id, window.id, b"visible first\n")]
                assert not release_recording.is_set()
            finally:
                release_recording.set()
    finally:
        test_client.close()



def test_client_agent_bulk_websocket_records_marker_only_terminal_command(client_agent_db, monkeypatch):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    broker = FakeTerminalBroker()
    monkeypatch.setattr(app.state, "terminal_broker", broker, raising=False)

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="terminal_output",
                        client_id=client_agent_db.client_id,
                        window_id=window.id,
                        payload=TerminalPayload.from_bytes(
                            window.id,
                            command_marker(window.id, "echo marker-only"),
                        ).model_dump(),
                    )
                )
            )
            wait_for_condition(lambda: asyncio.run(_event_count(client_agent_db.session_factory)) == 1)
    finally:
        test_client.close()

    async def load_events():
        async with client_agent_db.session_factory() as session:
            return (await session.execute(select(Event))).scalars().all()

    rows = asyncio.run(load_events())
    assert broker.published == []
    assert len(rows) == 1
    assert rows[0].kind == "terminal_input_command"
    assert rows[0].virtual_window_id == window.id
    assert rows[0].payload_json["command"] == "echo marker-only"



def test_client_agent_websocket_records_and_publishes_terminal_output(client_agent_db, monkeypatch):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    broker = FakeTerminalBroker()
    monkeypatch.setattr(app.state, "terminal_broker", broker, raising=False)

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="terminal_output",
                        client_id=client_agent_db.client_id,
                        window_id=window.id,
                        payload=TerminalPayload.from_bytes(window.id, b"remote output\n").model_dump(),
                    )
                )
            )
            wait_for_condition(
                lambda: len(broker.published) == 1
                and asyncio.run(_event_count(client_agent_db.session_factory)) == 1
            )
    finally:
        test_client.close()

    async def load_terminal_events():
        async with client_agent_db.session_factory() as session:
            return (await session.execute(select(Event))).scalars().all()

    rows = asyncio.run(load_terminal_events())
    assert broker.published == [(client_agent_db.client_id, window.id, b"remote output\n")]
    assert len(rows) == 1
    assert rows[0].client_id == client_agent_db.client_id
    assert rows[0].virtual_window_id == window.id
    assert rows[0].source_type is EventSourceType.terminal
    assert rows[0].payload_json == {"text": "remote output\n"}


def test_client_agent_websocket_publishes_terminal_snapshot_without_recording(client_agent_db, monkeypatch):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    broker = FakeTerminalBroker()
    monkeypatch.setattr(app.state, "terminal_broker", broker, raising=False)
    payload = TerminalPayload.from_bytes(window.id, b"remote prompt$ ").model_dump()
    payload["is_snapshot"] = True

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="terminal_output",
                        client_id=client_agent_db.client_id,
                        window_id=window.id,
                        payload=payload,
                    )
                )
            )
            wait_for_condition(lambda: len(broker.published) == 1)
    finally:
        test_client.close()

    async def load_terminal_events():
        async with client_agent_db.session_factory() as session:
            return (await session.execute(select(Event))).scalars().all()

    rows = asyncio.run(load_terminal_events())
    assert broker.published == [(client_agent_db.client_id, window.id, b"remote prompt$ ")]
    assert rows == []


def test_client_agent_websocket_records_terminal_output_when_browser_subscriber_fails(
    client_agent_db, monkeypatch
):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    broker = TerminalBroker()
    failures: list[bytes] = []

    async def failing_sender(data: bytes) -> None:
        failures.append(data)
        raise RuntimeError("stale browser websocket")

    asyncio.run(broker.subscribe(client_agent_db.client_id, window.id, failing_sender))
    monkeypatch.setattr(app.state, "terminal_broker", broker, raising=False)

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="terminal_output",
                        client_id=client_agent_db.client_id,
                        window_id=window.id,
                        payload=TerminalPayload.from_bytes(window.id, b"remote output\n").model_dump(),
                    )
                )
            )
            wait_for_condition(lambda: len(failures) == 1)
    finally:
        test_client.close()

    async def load_terminal_events():
        async with client_agent_db.session_factory() as session:
            return (await session.execute(select(Event))).scalars().all()

    rows = asyncio.run(load_terminal_events())
    assert failures == [b"remote output\n"]
    assert len(rows) == 1
    assert rows[0].client_id == client_agent_db.client_id
    assert rows[0].virtual_window_id == window.id
    assert rows[0].source_type is EventSourceType.terminal
    assert rows[0].payload_json == {"text": "remote output\n"}


def test_client_agent_websocket_rejects_cross_client_terminal_output(client_agent_db, monkeypatch):
    async def create_other_client_window():
        async with client_agent_db.session_factory() as session:
            other_client, _token = await create_client(
                session, name="other-terminal", runtime=ClientRuntime.remote
            )
            other_window = await create_window(session, other_client.id, cwd="/tmp", shell_command="/bin/bash")
            await session.commit()
            return other_window

    other_window = asyncio.run(create_other_client_window())
    broker = FakeTerminalBroker()
    monkeypatch.setattr(app.state, "terminal_broker", broker, raising=False)

    async def count_events():
        async with client_agent_db.session_factory() as session:
            return len((await session.execute(select(Event))).scalars().all())

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="terminal_output",
                        client_id=client_agent_db.client_id,
                        window_id=other_window.id,
                        payload=TerminalPayload.from_bytes(other_window.id, b"wrong client\n").model_dump(),
                    )
                )
            )
    finally:
        test_client.close()

    assert broker.published == []
    assert asyncio.run(count_events()) == 0


def test_client_agent_websocket_rejects_ai_event_without_message_window_id(client_agent_db):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    payload = {
        "type": "assistant",
        "WEB_TERMINAL_CLIENT_ID": str(client_agent_db.client_id),
        "WEB_TERMINAL_WINDOW_ID": str(window.id),
    }

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="ai_event",
                        client_id=client_agent_db.client_id,
                        request_id="missing-window",
                        payload={"provider": "claude", "payload": payload},
                    )
                )
            )
            response = websocket.receive_json()
    finally:
        test_client.close()

    assert response["type"] == "ai_event_ack"
    assert response["request_id"] == "missing-window"
    assert response["payload"]["ok"] is False
    assert "window_id is required" in response["payload"]["error"]


def test_client_agent_websocket_rejects_cross_client_ai_event_window(client_agent_db):
    async def create_other_client_window():
        async with client_agent_db.session_factory() as session:
            other_client, _token = await create_client(
                session, name="other", runtime=ClientRuntime.remote
            )
            other_window = await create_window(session, other_client.id, cwd="/tmp", shell_command="/bin/bash")
            await session.commit()
            return other_window

    other_window = asyncio.run(create_other_client_window())
    payload = {
        "type": "assistant",
        "WEB_TERMINAL_CLIENT_ID": str(client_agent_db.client_id),
        "WEB_TERMINAL_WINDOW_ID": str(other_window.id),
    }

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="ai_event",
                        client_id=client_agent_db.client_id,
                        window_id=other_window.id,
                        request_id="wrong-client-window",
                        payload={"provider": "claude", "payload": payload},
                    )
                )
            )
            response = websocket.receive_json()
    finally:
        test_client.close()

    async def count_events():
        async with client_agent_db.session_factory() as session:
            return len((await session.execute(select(Event))).scalars().all())

    assert response["type"] == "ai_event_ack"
    assert response["request_id"] == "wrong-client-window"
    assert response["payload"]["ok"] is False
    assert "window not found" in response["payload"]["error"]
    assert asyncio.run(count_events()) == 0


def test_client_agent_bulk_websocket_accepts_agent_work_presence(client_agent_db):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    broker = app.state.terminal_broker
    broker.published.clear()
    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as bulk_websocket:
            bulk_websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="agent_work_presence",
                        client_id=client_agent_db.client_id,
                        window_id=window.id,
                        payload={"providers": ["codex"], "reasons": ["tool_running"]},
                    )
                )
            )
            wait_for_condition(lambda: asyncio.run(_event_count(client_agent_db.session_factory)) == 1)

            bulk_websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="terminal_output",
                        client_id=client_agent_db.client_id,
                        window_id=window.id,
                        payload=TerminalPayload.from_bytes(window.id, b"still connected\n").model_dump(),
                    )
                )
            )
            wait_for_condition(
                lambda: broker.published == [(client_agent_db.client_id, window.id, b"still connected\n")]
            )
    finally:
        test_client.close()

    assert asyncio.run(_event_count(client_agent_db.session_factory)) == 1


def test_control_websocket_rejects_bulk_messages(client_agent_db):
    test_client = TestClient(app)
    try:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with test_client.websocket_connect(
                "/api/client-agent/ws",
                headers={
                    "X-Client-Id": str(client_agent_db.client_id),
                    "Authorization": f"Bearer {client_agent_db.token}",
                },
            ) as websocket:
                websocket.send_text(
                    encode_agent_message(
                        AgentMessage(type="terminal_output", client_id=client_agent_db.client_id)
                    )
                )
                websocket.receive_json()
    finally:
        test_client.close()

    assert exc_info.value.code == 1003


def test_control_websocket_responds_while_bulk_terminal_output_handler_is_blocked(
    client_agent_db, monkeypatch
):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    release_publish = threading.Event()
    broker = FakeTerminalBroker(block_publish=release_publish)
    monkeypatch.setattr(app.state, "terminal_broker", broker, raising=False)

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as bulk_websocket:
            with test_client.websocket_connect(
                "/api/client-agent/ws",
                headers={
                    "X-Client-Id": str(client_agent_db.client_id),
                    "Authorization": f"Bearer {client_agent_db.token}",
                },
            ) as control_websocket:
                bulk_websocket.send_text(
                    encode_agent_message(
                        AgentMessage(
                            type="terminal_output",
                            client_id=client_agent_db.client_id,
                            window_id=window.id,
                            payload=TerminalPayload.from_bytes(window.id, b"blocked output\n").model_dump(),
                        )
                    )
                )
                assert broker.publish_started.wait(timeout=2.0)

                control_websocket.send_text(
                    encode_agent_message(
                        AgentMessage(type="heartbeat", client_id=client_agent_db.client_id)
                    )
                )
                response = control_websocket.receive_json()
                release_publish.set()
                wait_for_condition(lambda: len(broker.published) == 1)
    finally:
        release_publish.set()
        test_client.close()

    assert response["type"] == "heartbeat_ack"
    assert broker.published == [(client_agent_db.client_id, window.id, b"blocked output\n")]
