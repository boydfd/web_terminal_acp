import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import app
from app.models import AiSession, ClientRuntime, Event, EventSourceType, SummaryJob, SummaryJobStatus, VirtualWindow
from app.repositories.clients import create_client, ensure_local_client
from app.repositories.windows import create_window
from app.routers import windows as windows_router
from app.routers.windows import get_tmux_manager
from app.services.runtime.client_connections import ClientConnectionClosed
from app.services.runtime.protocol import AgentMessage

try:
    from app.db import Base
except ImportError:  # pragma: no cover - compatibility with alternate app layout
    from app.model_base import Base


class FakeTmuxManager:
    killed_targets: list[object] = []

    async def create_window(
        self,
        cwd: str | None,
        shell_command: str | None,
        *,
        client_id: UUID | str | None = None,
        window_id: UUID | str | None = None,
    ):
        return type("TmuxTarget", (), {"session": "test_pool", "window_id": "@99"})()

    async def kill_window(self, target: object) -> None:
        self.killed_targets.append(target)


class FakeRemoteConnection:
    def __init__(self) -> None:
        self.requests: list[AgentMessage] = []

    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        self.requests.append(message)
        if message.type == "kill_window":
            return AgentMessage(
                type="kill_window_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload={},
            )
        return AgentMessage(
            type="create_window_result",
            client_id=message.client_id,
            window_id=message.window_id,
            request_id=message.request_id,
            payload={"remote_session_id": "remote-session", "remote_window_id": "remote-window"},
        )


class FailingRemoteConnection(FakeRemoteConnection):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        self.requests.append(message)
        raise self.exc


class FakeConnectionRegistry:
    def __init__(self, connection: FakeRemoteConnection | None = None) -> None:
        self.connection = connection
        self.requested_client_ids = []

    def get(self, client_id: UUID):
        self.requested_client_ids.append(client_id)
        return self.connection


class DbClient:
    def __init__(self, client: AsyncClient, session_factory: async_sessionmaker):
        self._client = client
        self.session_factory = session_factory

    async def post(self, *args, **kwargs):
        return await self._client.post(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return await self._client.get(*args, **kwargs)

    async def patch(self, *args, **kwargs):
        return await self._client.patch(*args, **kwargs)

    async def delete(self, *args, **kwargs):
        return await self._client.delete(*args, **kwargs)


@pytest.fixture
async def db_client(tmp_path):
    database_path = tmp_path / "windows.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        await ensure_local_client(session)
        await session.commit()

    async def override_get_session():
        async with session_factory() as session:
            yield session

    FakeTmuxManager.killed_targets = []
    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_tmux_manager] = FakeTmuxManager
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as test_client:
            yield DbClient(test_client, session_factory)
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_tmux_manager, None)
        for state_name in ("client_connections", "terminal_broker"):
            if hasattr(app.state, state_name):
                delattr(app.state, state_name)
        await engine.dispose()


async def get_local_client_id(db_client: DbClient) -> str:
    response = await db_client.get("/api/clients")
    assert response.status_code == 200
    local_clients = [client for client in response.json() if client["runtime"] == "local"]
    assert len(local_clients) == 1
    return local_clients[0]["id"]


async def create_remote_client_id(db_client: DbClient, name: str = "remote-a") -> str:
    async with db_client.session_factory() as session:
        client, _token = await create_client(
            session,
            name=name,
            runtime=ClientRuntime.remote,
        )
        client_id = str(client.id)
        await session.commit()
    return client_id


@pytest.mark.asyncio
async def test_create_window_kills_tmux_window_when_database_commit_fails(db_client, monkeypatch):
    client_id = await get_local_client_id(db_client)

    async def fail_commit(self):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        await db_client.post(
            f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
        )

    assert len(FakeTmuxManager.killed_targets) == 1
    assert FakeTmuxManager.killed_targets[0].session == "test_pool"
    assert FakeTmuxManager.killed_targets[0].window_id == "@99"


