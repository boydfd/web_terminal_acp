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
from app.models import (
    AiSession,
    Client,
    ClientRuntime,
    ClientStatus,
    Event,
    EventSourceType,
    GitWorktreeRun,
    VirtualWindow,
    WindowGitBinding,
    WindowStatus,
)
from app.repositories.clients import create_client, get_client
from app.repositories.windows import create_window
from app.services import git_worktree_coordinator
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
        self.published.append((client_id, window_id, message.encode("utf-8")))

    async def clear_client(self, client_id: UUID, *, status_message: str | None = None) -> None:
        self.cleared_clients.append((client_id, status_message))


class CaptureUiEventHub:
    def __init__(self) -> None:
        self.invalidations: list[dict[str, object]] = []
        self.debounced_invalidations: list[dict[str, object]] = []

    async def publish_invalidation(
        self,
        resources,
        *,
        client_id=None,
        window_id=None,
        reason=None,
    ) -> None:
        self.invalidations.append(
            {
                "resources": list(resources),
                "client_id": client_id,
                "window_id": window_id,
                "reason": reason,
            }
        )

    async def publish_debounced_invalidation(
        self,
        key,
        resources,
        *,
        client_id=None,
        window_id=None,
        reason=None,
        delay_seconds=1.0,
    ) -> None:
        self.debounced_invalidations.append(
            {
                "key": key,
                "resources": list(resources),
                "client_id": client_id,
                "window_id": window_id,
                "reason": reason,
                "delay_seconds": delay_seconds,
            }
        )


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


def worktree_marker(
    window_id: UUID,
    *,
    worktree_root: str = "/repo/.worktrees/feature",
    main_repo_root: str = "/repo",
    branch: str = "agent/feature",
) -> str:
    payload = {
        "worktree_root": worktree_root,
        "main_repo_root": main_repo_root,
        "branch": branch,
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"\x1b]777;web-terminal-worktree;window_id={window_id};payload={encoded}\x07"


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


def test_client_agent_websocket_heartbeat_survives_seen_update_failure(
    client_agent_db,
    monkeypatch,
):
    async def fail_seen_update(client_id: UUID, payload: dict[str, object]) -> bool:
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(
        client_agent_router,
        "_mark_client_seen_with_metadata",
        fail_seen_update,
    )

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
            first_response = websocket.receive_json()
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(type="heartbeat", client_id=client_agent_db.client_id)
                )
            )
            second_response = websocket.receive_json()
    finally:
        test_client.close()

    assert first_response["type"] == "heartbeat_ack"
    assert second_response["type"] == "heartbeat_ack"


def test_client_agent_websocket_hello_survives_seen_update_failure(
    client_agent_db,
    monkeypatch,
):
    async def fail_seen_update(client_id: UUID, payload: dict[str, object]) -> bool:
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(
        client_agent_router,
        "_mark_client_seen_with_metadata",
        fail_seen_update,
    )

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
                        payload={"hostname": "edge-host", "version": "9.9.9"},
                    )
                )
            )
            response = websocket.receive_json()
    finally:
        test_client.close()

    assert response["type"] == "hello_ack"


def test_client_agent_websocket_disconnect_cleanup_survives_offline_update_failure(
    client_agent_db,
    monkeypatch,
):
    async def fail_offline_update(client_id: UUID) -> bool:
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(
        client_agent_router,
        "_mark_client_disconnected_by_id",
        fail_offline_update,
    )

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

    assert response["type"] == "heartbeat_ack"


def test_client_agent_websocket_inventory_survives_inventory_update_failure(
    client_agent_db,
    monkeypatch,
):
    async def fail_inventory_update(websocket, client_id: UUID, message: AgentMessage) -> bool:
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(
        client_agent_router,
        "_handle_inventory_message",
        fail_inventory_update,
    )

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
                        payload={"tmux_windows": []},
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

    assert response["type"] == "heartbeat_ack"


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
    ui_event_hub = CaptureUiEventHub()
    monkeypatch.setattr(app.state, "es_client", es_client, raising=False)
    monkeypatch.setattr(app.state, "es_indexes_ready", True, raising=False)
    monkeypatch.setattr(
        client_agent_router,
        "_ui_event_hub",
        lambda _websocket: ui_event_hub,
    )
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
    assert ui_event_hub.invalidations == [
        {
            "resources": ["agent_record", "window", "search"],
            "client_id": client_agent_db.client_id,
            "window_id": window.id,
            "reason": "ai_event",
        }
    ]


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