@pytest.mark.asyncio
async def test_create_window_appears_in_uncategorized_tree(db_client):
    client_id = await get_local_client_id(db_client)
    response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    assert response.status_code == 200
    created = response.json()
    assert created["client_id"] == client_id
    assert created["title"].startswith("Terminal-")
    assert created["status"] == "ACTIVE"
    assert created["tmux_session"] == "test_pool"
    assert created["tmux_window_id"] == "@99"
    assert created["remote_session_id"] is None
    assert created["remote_window_id"] is None
    assert created["title_manually_overridden"] is False
    assert created["folder_manually_overridden"] is False
    assert created["command_capture_supported"] is True
    assert created["summary_job"] is None

    tree_response = await db_client.get(f"/api/clients/{client_id}/tree")
    tree = tree_response.json()
    uncategorized = next(folder for folder in tree if folder["path"] == "/未分类")
    assert uncategorized["windows"][0]["id"] == created["id"]
    assert uncategorized["windows"][0]["created_at"] == created["created_at"]


@pytest.mark.asyncio
async def test_create_window_persists_remote_session_and_window_ids(db_client):
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(
            session,
            client.id,
            cwd="/tmp",
            shell_command="/bin/bash",
            remote_session_id="remote-session-123",
            remote_window_id="remote-window-456",
        )
        window_id = window.id
        await session.commit()

    async with db_client.session_factory() as session:
        persisted = await session.get(VirtualWindow, window_id)

    assert persisted is not None
    assert persisted.remote_session_id == "remote-session-123"
    assert persisted.remote_window_id == "remote-window-456"


@pytest.mark.asyncio
async def test_create_window_creates_remote_window_when_client_connection_exists(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = FakeRemoteConnection()
    app.state.client_connections = FakeConnectionRegistry(connection)

    response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={"cwd": "/tmp/ignored", "shell_command": "/bin/bash"},
    )

    assert response.status_code == 200
    created = response.json()
    assert created["client_id"] == remote_client_id
    assert created["tmux_session"] is None
    assert created["tmux_window_id"] is None
    assert created["remote_session_id"] == "remote-session"
    assert created["remote_window_id"] == "remote-window"
    assert len(connection.requests) == 1
    assert connection.requests[0].type == "create_window"
    assert connection.requests[0].client_id == UUID(remote_client_id)
    assert connection.requests[0].window_id == UUID(created["id"])
    assert connection.requests[0].payload == {"cwd": "/tmp/ignored", "shell_command": "/bin/bash"}
    assert created["cwd"] == "/tmp/ignored"
    assert created["shell_command"] == "/bin/bash"
    assert FakeTmuxManager.killed_targets == []


@pytest.mark.asyncio
async def test_create_window_returns_503_for_remote_client_until_runtime_exists(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    app.state.client_connections = FakeConnectionRegistry(None)

    response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={"cwd": "/tmp", "shell_command": "/bin/bash"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "remote runtime unavailable"}
    assert FakeTmuxManager.killed_targets == []

    async with db_client.session_factory() as session:
        windows = (await session.execute(select(VirtualWindow))).scalars().all()
    assert windows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "remote_exc",
    [ClientConnectionClosed("client disconnected"), asyncio.TimeoutError()],
)
async def test_create_window_returns_503_when_remote_create_request_becomes_unavailable(
    db_client, remote_exc
):
    remote_client_id = await create_remote_client_id(db_client)
    app.state.client_connections = FakeConnectionRegistry(FailingRemoteConnection(remote_exc))

    response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={"cwd": "/tmp", "shell_command": "/bin/bash"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "remote runtime unavailable"}
    assert FakeTmuxManager.killed_targets == []

    async with db_client.session_factory() as session:
        windows = (await session.execute(select(VirtualWindow))).scalars().all()
    assert windows == []


@pytest.mark.asyncio
async def test_get_window_returns_window_metadata(db_client):
    client_id = await get_local_client_id(db_client)
    create_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp/project", "shell_command": "/bin/zsh"}
    )
    created = create_response.json()

    get_response = await db_client.get(f"/api/clients/{client_id}/windows/{created['id']}")

    assert get_response.status_code == 200
    assert get_response.json()["id"] == created["id"]
    assert get_response.json()["client_id"] == client_id
    assert get_response.json()["cwd"] == "/tmp/project"
    assert get_response.json()["shell_command"] == "/bin/zsh"


@pytest.mark.asyncio
async def test_move_window_to_folder(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    folder_response = await db_client.post(
        f"/api/clients/{client_id}/folders", json={"path": "/2026-05/生产排障"}
    )

    move_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}",
        json={"folder_id": folder_response.json()["id"], "title": "[Claude] 修复 Nginx 403"},
    )
    assert move_response.status_code == 200
    assert move_response.json()["title"] == "[Claude] 修复 Nginx 403"
    assert move_response.json()["folder_id"] == folder_response.json()["id"]


@pytest.mark.asyncio
async def test_patch_window_title_and_folder_set_manual_lock_flags(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    folder_response = await db_client.post(
        f"/api/clients/{client_id}/folders", json={"path": "/manual/folder"}
    )

    title_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}",
        json={"title": "Manual title"},
    )
    folder_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}",
        json={"folder_id": folder_response.json()["id"]},
    )

    assert title_response.status_code == 200
    assert title_response.json()["title_manually_overridden"] is True
    assert title_response.json()["folder_manually_overridden"] is False
    assert folder_response.status_code == 200
    assert folder_response.json()["title_manually_overridden"] is True
    assert folder_response.json()["folder_manually_overridden"] is True


@pytest.mark.asyncio
async def test_patch_window_summary_and_tags_do_not_set_manual_lock_flags(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )

    patch_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}",
        json={"summary": "Reviewed by user.", "title_tags": ["reviewed"]},
    )

    assert patch_response.status_code == 200
    assert patch_response.json()["title_manually_overridden"] is False
    assert patch_response.json()["folder_manually_overridden"] is False


@pytest.mark.asyncio
async def test_move_window_rejects_folder_from_another_client(db_client):
    local_client_id = await get_local_client_id(db_client)
    remote_client_id = await create_remote_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{local_client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    remote_folder_response = await db_client.post(
        f"/api/clients/{remote_client_id}/folders", json={"path": "/remote-only"}
    )

    move_response = await db_client.patch(
        f"/api/clients/{local_client_id}/windows/{window_response.json()['id']}",
        json={"folder_id": remote_folder_response.json()["id"]},
    )

    assert move_response.status_code == 404
    assert move_response.json() == {"detail": "folder not found"}


@pytest.mark.asyncio
async def test_delete_window_kills_tmux_and_removes_record(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    window_id = window_response.json()["id"]

    delete_response = await db_client.delete(f"/api/clients/{client_id}/windows/{window_id}")

    assert delete_response.status_code == 204
    assert len(FakeTmuxManager.killed_targets) == 1
    assert FakeTmuxManager.killed_targets[0].session == "test_pool"
    assert FakeTmuxManager.killed_targets[0].window_id == "@99"

    get_response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")
    assert get_response.status_code == 404

    tree_response = await db_client.get(f"/api/clients/{client_id}/tree")
    assert tree_response.status_code == 200
    tree_window_ids = [
        window["id"]
        for folder in tree_response.json()
        for window in folder["windows"]
    ]
    assert window_id not in tree_window_ids


@pytest.mark.asyncio
async def test_delete_window_returns_404_for_missing_window(db_client):
    client_id = await get_local_client_id(db_client)
    missing_window_id = "00000000-0000-4000-8000-000000000099"

    delete_response = await db_client.delete(f"/api/clients/{client_id}/windows/{missing_window_id}")

    assert delete_response.status_code == 404
    assert delete_response.json() == {"detail": "window not found"}


@pytest.mark.asyncio
async def test_delete_remote_window_requests_kill_when_client_is_online(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = FakeRemoteConnection()
    app.state.client_connections = FakeConnectionRegistry(connection)

    create_response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={"cwd": "/tmp", "shell_command": "/bin/bash"},
    )
    window_id = create_response.json()["id"]

    delete_response = await db_client.delete(f"/api/clients/{remote_client_id}/windows/{window_id}")

    assert delete_response.status_code == 204
    assert len(connection.requests) == 2
    assert connection.requests[1].type == "kill_window"
    assert connection.requests[1].window_id == UUID(window_id)

    async with db_client.session_factory() as session:
        persisted = await session.get(VirtualWindow, UUID(window_id))
    assert persisted is None


@pytest.mark.asyncio
async def test_patch_window_updates_metadata_fields(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )

    patch_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}",
        json={
            "title": "Reviewed terminal",
            "status": "ARCHIVED",
            "summary": "Nginx 403 was caused by a missing index file.",
            "title_tags": ["Claude", "Nginx"],
        },
    )

    assert patch_response.status_code == 200
    body = patch_response.json()
    assert body["title"] == "Reviewed terminal"
    assert body["status"] == "ARCHIVED"
    assert body["summary"] == "Nginx 403 was caused by a missing index file."
    assert body["title_tags"] == ["Claude", "Nginx"]