def test_client_agent_websocket_tracks_worktree_marker_from_codex_tool_output(
    client_agent_db,
):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    event_payload = {
        "trace_id": "trace-worktree-1",
        "name": "response_item",
        "type": "function_call_output",
        "output": f"Registered worktree\n{worktree_marker(window.id)}",
        "client_id": str(client_agent_db.client_id),
        "virtual_window_id": str(window.id),
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
                        request_id="request-worktree-1",
                        payload={"provider": "codex", "payload": event_payload},
                    )
                )
            )
            response = websocket.receive_json()
            assert response["type"] == "ai_event_ack"
            assert response["payload"] == {"ok": True}
            wait_for_condition(
                lambda: asyncio.run(_worktree_binding_exists(client_agent_db.session_factory, window.id))
            )
    finally:
        test_client.close()

    async def load_state():
        async with client_agent_db.session_factory() as session:
            event = await session.scalar(
                select(Event).where(Event.virtual_window_id == window.id)
            )
            binding = await session.scalar(
                select(WindowGitBinding).where(WindowGitBinding.virtual_window_id == window.id)
            )
            return event, binding

    event, binding = asyncio.run(load_state())
    assert event is not None
    assert event.source_type is EventSourceType.agent_tool_record
    assert binding is not None
    assert binding.worktree_root == "/repo/.worktrees/feature"
    assert binding.main_repo_root == "/repo"
    assert binding.discovery_method == "osc"


def test_client_agent_websocket_acks_worktree_ai_event_before_git_tracking(
    client_agent_db,
    monkeypatch,
):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    tracking_started = threading.Event()
    release_tracking = threading.Event()

    async def slow_process_worktree_registration(*args, **kwargs):
        tracking_started.set()
        await asyncio.to_thread(release_tracking.wait)

    monkeypatch.setattr(
        client_agent_router,
        "process_worktree_registration",
        slow_process_worktree_registration,
    )
    event_payload = {
        "trace_id": "trace-worktree-priority-1",
        "name": "response_item",
        "type": "function_call_output",
        "output": f"Registered worktree\n{worktree_marker(window.id)}",
        "client_id": str(client_agent_db.client_id),
        "virtual_window_id": str(window.id),
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
                        request_id="request-worktree-priority-1",
                        payload={"provider": "codex", "payload": event_payload},
                    )
                )
            )
            response = websocket.receive_json()
            assert response["type"] == "ai_event_ack"
            assert response["payload"] == {"ok": True}
            assert asyncio.run(_event_count(client_agent_db.session_factory)) == 1
            wait_for_condition(tracking_started.is_set)
            release_tracking.set()
    finally:
        release_tracking.set()
        test_client.close()


def test_client_agent_websocket_refreshes_git_tracking_after_codex_tool_output(
    client_agent_db,
    monkeypatch,
):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    asyncio.run(_set_client_runtime(client_agent_db.session_factory, client_agent_db.client_id, ClientRuntime.local))
    snapshots = [
        {
            "is_linked_worktree": True,
            "worktree_root": "/repo/.worktrees/feature",
            "main_repo_root": "/repo",
            "branch": "agent/feature",
            "head_sha": "base",
            "status_porcelain": "",
            "diff_stat": "",
            "staged_diff_stat": "",
            "commits": [],
        },
        {
            "is_linked_worktree": True,
            "worktree_root": "/repo/.worktrees/feature",
            "main_repo_root": "/repo",
            "branch": "agent/feature",
            "head_sha": "feature",
            "status_porcelain": "",
            "diff_stat": "",
            "staged_diff_stat": "",
            "commits": [
                {
                    "sha": "feature",
                    "short_sha": "feature",
                    "subject": "Fix terminal reload autofocus reconnect",
                    "author_name": "Open Claw",
                    "author_email": "open@example.com",
                    "authored_at": "2026-05-27T05:55:00+00:00",
                    "files": [
                        {
                            "path": "frontend/src/components/TerminalPane.tsx",
                            "old_path": None,
                            "status": "modified",
                            "additions": 12,
                            "deletions": 4,
                            "patch": "@@ -1 +1 @@\n-old\n+new\n",
                        }
                    ],
                }
            ],
        },
    ]

    async def fake_local_git_worktree_action(action: str, **payload):
        if action == "detect":
            return {
                "ok": True,
                "context": {
                    "is_linked_worktree": True,
                    "worktree_root": "/repo/.worktrees/feature",
                    "main_repo_root": "/repo",
                    "branch": "agent/feature",
                },
            }
        if action == "snapshot":
            snapshot = snapshots.pop(0) if len(snapshots) > 1 else snapshots[0]
            return {"ok": True, "snapshot": snapshot}
        return None

    monkeypatch.setattr(
        git_worktree_coordinator,
        "local_git_worktree_action",
        fake_local_git_worktree_action,
    )

    register_payload = {
        "trace_id": "trace-worktree-refresh-1",
        "name": "response_item",
        "type": "function_call_output",
        "output": f"Registered worktree\n{worktree_marker(window.id)}",
        "client_id": str(client_agent_db.client_id),
        "virtual_window_id": str(window.id),
    }
    tool_payload = {
        "trace_id": "trace-worktree-refresh-1",
        "name": "response_item",
        "type": "function_call_output",
        "output": "git commit completed",
        "client_id": str(client_agent_db.client_id),
        "virtual_window_id": str(window.id),
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
                        request_id="request-worktree-register",
                        payload={"provider": "codex", "payload": register_payload},
                    )
                )
            )
            response = websocket.receive_json()
            assert response["type"] == "ai_event_ack"
            wait_for_condition(
                lambda: asyncio.run(_worktree_binding_exists(client_agent_db.session_factory, window.id))
            )

            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="ai_event",
                        client_id=client_agent_db.client_id,
                        window_id=window.id,
                        request_id="request-worktree-refresh",
                        payload={"provider": "codex", "payload": tool_payload},
                    )
                )
            )
            response = websocket.receive_json()
            assert response["type"] == "ai_event_ack"
            assert response["payload"] == {"ok": True}
            wait_for_condition(
                lambda: asyncio.run(
                    _worktree_diff_contains_commit(client_agent_db.session_factory, window.id)
                )
            )
    finally:
        test_client.close()


async def _worktree_binding_exists(
    session_factory: async_sessionmaker,
    window_id: UUID,
) -> bool:
    async with session_factory() as session:
        binding = await session.scalar(
            select(WindowGitBinding).where(WindowGitBinding.virtual_window_id == window_id)
        )
        return binding is not None


async def _set_client_runtime(
    session_factory: async_sessionmaker,
    client_id: UUID,
    runtime: ClientRuntime,
) -> None:
    async with session_factory() as session:
        client = await session.get(Client, client_id)
        assert client is not None
        client.runtime = runtime
        await session.commit()


async def _worktree_diff_contains_commit(
    session_factory: async_sessionmaker,
    window_id: UUID,
) -> bool:
    async with session_factory() as session:
        run = await session.scalar(
            select(GitWorktreeRun).where(GitWorktreeRun.virtual_window_id == window_id)
        )
        diff = run.session_diff_json if run is not None else None
        commits = diff.get("commits") if isinstance(diff, dict) else None
        return bool(commits and commits[0].get("sha") == "feature")


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


def test_bulk_terminal_output_keeps_publishing_when_recording_queue_is_full(
    client_agent_db, monkeypatch
):
    monkeypatch.setattr(client_agent_router, "BACKGROUND_MESSAGE_QUEUE_MAX_SIZE", 1)
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
                for text in (b"first\n", b"second\n", b"third\n"):
                    websocket.send_text(
                        encode_agent_message(
                            AgentMessage(
                                type="terminal_output",
                                client_id=client_agent_db.client_id,
                                window_id=window.id,
                                payload=TerminalPayload.from_bytes(window.id, text).model_dump(),
                            )
                        )
                    )
                wait_for_condition(recording_started.is_set)
                wait_for_condition(lambda: len(broker.published) == 3)

                websocket.send_text(
                    encode_agent_message(
                        AgentMessage(
                            type="terminal_output",
                            client_id=client_agent_db.client_id,
                            window_id=window.id,
                            payload=TerminalPayload.from_bytes(window.id, b"fourth\n").model_dump(),
                        )
                    )
                )

                wait_for_condition(
                    lambda: broker.published
                    == [
                        (client_agent_db.client_id, window.id, b"first\n"),
                        (client_agent_db.client_id, window.id, b"second\n"),
                        (client_agent_db.client_id, window.id, b"third\n"),
                        (client_agent_db.client_id, window.id, b"fourth\n"),
                    ]
                )
                assert not release_recording.is_set()
            finally:
                release_recording.set()
    finally:
        test_client.close()