@pytest.mark.asyncio
async def test_window_and_tree_include_runtime_tags_for_agent_and_path(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/project", "shell_command": "/bin/bash"},
    )
    assert window_response.status_code == 200
    assert window_response.json()["runtime_tags"] == ["/tmp/project"]
    window_id = window_response.json()["id"]

    async with db_client.session_factory() as session:
        window = await session.get(VirtualWindow, UUID(window_id))
        assert window is not None
        window.title_tags = ["summary", "nginx"]
        session.add(
            AiSession(
                client_id=UUID(client_id),
                provider="codex",
                source_id="codex-session-runtime-tags",
                project_path="/workspace/project",
                virtual_window_id=UUID(window_id),
            )
        )
        await session.commit()

    detail_response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")
    assert detail_response.status_code == 200
    assert detail_response.json()["runtime_tags"] == ["codex", "/workspace/project"]

    tree_response = await db_client.get(f"/api/clients/{client_id}/tree")
    assert tree_response.status_code == 200
    tree = tree_response.json()
    tree_window = next(
        window
        for folder in tree
        for window in folder["windows"]
        if window["id"] == window_id
    )
    assert "runtime_tags" not in tree_window
    assert "work_status" not in tree_window
    assert tree_window["title_tags"] == ["summary", "nginx"]

    activity_response = await db_client.get(
        f"/api/clients/{client_id}/windows/activity",
        params={"include_runtime_tags": "true"},
    )
    assert activity_response.status_code == 200
    activity_window = next(
        item for item in activity_response.json()["windows"] if item["window_id"] == window_id
    )
    assert activity_window["runtime_tags"] == ["codex", "/workspace/project"]


@pytest.mark.asyncio
async def test_get_window_agent_record_returns_sessions_and_non_output_events(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/project", "shell_command": "/bin/bash"},
    )
    window_id = UUID(window_response.json()["id"])

    async with db_client.session_factory() as session:
        ai_session = AiSession(
            client_id=UUID(client_id),
            provider="claude",
            source_id="claude-session-1",
            source_path="/home/user/.claude/session.jsonl",
            virtual_window_id=window_id,
        )
        session.add(ai_session)
        await session.flush()
        session.add_all(
            [
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.claude_jsonl,
                    source_id="claude-session-1",
                    kind="user_message",
                    virtual_window_id=window_id,
                    ai_session_id=ai_session.id,
                    payload_json={"type": "user", "message": {"content": "hello"}},
                    fingerprint="agent-record-user",
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.terminal,
                    source_id=str(window_id),
                    kind="terminal_input_command",
                    virtual_window_id=window_id,
                    payload_json={"command": "codex", "shell": "bash"},
                    fingerprint="agent-record-command",
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.terminal,
                    source_id=str(window_id),
                    kind="terminal_output",
                    virtual_window_id=window_id,
                    payload_json={"text": "raw terminal output"},
                    fingerprint="agent-record-output",
                ),
            ]
        )
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}/agent-record")

    assert response.status_code == 200
    body = response.json()
    assert body["window_id"] == str(window_id)
    assert [item["source_id"] for item in body["sessions"]] == ["claude-session-1"]
    assert [item["kind"] for item in body["events"]] == ["user_message", "terminal_input_command"]
    assert body["events"][0]["payload_json"]["message"]["content"] == "hello"
    assert body["events"][0]["projection"] == {
        "tone": "user-input",
        "label": "User input",
        "body": "hello",
        "body_format": "markdown",
        "subtype": "user_message",
    }
    assert body["events"][1]["projection"] == {
        "tone": "terminal",
        "label": "Terminal command",
        "body": "codex",
        "body_format": "markdown",
        "subtype": "command",
    }
    assert body["events_total"] == 2
    assert body["events_limit"] == 100
    assert body["events_offset"] == 0
    assert body["events_has_more"] is False