def test_bulk_terminal_output_keeps_publishing_when_ai_event_queue_is_full(
    client_agent_db, monkeypatch
):
    monkeypatch.setattr(client_agent_router, "BACKGROUND_MESSAGE_QUEUE_MAX_SIZE", 1)
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    broker = FakeTerminalBroker()
    ai_event_started = threading.Event()
    release_ai_event = threading.Event()

    async def slow_handle_ai_event(*args, **kwargs):
        ai_event_started.set()
        await asyncio.to_thread(release_ai_event.wait)

    monkeypatch.setattr(app.state, "terminal_broker", broker, raising=False)
    monkeypatch.setattr(
        client_agent_router,
        "_handle_ai_event_message_with_ack_sender",
        slow_handle_ai_event,
    )

    test_client = TestClient(app)
    try:
        with connect_client_agent_bulk(test_client, client_agent_db) as websocket:
            try:
                for index in range(3):
                    websocket.send_text(
                        encode_agent_message(
                            AgentMessage(
                                type="ai_event",
                                client_id=client_agent_db.client_id,
                                window_id=window.id,
                                payload={"payload": {"id": f"queued-{index}"}},
                            )
                        )
                    )
                wait_for_condition(ai_event_started.is_set)

                websocket.send_text(
                    encode_agent_message(
                        AgentMessage(
                            type="terminal_output",
                            client_id=client_agent_db.client_id,
                            window_id=window.id,
                            payload=TerminalPayload.from_bytes(
                                window.id,
                                b"terminal-still-visible\n",
                            ).model_dump(),
                        )
                    )
                )

                wait_for_condition(
                    lambda: broker.published
                    == [
                        (
                            client_agent_db.client_id,
                            window.id,
                            b"terminal-still-visible\n",
                        )
                    ]
                )
                assert not release_ai_event.is_set()
            finally:
                release_ai_event.set()
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
    es_client = FakeElasticsearch()
    monkeypatch.setattr(app.state, "terminal_broker", broker, raising=False)
    monkeypatch.setattr(app.state, "es_client", es_client, raising=False)
    monkeypatch.setattr(app.state, "es_indexes_ready", True, raising=False)

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
                and len(es_client.indexed_documents) == 1
            )
    finally:
        test_client.close()

    async def load_terminal_state():
        async with client_agent_db.session_factory() as session:
            rows = (await session.execute(select(Event))).scalars().all()
            db_window = await session.get(VirtualWindow, window.id)
            return rows, db_window

    rows, db_window = asyncio.run(load_terminal_state())
    assert broker.published == [(client_agent_db.client_id, window.id, b"remote output\n")]
    assert rows == []
    assert db_window is not None
    assert db_window.terminal_last_output_at is not None
    assert es_client.indexed_documents[0]["document"] == {
        "client_id": str(client_agent_db.client_id),
        "virtual_window_id": str(window.id),
        "text": "remote output\n",
        "source_event_ids": [],
    }


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
    es_client = FakeElasticsearch()
    failures: list[bytes] = []

    async def failing_sender(data: bytes) -> None:
        failures.append(data)
        raise RuntimeError("stale browser websocket")

    asyncio.run(broker.subscribe(client_agent_db.client_id, window.id, failing_sender))
    monkeypatch.setattr(app.state, "terminal_broker", broker, raising=False)
    monkeypatch.setattr(app.state, "es_client", es_client, raising=False)
    monkeypatch.setattr(app.state, "es_indexes_ready", True, raising=False)

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
            wait_for_condition(lambda: len(es_client.indexed_documents) == 1)
    finally:
        test_client.close()

    async def load_terminal_state():
        async with client_agent_db.session_factory() as session:
            rows = (await session.execute(select(Event))).scalars().all()
            db_window = await session.get(VirtualWindow, window.id)
            return rows, db_window

    rows, db_window = asyncio.run(load_terminal_state())
    assert failures == [b"remote output\n"]
    assert rows == []
    assert db_window is not None
    assert db_window.terminal_last_output_at is not None
    assert es_client.indexed_documents[0]["document"]["text"] == "remote output\n"


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


def test_client_agent_bulk_websocket_accepts_agent_work_presence(client_agent_db, monkeypatch):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    broker = app.state.terminal_broker
    ui_event_hub = CaptureUiEventHub()
    monkeypatch.setattr(app.state, "ui_event_hub", ui_event_hub, raising=False)
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
                and asyncio.run(_event_count(client_agent_db.session_factory)) == 1
            )
    finally:
        test_client.close()

    assert asyncio.run(_event_count(client_agent_db.session_factory)) == 1
    assert ui_event_hub.invalidations == [
        {
            "resources": ["window"],
            "client_id": client_agent_db.client_id,
            "window_id": window.id,
            "reason": "agent_work_presence",
        }
    ]


def test_client_agent_websocket_publishes_view_terminal_selection(client_agent_db):
    window = asyncio.run(create_remote_window(client_agent_db.session_factory, client_agent_db.client_id))
    broker = app.state.terminal_broker
    broker.published.clear()
    view_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

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
                encode_agent_message(AgentMessage(type="hello", client_id=client_agent_db.client_id))
            )
            response = websocket.receive_json()
            assert response["type"] == "hello_ack"
            websocket.send_text(
                encode_agent_message(
                    AgentMessage(
                        type="terminal_selection",
                        client_id=client_agent_db.client_id,
                        window_id=window.id,
                        payload={"view_id": str(view_id)},
                    )
                )
            )
            wait_for_condition(lambda: len(broker.published) == 1)
    finally:
        test_client.close()

    client_id, published_view_id, raw_message = broker.published[0]
    assert client_id == client_agent_db.client_id
    assert published_view_id == view_id
    assert json.loads(raw_message.decode("utf-8")) == {
        "type": "terminal_selection",
        "client_id": str(client_agent_db.client_id),
        "window_id": str(window.id),
        "view_id": str(view_id),
    }


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