@pytest.mark.asyncio
async def test_get_window_agent_record_chat_returns_minimal_messages(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/project", "shell_command": "/bin/bash"},
    )
    window_id = UUID(window_response.json()["id"])

    async with db_client.session_factory() as session:
        ai_session = AiSession(
            client_id=UUID(client_id),
            provider="codex",
            source_id="codex-session-1",
            virtual_window_id=window_id,
        )
        session.add(ai_session)
        await session.flush()
        base_time = datetime.now(timezone.utc)
        session.add_all(
            [
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.codex_trace,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window_id,
                    ai_session_id=ai_session.id,
                    payload_json={
                        "raw_type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
                    },
                    fingerprint="agent-record-chat-user",
                    created_at=base_time,
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.codex_trace,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window_id,
                    ai_session_id=ai_session.id,
                    payload_json={
                        "raw_type": "response_item",
                        "payload": {"type": "function_call", "name": "bash", "arguments": "{\"cmd\":\"ls\"}"},
                    },
                    fingerprint="agent-record-chat-tool",
                    created_at=base_time + timedelta(milliseconds=1),
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.codex_trace,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window_id,
                    ai_session_id=ai_session.id,
                    payload_json={
                        "raw_type": "response_item",
                        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
                    },
                    fingerprint="agent-record-chat-agent",
                    created_at=base_time + timedelta(milliseconds=2),
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.codex_trace,
                    source_id="codex-session-1",
                    kind="event_msg",
                    virtual_window_id=window_id,
                    ai_session_id=ai_session.id,
                    payload_json={
                        "raw_type": "event_msg",
                        "payload": {"type": "agent_message", "message": "done"},
                    },
                    fingerprint="agent-record-chat-agent-duplicate",
                    created_at=base_time + timedelta(milliseconds=3),
                ),
            ]
        )
        await session.commit()

    response = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/agent-record/chat?messages_limit=1"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["window_id"] == str(window_id)
    assert [(item["role"], item["body"]) for item in body["messages"]] == [("user", "hello")]
    assert "payload_json" not in body["messages"][0]
    assert body["messages_total"] == 2
    assert body["messages_limit"] == 1
    assert body["messages_offset"] == 0
    assert body["messages_has_more"] is True


@pytest.mark.asyncio
async def test_get_window_agent_record_detail_dedupes_codex_duplicate_message_events(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/project", "shell_command": "/bin/bash"},
    )
    window_id = UUID(window_response.json()["id"])

    async with db_client.session_factory() as session:
        ai_session = AiSession(
            client_id=UUID(client_id),
            provider="codex",
            source_id="codex-session-1",
            virtual_window_id=window_id,
        )
        session.add(ai_session)
        await session.flush()
        base_time = datetime.now(timezone.utc)
        session.add_all(
            [
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.codex_trace,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window_id,
                    ai_session_id=ai_session.id,
                    payload_json={
                        "raw_type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    },
                    fingerprint="agent-record-detail-agent",
                    created_at=base_time,
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.codex_trace,
                    source_id="codex-session-1",
                    kind="event_msg",
                    virtual_window_id=window_id,
                    ai_session_id=ai_session.id,
                    payload_json={
                        "raw_type": "event_msg",
                        "payload": {"type": "agent_message", "message": "done"},
                    },
                    fingerprint="agent-record-detail-agent-duplicate",
                    created_at=base_time + timedelta(milliseconds=1),
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.codex_trace,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window_id,
                    ai_session_id=ai_session.id,
                    payload_json={
                        "raw_type": "response_item",
                        "payload": {"type": "function_call", "name": "bash", "arguments": "{}"},
                    },
                    fingerprint="agent-record-detail-tool",
                    created_at=base_time + timedelta(milliseconds=2),
                ),
            ]
        )
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}/agent-record/detail")

    assert response.status_code == 200
    body = response.json()
    assert [(event["kind"], event["projection"]["body"]) for event in body["events"]] == [
        ("response_item", "done"),
        ("response_item", "bash\n\n```json\n{}\n```"),
    ]
    assert body["events_total"] == 3


@pytest.mark.asyncio
async def test_get_window_agent_record_projection_falls_back_when_adapter_projection_fails(
    db_client, monkeypatch
):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/project", "shell_command": "/bin/bash"},
    )
    window_id = UUID(window_response.json()["id"])

    async with db_client.session_factory() as session:
        session.add(
            Event(
                client_id=UUID(client_id),
                source_type=EventSourceType.agent_tool_record,
                source_id="cursor-session-1",
                kind="assistant_message",
                virtual_window_id=window_id,
                payload_json={"provider": "cursor_cli", "role": "assistant", "text": "fallback body"},
                fingerprint="agent-record-projection-fallback",
            )
        )
        await session.commit()

    class FailingAdapter:
        def project_event(self, event):
            raise ValueError("bad legacy payload")

        def project_chat(self, event):
            return None

    monkeypatch.setattr(windows_router, "_adapter_for_event", lambda event: FailingAdapter())

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}/agent-record/detail")

    assert response.status_code == 200
    assert response.json()["events"][0]["projection"] == {
        "tone": "assistant",
        "label": "Assistant",
        "body": "fallback body",
        "body_format": "markdown",
        "subtype": "message",
    }


@pytest.mark.asyncio
async def test_get_window_agent_record_projects_generic_cursor_and_legacy_alias_claude_events(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/project", "shell_command": "/bin/bash"},
    )
    window_id = UUID(window_response.json()["id"])

    async with db_client.session_factory() as session:
        claude_session = AiSession(
            client_id=UUID(client_id),
            provider="claude",
            source_id="claude-alias-session",
            virtual_window_id=window_id,
        )
        session.add(claude_session)
        await session.flush()
        base_time = datetime.now(timezone.utc)
        session.add_all(
            [
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="cursor-session-1",
                    kind="assistant_message",
                    virtual_window_id=window_id,
                    payload_json={
                        "provider": "cursor_cli",
                        "role": "assistant",
                        "text": "Cursor rendered this",
                    },
                    fingerprint="agent-record-cursor-projection",
                    created_at=base_time,
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="claude-alias-session",
                    kind="user_message",
                    virtual_window_id=window_id,
                    ai_session_id=claude_session.id,
                    payload_json={"type": "user", "message": {"content": "Legacy Claude alias"}},
                    fingerprint="agent-record-claude-alias-projection",
                    created_at=base_time + timedelta(milliseconds=1),
                ),
            ]
        )
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}/agent-record/detail")

    assert response.status_code == 200
    projections = [event["projection"] for event in response.json()["events"]]
    assert projections == [
        {
            "tone": "agent",
            "label": "Agent response",
            "body": "Cursor rendered this",
            "body_format": "markdown",
            "subtype": "assistant_message",
        },
        {
            "tone": "user-input",
            "label": "User input",
            "body": "Legacy Claude alias",
            "body_format": "markdown",
            "subtype": "user_message",
        },
    ]


@pytest.mark.asyncio
async def test_get_window_agent_record_paginates_events(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/project", "shell_command": "/bin/bash"},
    )
    window_id = UUID(window_response.json()["id"])
    base_time = datetime(2026, 5, 22, tzinfo=timezone.utc)

    async with db_client.session_factory() as session:
        session.add_all(
            [
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.terminal,
                    source_id=str(window_id),
                    kind="terminal_input_command",
                    virtual_window_id=window_id,
                    payload_json={"command": f"cmd-{index}"},
                    fingerprint=f"agent-record-page-{index}",
                    created_at=base_time + timedelta(seconds=index),
                )
                for index in range(3)
            ]
        )
        await session.commit()

    response = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/agent-record?events_limit=2&events_offset=1"
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["payload_json"]["command"] for item in body["events"]] == ["cmd-1", "cmd-2"]
    assert body["events_total"] == 3
    assert body["events_limit"] == 2
    assert body["events_offset"] == 1
    assert body["events_has_more"] is False


@pytest.mark.asyncio
async def test_patch_window_accepts_disconnected_status(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )

    patch_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}",
        json={"status": "DISCONNECTED"},
    )

    assert patch_response.status_code == 200
    assert patch_response.json()["status"] == "DISCONNECTED"


@pytest.mark.asyncio
async def test_summary_job_enqueued_once_for_repeated_enqueue(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    window_id = window_response.json()["id"]

    first_retry = await db_client.post(f"/api/clients/{client_id}/windows/{window_id}/summary_jobs")
    second_retry = await db_client.post(f"/api/clients/{client_id}/windows/{window_id}/summary_jobs")

    assert first_retry.status_code == 200
    assert second_retry.status_code == 200
    assert second_retry.json()["id"] == window_id

    async with db_client.session_factory() as session:
        jobs = list(
            await session.scalars(
                select(SummaryJob).where(SummaryJob.virtual_window_id == UUID(window_id))
            )
        )

    assert len(jobs) == 1
    assert jobs[0].status is SummaryJobStatus.pending


@pytest.mark.asyncio
async def test_retry_summary_job_accepts_missing_body_and_records_manual_retry(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    window_id = window_response.json()["id"]

    retry_response = await db_client.post(f"/api/clients/{client_id}/windows/{window_id}/summary_jobs")

    assert retry_response.status_code == 200
    body = retry_response.json()
    assert body["summary_job"]["trigger_reason"] == "manual_retry"
    assert body["summary_job"]["allow_title_folder_override"] is False
    assert body["summary_job"]["run_after"] is not None


@pytest.mark.asyncio
async def test_retry_summary_job_with_override_updates_pending_job(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    window_id = window_response.json()["id"]

    retry_response = await db_client.post(
        f"/api/clients/{client_id}/windows/{window_id}/summary_jobs",
        json={"allow_title_folder_override": True},
    )

    assert retry_response.status_code == 200
    body = retry_response.json()
    assert body["summary_job"]["status"] == "PENDING"
    assert body["summary_job"]["trigger_reason"] == "manual_retry"
    assert body["summary_job"]["allow_title_folder_override"] is True

    async with db_client.session_factory() as session:
        job = await session.scalar(select(SummaryJob).where(SummaryJob.virtual_window_id == UUID(window_id)))

    assert job is not None
    assert job.status is SummaryJobStatus.pending
    assert job.trigger_reason == "manual_retry"
    assert job.allow_title_folder_override is True
    assert job.run_after is not None


@pytest.mark.asyncio
async def test_get_window_returns_latest_summary_job_fields(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    window_id = window_response.json()["id"]
    async with db_client.session_factory() as session:
        job = SummaryJob(virtual_window_id=UUID(window_id), status=SummaryJobStatus.pending)
        session.add(job)
        await session.flush()
        job.attempts = 2
        job.last_error = "temporary failure"
        job.trigger_reason = "terminal_input"
        await session.commit()

    get_response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")

    assert get_response.status_code == 200
    summary_job = get_response.json()["summary_job"]
    assert UUID(summary_job["id"])
    assert summary_job["status"] == "PENDING"
    assert summary_job["trigger_reason"] == "terminal_input"
    assert summary_job["attempts"] == 2
    assert summary_job["last_error"] == "temporary failure"
    assert summary_job["allow_title_folder_override"] is False
    assert summary_job["updated_at"] is not None


@pytest.mark.asyncio
async def test_summary_job_enqueue_is_idempotent_under_concurrent_requests(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    window_id = window_response.json()["id"]

    async with db_client.session_factory() as session:
        existing_jobs = list(
            await session.scalars(
                select(SummaryJob).where(SummaryJob.virtual_window_id == UUID(window_id))
            )
        )
        for job in existing_jobs:
            job.status = SummaryJobStatus.succeeded
        await session.commit()

    responses = await asyncio.gather(
        *(db_client.post(f"/api/clients/{client_id}/windows/{window_id}/summary_jobs") for _ in range(8))
    )

    assert all(response.status_code < 500 for response in responses)

    async with db_client.session_factory() as session:
        active_jobs = list(
            await session.scalars(
                select(SummaryJob).where(
                    SummaryJob.virtual_window_id == UUID(window_id),
                    SummaryJob.status.in_([SummaryJobStatus.pending, SummaryJobStatus.running]),
                )
            )
        )

    assert len(active_jobs) == 1


@pytest.mark.asyncio
async def test_patch_window_rejects_blank_title(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )

    response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}", json={"title": "   "}
    )

    assert 400 <= response.status_code < 500


@pytest.mark.asyncio
async def test_patch_window_rejects_too_long_title(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )

    response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}", json={"title": "x" * 256}
    )

    assert 400 <= response.status_code < 500


@pytest.mark.asyncio
async def test_patch_window_rejects_invalid_title_tags(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )

    blank_tag_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}", json={"title_tags": ["Claude", "   "]}
    )
    too_many_tags_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}",
        json={"title_tags": [f"tag-{index}" for index in range(21)]},
    )
    too_long_tag_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}", json={"title_tags": ["x" * 65]}
    )

    assert 400 <= blank_tag_response.status_code < 500
    assert 400 <= too_many_tags_response.status_code < 500
    assert 400 <= too_long_tag_response.status_code < 500


@pytest.mark.asyncio
async def test_create_window_rejects_too_long_cwd_and_shell_command(db_client):
    client_id = await get_local_client_id(db_client)
    too_long_value = "x" * 4097

    cwd_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": too_long_value, "shell_command": "/bin/bash"}
    )
    shell_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": too_long_value}
    )

    assert 400 <= cwd_response.status_code < 500
    assert 400 <= shell_response.status_code < 500


@pytest.mark.asyncio
async def test_missing_window_returns_404(db_client):
    client_id = await get_local_client_id(db_client)
    missing_id = "00000000-0000-0000-0000-000000000000"

    get_response = await db_client.get(f"/api/clients/{client_id}/windows/{missing_id}")
    patch_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{missing_id}", json={"title": "missing"}
    )
    retry_response = await db_client.post(f"/api/clients/{client_id}/windows/{missing_id}/summary_jobs")

    assert get_response.status_code == 404
    assert patch_response.status_code == 404
    assert retry_response.status_code == 404


@pytest.mark.asyncio
async def test_window_route_returns_404_for_missing_client(db_client):
    missing_client_id = "00000000-0000-0000-0000-000000000000"
    missing_window_id = "00000000-0000-0000-0000-000000000000"

    create_response = await db_client.post(
        f"/api/clients/{missing_client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    get_response = await db_client.get(f"/api/clients/{missing_client_id}/windows/{missing_window_id}")
    patch_response = await db_client.patch(
        f"/api/clients/{missing_client_id}/windows/{missing_window_id}", json={"title": "missing"}
    )
    retry_response = await db_client.post(
        f"/api/clients/{missing_client_id}/windows/{missing_window_id}/summary_jobs"
    )

    assert create_response.status_code == 404
    assert get_response.status_code == 404
    assert patch_response.status_code == 404
    assert retry_response.status_code == 404


@pytest.mark.asyncio
async def test_invalid_status_returns_client_error(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )

    response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_response.json()['id']}", json={"status": "NOT_A_STATUS"}
    )

    assert 400 <= response.status_code < 500


@pytest.mark.asyncio
async def test_get_window_returns_long_idle_work_status_without_activity(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_response.json()['id']}")

    assert response.status_code == 200
    work_status = response.json()["work_status"]
    assert work_status["state"] == "LONG_IDLE"
    assert work_status["label"] == "长时间没有工作了"
    assert work_status["color"] == "gray"
    assert work_status["last_activity_at"] is None
    assert work_status["last_working_activity_at"] is None


@pytest.mark.asyncio
async def test_get_window_returns_recent_active_for_recent_shell_command(db_client):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        session.add(
            Event(
                client_id=client.id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "pwd"},
                fingerprint=f"terminal_input_command:{window.id}:recent",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=30),
            )
        )
        window_id = window.id
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")

    assert response.status_code == 200
    work_status = response.json()["work_status"]
    assert work_status["state"] == "RECENT_ACTIVE"
    assert work_status["label"] == "最近刚活跃过"
    assert work_status["color"] == "green"
    assert work_status["last_activity_at"] is not None
    assert work_status["last_working_activity_at"] is None


@pytest.mark.asyncio
async def test_get_window_returns_working_for_in_progress_agent_command(db_client):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        session.add_all([
            Event(
                client_id=client.id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'fix tests'", "sequence": 42},
                fingerprint=f"terminal_input_command:{window.id}:codex",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_output",
                virtual_window_id=window.id,
                payload_json={"text": "working\n"},
                fingerprint=f"terminal_output:{window.id}:recent",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            ),
        ])
        window_id = window.id
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")

    assert response.status_code == 200
    work_status = response.json()["work_status"]
    assert work_status["state"] == "WORKING"
    assert work_status["label"] == "正在工作中"
    assert work_status["color"] == "orange"
    assert work_status["last_activity_at"] is not None
    assert work_status["last_working_activity_at"] is not None


@pytest.mark.asyncio
async def test_windows_activity_returns_work_status_for_windows(db_client):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        session.add(
            Event(
                client_id=client.id,
                source_type=EventSourceType.codex_trace,
                source_id="codex-session",
                kind="event_msg",
                virtual_window_id=window.id,
                payload_json={"type": "agent_message", "message": "Working"},
                fingerprint=f"codex_trace:{window.id}:recent",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            )
        )
        window_id = str(window.id)
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert response.status_code == 200
    activity_window = next(
        item for item in response.json()["windows"] if item["window_id"] == window_id
    )
    assert activity_window["work_status"]["state"] == "WORKING"
