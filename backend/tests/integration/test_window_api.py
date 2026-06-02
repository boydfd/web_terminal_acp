import asyncio
import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import app
from app.models import (
    AiSession,
    ClientRuntime,
    Event,
    EventSourceType,
    Folder,
    GitWorktreeRun,
    SummaryJob,
    SummaryJobStatus,
    TerminalRecentUsage,
    VirtualWindow,
    WindowGitBinding,
)
from app.repositories.clients import create_client, ensure_local_client
from app.repositories.windows import create_window
from app.routers import folders as folders_router
from app.routers import windows as windows_router
from app.routers.windows import get_tmux_manager
from app.schemas import WindowCreateIn
from app.services import polling_response_cache
from app.services.polling_response_cache import clear_polling_response_cache
from app.services.runtime.client_connections import ClientConnectionClosed
from app.services.runtime.protocol import AgentMessage
from app.services.window_activity_api import clear_client_windows_activity_cache

try:
    from app.db import Base
except ImportError:  # pragma: no cover - compatibility with alternate app layout
    from app.model_base import Base


def codex_message_payload(text: str, *, timestamp: datetime | None = None) -> dict:
    payload = {
        "provider": "codex",
        "raw_type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp.isoformat()
    return payload


def codex_user_message_payload(text: str, *, timestamp: datetime | None = None) -> dict:
    payload = {
        "provider": "codex",
        "raw_type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp.isoformat()
    return payload


def codex_completion_payload(
    *, event_type: str = "task_completed", timestamp: datetime | None = None
) -> dict:
    payload = {
        "provider": "codex",
        "raw_type": "event_msg",
        "payload": {"type": event_type},
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp.isoformat()
    return payload


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


class CommitFailingOnSecondCallTmuxManager(FakeTmuxManager):
    commit_calls = 0


class ObservingTmuxManager(FakeTmuxManager):
    observed_in_transaction: bool | None = None

    async def create_window(
        self,
        cwd: str | None,
        shell_command: str | None,
        *,
        client_id: UUID | str | None = None,
        window_id: UUID | str | None = None,
    ):
        observed_session = getattr(windows_router, "_TEST_OBSERVED_SESSION", None)
        if observed_session is not None:
            self.__class__.observed_in_transaction = observed_session.in_transaction()
        return await super().create_window(
            cwd,
            shell_command,
            client_id=client_id,
            window_id=window_id,
        )


class CancellingTmuxManager(FakeTmuxManager):
    async def create_window(
        self,
        cwd: str | None,
        shell_command: str | None,
        *,
        client_id: UUID | str | None = None,
        window_id: UUID | str | None = None,
    ):
        raise asyncio.CancelledError()


class FakeRemoteConnection:
    def __init__(self) -> None:
        self.requests: list[AgentMessage] = []
        self.sent: list[AgentMessage] = []
        self.request_started = asyncio.Event()
        self.request_continue = asyncio.Event()

    async def send(self, message: AgentMessage) -> None:
        self.sent.append(message)

    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        self.requests.append(message)
        self.request_started.set()
        await self.request_continue.wait()
        if message.type == "kill_window":
            return AgentMessage(
                type="kill_window_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload={},
            )
        if message.type == "agent_clients_list":
            return AgentMessage(
                type="agent_client_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload={
                    "agent_clients": [
                        {
                            "id": "remote_codex",
                            "provider_id": "codex",
                            "label": "Remote Codex",
                            "aliases": [],
                            "default_command": "codex",
                            "command_names": ["codex"],
                        }
                    ]
                },
            )
        return AgentMessage(
            type="create_window_result",
            client_id=message.client_id,
            window_id=message.window_id,
            request_id=message.request_id,
            payload={"remote_session_id": "remote-session", "remote_window_id": "remote-window"},
        )

class AgentConfigRemoteConnection(FakeRemoteConnection):
    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        self.requests.append(message)
        if message.type == "agent_clients_list":
            return AgentMessage(
                type="agent_client_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload={
                    "agent_clients": [
                        {
                            "id": "claude",
                            "provider_id": "claude_code",
                            "label": "Remote Claude Code",
                            "aliases": ["claude_code"],
                            "default_command": "claude",
                            "command_names": ["claude"],
                        }
                    ]
                },
            )
        if message.type == "agent_config_get":
            return AgentMessage(
                type="agent_config_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload={
                    "agent": "claude",
                    "sections": [
                        {
                            "id": "skills",
                            "name": "Skills",
                            "items": [
                                {
                                    "id": "review",
                                    "name": "review",
                                    "enabled": True,
                                    "path": "/home/test/.claude/skills/review",
                                }
                            ],
                        },
                        {"id": "plugins", "name": "Plugins", "items": []},
                        {"id": "hooks", "name": "Hooks", "items": []},
                    ],
                },
            )
        if message.type == "agent_config_set_enabled":
            return AgentMessage(
                type="agent_config_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload={
                    "agent": "claude",
                    "sections": [
                        {"id": "skills", "name": "Skills", "items": []},
                        {"id": "plugins", "name": "Plugins", "items": []},
                        {"id": "hooks", "name": "Hooks", "items": []},
                    ],
                },
            )
        self.request_started.set()
        await self.request_continue.wait()
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


class FutureAgentRemoteConnection(AgentConfigRemoteConnection):
    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        self.requests.append(message)
        if message.type == "agent_clients_list":
            return AgentMessage(
                type="agent_client_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload={
                    "agent_clients": [
                        {
                            "id": "future_agent",
                            "provider_id": "future_provider",
                            "label": "Future Agent",
                            "aliases": ["future"],
                            "default_command": "future-agent",
                            "command_names": ["future-agent"],
                        }
                    ]
                },
            )
        if message.type == "agent_config_get":
            return AgentMessage(
                type="agent_config_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload={
                    "agent": message.payload["agent"],
                    "sections": [
                        {"id": "skills", "name": "Skills", "items": []},
                        {"id": "plugins", "name": "Plugins", "items": []},
                        {"id": "hooks", "name": "Hooks", "items": []},
                    ],
                },
            )
        if message.type == "agent_config_set_enabled":
            return AgentMessage(
                type="agent_config_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload={
                    "agent": message.payload["agent"],
                    "sections": [
                        {"id": "skills", "name": "Skills", "items": []},
                        {"id": "plugins", "name": "Plugins", "items": []},
                        {"id": "hooks", "name": "Hooks", "items": []},
                    ],
                },
            )
        self.request_started.set()
        await self.request_continue.wait()
        return AgentMessage(
            type="create_window_result",
            client_id=message.client_id,
            window_id=message.window_id,
            request_id=message.request_id,
            payload={"remote_session_id": "remote-session", "remote_window_id": "remote-window"},
        )


class CapabilityRemoteConnection(AgentConfigRemoteConnection):
    def __init__(self, capabilities: dict[str, bool]) -> None:
        super().__init__()
        self.capabilities = capabilities

    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        if message.type != "agent_clients_list":
            return await super().request(message, timeout=timeout)
        self.requests.append(message)
        return AgentMessage(
            type="agent_client_result",
            client_id=message.client_id,
            window_id=message.window_id,
            request_id=message.request_id,
            payload={
                "agent_clients": [
                    {
                        "id": "restricted_agent",
                        "provider_id": "restricted_provider",
                        "label": "Restricted Agent",
                        "aliases": ["restricted"],
                        "default_command": "restricted-agent",
                        "command_names": ["restricted-agent"],
                        "capabilities": self.capabilities,
                    }
                ]
            },
        )


class FailingRemoteConnection(FakeRemoteConnection):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        self.requests.append(message)
        self.request_started.set()
        await self.request_continue.wait()
        raise self.exc


class TerminalErrorRemoteConnection(FakeRemoteConnection):
    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        self.requests.append(message)
        self.request_started.set()
        await self.request_continue.wait()
        return AgentMessage(
            type="terminal_error",
            client_id=message.client_id,
            window_id=message.window_id,
            request_id=message.request_id,
            payload={"message": "tmux command failed"},
        )


class ObservingRemoteConnection(FakeRemoteConnection):
    observed_in_transaction: bool | None = None

    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        observed_session = getattr(windows_router, "_TEST_OBSERVED_SESSION", None)
        if observed_session is not None:
            self.__class__.observed_in_transaction = observed_session.in_transaction()
        return await super().request(message, timeout=timeout)


class FakeConnectionRegistry:
    def __init__(self, connection: FakeRemoteConnection | None = None) -> None:
        self.connection = connection
        self.requested_client_ids = []

    def get(self, client_id: UUID):
        self.requested_client_ids.append(client_id)
        return self.connection


async def allow_remote_create_to_finish(connection: FakeRemoteConnection) -> None:
    await asyncio.wait_for(connection.request_started.wait(), timeout=1.0)
    connection.request_continue.set()


async def wait_for_remote_window_ready(
    db_client: "DbClient",
    client_id: str,
    window_id: str,
) -> VirtualWindow:
    for _ in range(50):
        async with db_client.session_factory() as session:
            window = await session.get(VirtualWindow, UUID(window_id))
            if (
                window is not None
                and window.client_id == UUID(client_id)
                and window.remote_session_id is not None
                and window.remote_window_id is not None
            ):
                return window
        await asyncio.sleep(0.01)
    raise AssertionError("remote window did not become ready")


async def wait_for_remote_window_status(
    db_client: "DbClient",
    window_id: str,
    status: str,
) -> VirtualWindow:
    for _ in range(50):
        async with db_client.session_factory() as session:
            window = await session.get(VirtualWindow, UUID(window_id))
            if window is not None and window.status.value == status:
                return window
        await asyncio.sleep(0.01)
    raise AssertionError(f"remote window did not reach status {status}")


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
    clear_polling_response_cache()
    clear_client_windows_activity_cache()
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
        clear_polling_response_cache()
        clear_client_windows_activity_cache()
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


def tracking_sequence(worktree_root: str) -> str:
    digest = sha256(worktree_root.encode("utf-8")).hexdigest()[:16]
    return f"worktree:{digest}"


def parse_response_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@pytest.mark.asyncio
async def test_create_window_kills_tmux_window_when_database_commit_fails(db_client, monkeypatch):
    client_id = await get_local_client_id(db_client)
    app.dependency_overrides[get_tmux_manager] = CommitFailingOnSecondCallTmuxManager
    CommitFailingOnSecondCallTmuxManager.commit_calls = 0
    original_commit = AsyncSession.commit

    async def fail_commit(self):
        CommitFailingOnSecondCallTmuxManager.commit_calls += 1
        if CommitFailingOnSecondCallTmuxManager.commit_calls >= 2:
            raise RuntimeError("commit failed")
        await original_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        await db_client.post(
            f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
        )

    assert len(CommitFailingOnSecondCallTmuxManager.killed_targets) == 1
    assert CommitFailingOnSecondCallTmuxManager.killed_targets[0].session == "test_pool"
    assert CommitFailingOnSecondCallTmuxManager.killed_targets[0].window_id == "@99"


@pytest.mark.asyncio
async def test_create_window_commits_client_lookup_before_tmux_create(db_client):
    client_id = await get_local_client_id(db_client)
    app.dependency_overrides[get_tmux_manager] = ObservingTmuxManager
    ObservingTmuxManager.observed_in_transaction = None

    async with db_client.session_factory() as session:
        windows_router._TEST_OBSERVED_SESSION = session
        try:
            response = await db_client.post(
                f"/api/clients/{client_id}/windows",
                json={"cwd": "/tmp", "shell_command": "/bin/bash"},
            )
        finally:
            delattr(windows_router, "_TEST_OBSERVED_SESSION")

    assert response.status_code == 200
    assert ObservingTmuxManager.observed_in_transaction is False


@pytest.mark.asyncio
async def test_create_window_rolls_back_when_tmux_create_is_cancelled(db_client):
    client = windows_router._RuntimeClient(UUID(await get_local_client_id(db_client)), ClientRuntime.local)

    async with db_client.session_factory() as session:
        with pytest.raises(asyncio.CancelledError):
            await windows_router._create_virtual_window_for_client(
                client,
                WindowCreateIn(cwd="/tmp", shell_command="/bin/bash"),
                session,
                CancellingTmuxManager(),
                session_factory=db_client.session_factory,
            )

    async with db_client.session_factory() as session:
        assert session.in_transaction() is False
        windows = (
            await session.execute(
                select(VirtualWindow).where(VirtualWindow.client_id == client.id)
            )
        ).scalars().all()


    assert windows == []


@pytest.mark.asyncio
async def test_create_window_with_folder_path_assigns_topic_folder(db_client):
    client_id = await get_local_client_id(db_client)
    response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={
            "cwd": "/tmp/project",
            "shell_command": "/bin/bash",
            "folder_path": "/开发调试/后端摘要",
        },
    )
    assert response.status_code == 200
    created = response.json()

    tree_response = await db_client.get(f"/api/clients/{client_id}/tree")
    tree = tree_response.json()

    def find_folder(nodes, path):
        for node in nodes:
            if node["path"] == path:
                return node
            child = find_folder(node["folders"], path)
            if child is not None:
                return child
        return None

    target_folder = find_folder(tree, "/开发调试/后端摘要")
    assert target_folder is not None
    assert any(window["id"] == created["id"] for window in target_folder["windows"])
    assert created["folder_manually_overridden"] is True
    assert created["cwd"] == "/tmp/project"


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
    connection = AgentConfigRemoteConnection()
    app.state.client_connections = FakeConnectionRegistry(connection)

    response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={"cwd": "/tmp/ignored", "shell_command": "/bin/bash"},
    )

    assert response.status_code == 200
    created = response.json()
    await allow_remote_create_to_finish(connection)
    persisted = await wait_for_remote_window_ready(db_client, remote_client_id, created["id"])
    create_payload = connection.requests[0].payload
    create_payload.pop("agent_config_selection", None)
    assert created["client_id"] == remote_client_id
    assert created["tmux_session"] is None
    assert created["tmux_window_id"] is None
    assert created["remote_session_id"] is None
    assert created["remote_window_id"] is None
    assert persisted.remote_session_id == "remote-session"
    assert persisted.remote_window_id == "remote-window"
    assert len(connection.requests) == 1
    assert connection.requests[0].type == "create_window"
    assert connection.requests[0].client_id == UUID(remote_client_id)
    assert connection.requests[0].window_id == UUID(created["id"])
    assert create_payload == {"cwd": "/tmp/ignored", "shell_command": "/bin/bash"}
    assert created["cwd"] == "/tmp/ignored"
    assert created["shell_command"] == "/bin/bash"
    assert persisted.cwd == "/tmp/ignored"
    assert persisted.shell_command == "/bin/bash"
    assert FakeTmuxManager.killed_targets == []


@pytest.mark.asyncio
async def test_read_agent_clients_returns_builtin_plugin_descriptors(db_client):
    client_id = await get_local_client_id(db_client)

    response = await db_client.get(f"/api/clients/{client_id}/agent-clients")

    assert response.status_code == 200
    clients = {item["id"]: item for item in response.json()["agent_clients"]}
    assert clients["codex"]["provider_id"] == "codex"
    assert clients["codex"]["capabilities"]["agent_records"] is True
    assert clients["claude"]["provider_id"] == "claude_code"
    assert clients["cursor"]["default_command"] == "agent"
    assert "cursor-agent" in clients["cursor"]["command_names"]


@pytest.mark.asyncio
async def test_read_agent_clients_queries_remote_client_descriptors(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = FakeRemoteConnection()
    connection.request_continue.set()
    app.state.client_connections = FakeConnectionRegistry(connection)

    response = await db_client.get(f"/api/clients/{remote_client_id}/agent-clients")

    assert response.status_code == 200
    assert connection.requests[0].type == "agent_clients_list"
    clients = {item["id"]: item for item in response.json()["agent_clients"]}
    assert clients == {
        "remote_codex": {
            "id": "remote_codex",
            "provider_id": "codex",
            "label": "Remote Codex",
            "aliases": [],
            "default_command": "codex",
            "command_names": ["codex"],
            "capabilities": {},
        }
    }


@pytest.mark.asyncio
async def test_create_agent_window_sends_remote_config_selection(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = AgentConfigRemoteConnection()
    app.state.client_connections = FakeConnectionRegistry(connection)

    response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={
            "cwd": "/tmp/project",
            "agent_launch": {
                "agent": "claude",
                "command": "claude",
                "config": {
                    "agent": "claude",
                    "sections": [
                        {
                            "id": "skills",
                            "items": [{"id": "review", "enabled": True}],
                        }
                    ],
                },
            },
        },
    )

    assert response.status_code == 200
    created = response.json()
    await allow_remote_create_to_finish(connection)
    assert created["shell_command"] == "claude"
    assert created["command_capture_supported"] is True
    assert "claude_code" in created["runtime_tags"]
    assert [request.type for request in connection.requests[:2]] == ["agent_clients_list", "create_window"]
    assert connection.requests[1].payload == {
        "cwd": "/tmp/project",
        "shell_command": "claude",
        "agent_config_selection": {
            "agent": "claude",
            "sections": [
                {
                    "id": "skills",
                    "items": [{"id": "review", "enabled": True}],
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_create_remote_agent_window_allows_remote_only_agent_descriptor(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = FutureAgentRemoteConnection()
    app.state.client_connections = FakeConnectionRegistry(connection)

    response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={
            "cwd": "/tmp/project",
            "agent_launch": {
                "agent": "future_agent",
                "command": "future-agent",
                "config": {
                    "agent": "future_agent",
                    "sections": [
                        {
                            "id": "skills",
                            "items": [{"id": "review", "enabled": True}],
                        }
                    ],
                },
            },
        },
    )

    assert response.status_code == 200
    created = response.json()
    await allow_remote_create_to_finish(connection)
    assert created["shell_command"] == "future-agent"
    assert "future_provider" not in created["runtime_tags"]
    assert [request.type for request in connection.requests[:2]] == ["agent_clients_list", "create_window"]
    assert connection.requests[1].payload == {
        "cwd": "/tmp/project",
        "shell_command": "future-agent",
        "agent_config_selection": {
            "agent": "future_agent",
            "sections": [
                {
                    "id": "skills",
                    "items": [{"id": "review", "enabled": True}],
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_create_remote_agent_window_rejects_launch_disabled_descriptor(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = CapabilityRemoteConnection({"launch": False})
    app.state.client_connections = FakeConnectionRegistry(connection)

    response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={
            "cwd": "/tmp/project",
            "agent_launch": {"agent": "restricted_agent", "command": "restricted-agent"},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "agent client does not support launch"
    assert [request.type for request in connection.requests] == ["agent_clients_list"]


@pytest.mark.asyncio
async def test_create_remote_window_returns_before_runtime_create_finishes(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = AgentConfigRemoteConnection()
    app.state.client_connections = FakeConnectionRegistry(connection)

    response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={"cwd": "/tmp/slow", "shell_command": "/bin/bash"},
    )

    assert response.status_code == 200
    created = response.json()
    assert created["remote_session_id"] is None
    assert created["remote_window_id"] is None

    await allow_remote_create_to_finish(connection)
    persisted = await wait_for_remote_window_ready(db_client, remote_client_id, created["id"])
    assert persisted.remote_session_id == "remote-session"
    assert persisted.remote_window_id == "remote-window"


@pytest.mark.asyncio
async def test_create_window_commits_client_lookup_before_remote_create(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = ObservingRemoteConnection()
    ObservingRemoteConnection.observed_in_transaction = None
    app.state.client_connections = FakeConnectionRegistry(connection)

    async with db_client.session_factory() as session:
        windows_router._TEST_OBSERVED_SESSION = session
        try:
            response = await db_client.post(
                f"/api/clients/{remote_client_id}/windows",
                json={"cwd": "/tmp/remote", "shell_command": "/bin/bash"},
            )
        finally:
            delattr(windows_router, "_TEST_OBSERVED_SESSION")

    assert response.status_code == 200
    await allow_remote_create_to_finish(connection)
    await wait_for_remote_window_ready(db_client, remote_client_id, response.json()["id"])
    assert ObservingRemoteConnection.observed_in_transaction is False


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
async def test_create_window_marks_remote_window_disconnected_when_runtime_create_becomes_unavailable(
    db_client, remote_exc
):
    remote_client_id = await create_remote_client_id(db_client)
    connection = FailingRemoteConnection(remote_exc)
    connection.request_continue.set()
    app.state.client_connections = FakeConnectionRegistry(connection)

    response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={"cwd": "/tmp", "shell_command": "/bin/bash"},
    )

    assert response.status_code == 200
    created = response.json()
    assert FakeTmuxManager.killed_targets == []

    persisted = await wait_for_remote_window_status(db_client, created["id"], "DISCONNECTED")
    assert persisted.remote_session_id is None
    assert persisted.remote_window_id is None


@pytest.mark.asyncio
async def test_create_window_marks_remote_window_error_when_runtime_reports_terminal_error(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = TerminalErrorRemoteConnection()
    connection.request_continue.set()
    app.state.client_connections = FakeConnectionRegistry(connection)

    response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={"cwd": "/tmp", "shell_command": "/bin/bash"},
    )

    assert response.status_code == 200
    created = response.json()

    persisted = await wait_for_remote_window_status(db_client, created["id"], "ERROR")
    assert persisted.remote_session_id is None
    assert persisted.remote_window_id is None


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
    assert get_response.json()["created_at"] == created["created_at"]
    assert get_response.json()["last_terminal_command_at"] is None
    assert get_response.json()["last_agent_event_at"] is None
    assert parse_response_datetime(get_response.json()["last_active_at"]) == parse_response_datetime(
        created["created_at"]
    )


@pytest.mark.asyncio
async def test_get_window_returns_overview_timestamps(db_client):
    client_id = await get_local_client_id(db_client)
    create_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp/project", "shell_command": "/bin/bash"}
    )
    window_id = UUID(create_response.json()["id"])
    created_at = datetime(2026, 5, 29, 10, 0, tzinfo=timezone.utc)
    command_at = created_at + timedelta(minutes=5)
    agent_at = created_at + timedelta(minutes=9)
    output_at = created_at + timedelta(minutes=10)
    recent_at = created_at + timedelta(minutes=12)

    async with db_client.session_factory() as session:
        window = await session.get(VirtualWindow, window_id)
        assert window is not None
        window.created_at = created_at
        window.terminal_last_output_at = output_at
        session.add_all(
            [
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.terminal,
                    source_id=str(window_id),
                    kind="terminal_input_command",
                    virtual_window_id=window_id,
                    payload_json={"command": "pytest", "sequence": 1},
                    fingerprint=f"test-command:{window_id}",
                    created_at=command_at,
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="codex-session",
                    kind="assistant_message",
                    virtual_window_id=window_id,
                    payload_json=codex_message_payload("done"),
                    fingerprint=f"test-agent:{window_id}",
                    created_at=agent_at,
                ),
                TerminalRecentUsage(
                    client_id=UUID(client_id),
                    window_id=window_id,
                    title="Terminal selected",
                    last_used_at=recent_at,
                ),
            ]
        )
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")

    assert response.status_code == 200
    body = response.json()
    assert parse_response_datetime(body["created_at"]) == created_at
    assert parse_response_datetime(body["last_terminal_command_at"]) == command_at
    assert parse_response_datetime(body["last_agent_event_at"]) == agent_at
    assert parse_response_datetime(body["last_active_at"]) == recent_at


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
async def test_patch_window_records_title_history_with_summary_snapshot(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows", json={"cwd": "/tmp", "shell_command": "/bin/bash"}
    )
    window_id = window_response.json()["id"]

    summary_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_id}",
        json={"summary": "First summary."},
    )
    title_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_id}",
        json={"title": "Manual title"},
    )
    history_response = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/title-history"
    )

    assert summary_response.status_code == 200
    assert title_response.status_code == 200
    assert history_response.status_code == 200
    body = history_response.json()
    assert body["total"] == 3
    assert body["has_more"] is False
    assert [(item["title"], item["summary"], item["source"]) for item in body["items"]] == [
        ("Manual title", "First summary.", "manual"),
        (window_response.json()["title"], "First summary.", "manual"),
        (window_response.json()["title"], None, "initial"),
    ]


@pytest.mark.asyncio
async def test_title_history_endpoint_is_client_scoped(db_client):
    local_client_id = await get_local_client_id(db_client)
    remote_client_id = await create_remote_client_id(db_client)
    async with db_client.session_factory() as session:
        remote_window = await create_window(session, UUID(remote_client_id), cwd="/tmp", shell_command="/bin/bash")
        await session.commit()

    response = await db_client.get(
        f"/api/clients/{local_client_id}/windows/{remote_window.id}/title-history"
    )

    assert response.status_code == 404


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
async def test_delete_last_window_removes_empty_topic_branch_and_summary_jobs(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={
            "cwd": "/tmp/project",
            "shell_command": "/bin/bash",
            "folder_path": "/开发调试/后端摘要",
        },
    )
    window_id = window_response.json()["id"]
    cached_tree_response = await db_client.get(f"/api/clients/{client_id}/tree")
    assert cached_tree_response.status_code == 200
    assert _tree_contains_path(cached_tree_response.json(), "/开发调试/后端摘要")

    async with db_client.session_factory() as session:
        session.add(
            SummaryJob(
                virtual_window_id=UUID(window_id),
                status=SummaryJobStatus.pending,
            )
        )
        await session.commit()

    delete_response = await db_client.delete(f"/api/clients/{client_id}/windows/{window_id}")

    assert delete_response.status_code == 204

    async with db_client.session_factory() as session:
        assert await session.get(VirtualWindow, UUID(window_id)) is None
        summary_jobs = list(await session.scalars(select(SummaryJob)))
        folders = list(await session.scalars(select(Folder).order_by(Folder.path)))

    assert summary_jobs == []
    assert "/开发调试" not in [folder.path for folder in folders]
    assert "/开发调试/后端摘要" not in [folder.path for folder in folders]

    tree_response = await db_client.get(f"/api/clients/{client_id}/tree")
    assert tree_response.status_code == 200
    assert not _tree_contains_path(tree_response.json(), "/开发调试")
    assert not _tree_contains_path(tree_response.json(), "/开发调试/后端摘要")


@pytest.mark.asyncio
async def test_delete_window_keeps_topic_when_another_terminal_remains(db_client):
    client_id = await get_local_client_id(db_client)
    first_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={
            "cwd": "/tmp/first",
            "shell_command": "/bin/bash",
            "folder_path": "/开发调试/后端摘要",
        },
    )
    second_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={
            "cwd": "/tmp/second",
            "shell_command": "/bin/bash",
            "folder_path": "/开发调试/后端摘要",
        },
    )

    delete_response = await db_client.delete(
        f"/api/clients/{client_id}/windows/{first_response.json()['id']}"
    )

    assert delete_response.status_code == 204

    tree_response = await db_client.get(f"/api/clients/{client_id}/tree")
    tree = tree_response.json()

    def find_folder(nodes, path):
        for node in nodes:
            if node["path"] == path:
                return node
            child = find_folder(node["folders"], path)
            if child is not None:
                return child
        return None

    target_folder = find_folder(tree, "/开发调试/后端摘要")
    assert target_folder is not None
    assert [window["id"] for window in target_folder["windows"]] == [second_response.json()["id"]]


def _tree_contains_path(nodes, path):
    for node in nodes:
        if node["path"] == path:
            return True
        if _tree_contains_path(node["folders"], path):
            return True
    return False


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
    await allow_remote_create_to_finish(connection)
    await wait_for_remote_window_ready(db_client, remote_client_id, window_id)

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
async def test_terminal_projects_and_project_scoped_tree_use_runtime_project_path(db_client):
    client_id = await get_local_client_id(db_client)
    first_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/first", "shell_command": "/bin/bash"},
    )
    second_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/second", "shell_command": "/bin/bash"},
    )
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_window_id = first_response.json()["id"]
    second_window_id = second_response.json()["id"]

    async with db_client.session_factory() as session:
        session.add(
            AiSession(
                client_id=UUID(client_id),
                provider="codex",
                source_id="codex-project-first",
                project_path="/workspace/shared",
                virtual_window_id=UUID(first_window_id),
            )
        )
        await session.commit()

    projects_response = await db_client.get(f"/api/clients/{client_id}/terminal-projects")
    assert projects_response.status_code == 200
    assert projects_response.json() == [
        {"project_path": "/tmp/second", "window_count": 1},
        {"project_path": "/workspace/shared", "window_count": 1},
    ]

    tree_response = await db_client.get(
        f"/api/clients/{client_id}/tree",
        params={"project_path": "/workspace/shared"},
    )
    assert tree_response.status_code == 200
    tree_window_ids = {
        window["id"]
        for folder in tree_response.json()
        for window in folder["windows"]
    }
    assert tree_window_ids == {first_window_id}

    activity_response = await db_client.get(
        f"/api/clients/{client_id}/windows/activity",
        params={"include_runtime_tags": "true", "project_path": "/workspace/shared"},
    )
    assert activity_response.status_code == 200
    assert [
        item["window_id"] for item in activity_response.json()["windows"]
    ] == [first_window_id]
    assert activity_response.json()["windows"][0]["runtime_tags"] == ["codex", "/workspace/shared"]


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
    assert body["events"][0]["projection"] | {
        "tone": "user-input",
        "label": "User input",
        "body": "hello",
        "body_format": "markdown",
        "subtype": "user_message",
    } == body["events"][0]["projection"]
    assert body["events"][1]["projection"] | {
        "tone": "terminal",
        "label": "Terminal command",
        "body": "codex",
        "body_format": "markdown",
        "subtype": "command",
    } == body["events"][1]["projection"]
    assert body["events_total"] == 2
    assert body["events_limit"] == 100
    assert body["events_offset"] == 0
    assert body["events_has_more"] is False


@pytest.mark.asyncio
async def test_get_window_agent_config_detects_local_agent_from_terminal_command(
    db_client,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(windows_router.agent_config_service.Path, "home", lambda: tmp_path)
    codex_home = tmp_path / ".codex"
    skill_dir = codex_home / "skills" / "docker"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: docker\n---\n", encoding="utf-8")
    (codex_home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "hooks/preflight.sh",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/workspace/project", "shell_command": "/bin/bash"},
    )
    assert window_response.status_code == 200
    window_id = window_response.json()["id"]

    async with db_client.session_factory() as session:
        session.add(
            Event(
                client_id=UUID(client_id),
                virtual_window_id=UUID(window_id),
                source_type=EventSourceType.terminal,
                source_id="terminal",
                kind="terminal_input_command",
                payload_json={"command": "codex exec 'fix tests'"},
                fingerprint="agent-config-codex-command",
            )
        )
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}/agent-config")

    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "codex"
    skills = next(section for section in body["sections"] if section["id"] == "skills")
    assert skills["items"][0]["id"] == "docker"

    patch_response = await db_client.patch(
        f"/api/clients/{client_id}/windows/{window_id}/agent-config/hooks/UserPromptSubmit:hooks%2Fpreflight.sh",
        json={"enabled": False},
    )
    assert patch_response.status_code == 200
    updated_hooks = next(section for section in patch_response.json()["sections"] if section["id"] == "hooks")
    assert updated_hooks["items"][0]["id"] == "UserPromptSubmit:hooks/preflight.sh"
    assert updated_hooks["items"][0]["enabled"] is False


@pytest.mark.asyncio
async def test_remote_window_agent_config_uses_client_agent_request(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = AgentConfigRemoteConnection()
    app.state.client_connections = FakeConnectionRegistry(connection)
    create_response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={"cwd": "/workspace/project", "shell_command": "claude"},
    )
    assert create_response.status_code == 200
    window_id = create_response.json()["id"]
    await allow_remote_create_to_finish(connection)
    await wait_for_remote_window_ready(db_client, remote_client_id, window_id)

    get_response = await db_client.get(
        f"/api/clients/{remote_client_id}/windows/{window_id}/agent-config"
    )
    assert get_response.status_code == 200
    assert get_response.json()["agent"] == "claude"

    patch_response = await db_client.patch(
        f"/api/clients/{remote_client_id}/windows/{window_id}/agent-config/skills/review",
        json={"enabled": False},
    )
    assert patch_response.status_code == 200

    config_requests = [request for request in connection.requests if request.type.startswith("agent_config")]
    assert [request.type for request in config_requests] == [
        "agent_config_get",
        "agent_config_set_enabled",
    ]
    assert config_requests[0].payload["agent"] == "claude_code"
    assert config_requests[1].payload == {
        "agent": "claude_code",
        "section_id": "skills",
        "item_id": "review",
        "enabled": False,
    }


@pytest.mark.asyncio
async def test_client_agent_config_endpoint_uses_remote_agent_config_request(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = AgentConfigRemoteConnection()
    app.state.client_connections = FakeConnectionRegistry(connection)

    response = await db_client.get(f"/api/clients/{remote_client_id}/agent-config/claude")

    assert response.status_code == 200
    assert response.json()["agent"] == "claude"
    assert [request.type for request in connection.requests[:2]] == ["agent_clients_list", "agent_config_get"]
    assert connection.requests[1].window_id is None
    assert connection.requests[1].payload == {"agent": "claude_code"}


@pytest.mark.asyncio
async def test_client_agent_config_rejects_client_config_disabled_descriptor(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = CapabilityRemoteConnection({"client_config": False})
    app.state.client_connections = FakeConnectionRegistry(connection)

    response = await db_client.get(f"/api/clients/{remote_client_id}/agent-config/restricted_agent")

    assert response.status_code == 400
    assert response.json()["detail"] == "agent client does not support client_config"
    assert [request.type for request in connection.requests] == ["agent_clients_list"]


@pytest.mark.asyncio
async def test_remote_agent_config_endpoints_allow_remote_only_agent_descriptor(db_client):
    remote_client_id = await create_remote_client_id(db_client)
    connection = FutureAgentRemoteConnection()
    app.state.client_connections = FakeConnectionRegistry(connection)

    create_response = await db_client.post(
        f"/api/clients/{remote_client_id}/windows",
        json={
            "cwd": "/workspace/project",
            "agent_launch": {"agent": "future_agent", "command": "future-agent"},
        },
    )
    assert create_response.status_code == 200
    window_id = create_response.json()["id"]
    await allow_remote_create_to_finish(connection)
    await wait_for_remote_window_ready(db_client, remote_client_id, window_id)

    client_response = await db_client.get(f"/api/clients/{remote_client_id}/agent-config/future_agent")
    assert client_response.status_code == 200
    assert client_response.json()["agent"] == "future_agent"

    window_response = await db_client.get(
        f"/api/clients/{remote_client_id}/windows/{window_id}/agent-config"
    )
    assert window_response.status_code == 200
    assert window_response.json()["agent"] == "future_agent"

    config_requests = [request for request in connection.requests if request.type == "agent_config_get"]
    assert [request.payload["agent"] for request in config_requests] == [
        "future_agent",
        "future_agent",
    ]


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
async def test_get_window_agent_record_chat_stops_after_requested_page(db_client):
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
                    payload_json=codex_user_message_payload(f"message {index}"),
                    fingerprint=f"agent-record-chat-page-{index}",
                    created_at=base_time + timedelta(milliseconds=index),
                )
                for index in range(600)
            ]
        )
        await session.commit()

    response = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/agent-record/chat?messages_limit=30"
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["body"] for item in body["messages"]] == [f"message {index}" for index in range(30)]
    assert body["messages_total"] == 31
    assert body["messages_total_exact"] is False
    assert body["messages_has_more"] is True


@pytest.mark.asyncio
async def test_get_window_agent_record_chat_filters_by_role(db_client):
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
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hello"}],
                        },
                    },
                    fingerprint="agent-record-chat-role-user",
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
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    },
                    fingerprint="agent-record-chat-role-agent",
                    created_at=base_time + timedelta(milliseconds=1),
                ),
            ]
        )
        await session.commit()

    response = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/agent-record/chat?role=agent"
    )

    assert response.status_code == 200
    body = response.json()
    assert [(item["role"], item["body"]) for item in body["messages"]] == [("agent", "done")]
    assert body["messages_total"] == 1
    assert body["messages_has_more"] is False


@pytest.mark.asyncio
async def test_get_window_agent_record_chat_distinguishes_claude_subagent_messages(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/project", "shell_command": "/bin/bash"},
    )
    window_id = UUID(window_response.json()["id"])

    async with db_client.session_factory() as session:
        base_time = datetime.now(timezone.utc)
        main_session = AiSession(
            client_id=UUID(client_id),
            provider="claude_code",
            source_id="main-session-1",
            virtual_window_id=window_id,
            created_at=base_time,
            updated_at=base_time,
        )
        sub_session = AiSession(
            client_id=UUID(client_id),
            provider="claude_code",
            source_id="agent-subagent-1",
            source_path="/tmp/main-session-1/subagents/agent-subagent-1.jsonl",
            virtual_window_id=window_id,
            created_at=base_time + timedelta(milliseconds=1),
            updated_at=base_time + timedelta(milliseconds=1),
        )
        session.add_all([main_session, sub_session])
        await session.flush()
        session.add_all(
            [
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="main-session-1",
                    kind="assistant_message",
                    virtual_window_id=window_id,
                    ai_session_id=main_session.id,
                    payload_json={
                        "provider": "claude_code",
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "call-subagent-1",
                                    "name": "Agent",
                                    "input": {"description": "Return one", "prompt": "Return exactly: 1"},
                                }
                            ],
                        },
                        "subagent_tool_use_results": [
                            {"tool_use_id": "call-subagent-1", "agent_id": "subagent-1"}
                        ],
                    },
                    fingerprint="agent-record-chat-subagent-call",
                    created_at=base_time,
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="agent-subagent-1",
                    kind="user_message",
                    virtual_window_id=window_id,
                    ai_session_id=sub_session.id,
                    payload_json={
                        "provider": "claude_code",
                        "type": "user",
                        "sessionId": "main-session-1",
                        "agentId": "subagent-1",
                        "isSidechain": True,
                        "subagent": {"toolUseId": "call-subagent-1"},
                        "message": {"role": "user", "content": "Return exactly: 1"},
                    },
                    fingerprint="agent-record-chat-subagent-prompt",
                    created_at=base_time + timedelta(milliseconds=1),
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="main-session-1",
                    kind="user_message",
                    virtual_window_id=window_id,
                    ai_session_id=main_session.id,
                    payload_json={
                        "provider": "claude_code",
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {"type": "tool_result", "tool_use_id": "call-subagent-1", "content": "1"}
                            ],
                        },
                        "toolUseResult": {"agentId": "subagent-1", "toolUseId": "call-subagent-1"},
                    },
                    fingerprint="agent-record-chat-subagent-result",
                    created_at=base_time + timedelta(milliseconds=2),
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="agent-subagent-1",
                    kind="assistant_message",
                    virtual_window_id=window_id,
                    ai_session_id=sub_session.id,
                    payload_json={
                        "provider": "claude_code",
                        "type": "assistant",
                        "sessionId": "main-session-1",
                        "agentId": "subagent-1",
                        "isSidechain": True,
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "subagent internal answer"}]},
                    },
                    fingerprint="agent-record-chat-subagent-internal-answer",
                    created_at=base_time + timedelta(milliseconds=3),
                ),
            ]
        )
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}/agent-record/chat")

    assert response.status_code == 200
    body = response.json()
    assert [(item["role"], item["agent_message_type"], item["body"]) for item in body["messages"]] == [
        ("agent", "subagent_call", "Description: Return one\n\nReturn exactly: 1"),
        ("agent", "subagent_result", "1"),
        ("agent", "agent", "subagent internal answer"),
    ]
    assert body["messages"][0]["target_session_id"] == str(sub_session.id)
    assert body["messages"][0]["target_session_source_id"] == "agent-subagent-1"
    assert body["messages_total"] == 3

    filtered = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/agent-record/chat?role=agent"
    )
    assert filtered.status_code == 200
    assert [(item["agent_message_type"], item["body"]) for item in filtered.json()["messages"]] == [
        ("agent", "subagent internal answer")
    ]

    subagent_calls = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/agent-record/chat?role=subagent_call"
    )
    assert subagent_calls.status_code == 200
    assert [item["agent_message_type"] for item in subagent_calls.json()["messages"]] == ["subagent_call"]

    main_chat = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/agent-record/chat?session_id={main_session.id}"
    )
    assert main_chat.status_code == 200
    assert [(item["agent_message_type"], item["body"]) for item in main_chat.json()["messages"]] == [
        ("subagent_call", "Description: Return one\n\nReturn exactly: 1"),
        ("subagent_result", "1"),
    ]

    subagent_chat = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/agent-record/chat?session_id={sub_session.id}"
    )
    assert subagent_chat.status_code == 200
    assert [(item["agent_message_type"], item["body"]) for item in subagent_chat.json()["messages"]] == [
        ("subagent_call", "Return exactly: 1"),
        ("agent", "subagent internal answer")
    ]
    assert subagent_chat.json()["messages"][0]["subagent_tool_use_id"] == "call-subagent-1"

    subagent_detail = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/agent-record/detail?session_id={sub_session.id}"
    )
    assert subagent_detail.status_code == 200
    detail_body = subagent_detail.json()
    assert [item["id"] for item in detail_body["sessions"]] == [str(main_session.id), str(sub_session.id)]
    assert [item["ai_session_id"] for item in detail_body["events"]] == [str(sub_session.id), str(sub_session.id)]


@pytest.mark.asyncio
async def test_get_window_agent_record_chat_filters_agent_default_user_inputs(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/project", "shell_command": "/bin/bash"},
    )
    window_id = UUID(window_response.json()["id"])

    async with db_client.session_factory() as session:
        base_time = datetime.now(timezone.utc)
        session.add_all(
            [
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="cursor-session-1",
                    kind="user_message",
                    virtual_window_id=window_id,
                    payload_json={
                        "provider": "cursor_cli",
                        "role": "user",
                        "content": "<user_info>\nOS Version: linux\n</user_info>",
                    },
                    fingerprint="agent-record-chat-cursor-user-info",
                    created_at=base_time,
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="cursor-session-1",
                    kind="user_message",
                    virtual_window_id=window_id,
                    payload_json={
                        "provider": "cursor_cli",
                        "role": "user",
                        "content": "<user_query>\n修复 summary 输入过滤\n</user_query>",
                    },
                    fingerprint="agent-record-chat-cursor-query",
                    created_at=base_time + timedelta(milliseconds=1),
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window_id,
                    payload_json={
                        "provider": "codex",
                        "raw_type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "# AGENTS.md instructions for /workspace\n\n<INSTRUCTIONS>...</INSTRUCTIONS>",
                                }
                            ],
                        },
                    },
                    fingerprint="agent-record-chat-codex-context",
                    created_at=base_time + timedelta(milliseconds=2),
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="claude-session-1",
                    kind="user_message",
                    virtual_window_id=window_id,
                    payload_json={
                        "provider": "claude_code",
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool-1",
                                    "content": "pytest output",
                                }
                            ],
                        },
                    },
                    fingerprint="agent-record-chat-claude-tool-result",
                    created_at=base_time + timedelta(milliseconds=3),
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window_id,
                    payload_json={
                        "provider": "codex",
                        "raw_type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    },
                    fingerprint="agent-record-chat-codex-agent",
                    created_at=base_time + timedelta(milliseconds=4),
                ),
            ]
        )
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}/agent-record/chat")

    assert response.status_code == 200
    assert [(item["role"], item["body"]) for item in response.json()["messages"]] == [
        ("user", "修复 summary 输入过滤"),
        ("agent", "done"),
    ]


@pytest.mark.asyncio
async def test_get_window_agent_record_chat_excludes_terminal_commands(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/project", "shell_command": "/bin/bash"},
    )
    window_id = UUID(window_response.json()["id"])

    async with db_client.session_factory() as session:
        base_time = datetime.now(timezone.utc)
        session.add_all(
            [
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.terminal,
                    source_id=str(window_id),
                    kind="terminal_input_command",
                    virtual_window_id=window_id,
                    payload_json={"command": "claude --resume claude-session", "sequence": 1},
                    fingerprint="agent-record-chat-terminal-agent-command",
                    created_at=base_time,
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.terminal,
                    source_id=str(window_id),
                    kind="terminal_input_command",
                    virtual_window_id=window_id,
                    payload_json={"command": "npm test", "sequence": 2},
                    fingerprint="agent-record-chat-terminal-plain-command",
                    created_at=base_time + timedelta(milliseconds=1),
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.agent_tool_record,
                    source_id="claude-session-1",
                    kind="assistant_message",
                    virtual_window_id=window_id,
                    payload_json={
                        "provider": "claude_code",
                        "type": "assistant",
                        "message": {"role": "assistant", "content": "real agent response"},
                    },
                    fingerprint="agent-record-chat-real-agent-response",
                    created_at=base_time + timedelta(milliseconds=2),
                ),
            ]
        )
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}/agent-record/chat")

    assert response.status_code == 200
    body = response.json()
    assert [(item["role"], item["body"]) for item in body["messages"]] == [
        ("agent", "real agent response")
    ]
    assert body["messages_total"] == 1


@pytest.mark.asyncio
async def test_get_window_command_history_returns_terminal_input_commands(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp/project", "shell_command": "/bin/bash"},
    )
    window_id = UUID(window_response.json()["id"])
    base_time = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    async with db_client.session_factory() as session:
        session.add_all(
            [
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.terminal,
                    source_id=str(window_id),
                    kind="terminal_input_command",
                    virtual_window_id=window_id,
                    payload_json={
                        "command": "npm test",
                        "shell": "bash",
                        "cwd": "/tmp/project",
                        "captured_at": base_time.isoformat(),
                        "sequence": 1,
                    },
                    fingerprint="command-history-1",
                    created_at=base_time,
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.terminal,
                    source_id=str(window_id),
                    kind="terminal_command_finished",
                    virtual_window_id=window_id,
                    payload_json={
                        "command": "npm test",
                        "shell": "bash",
                        "cwd": "/tmp/project",
                        "captured_at": (base_time + timedelta(seconds=2)).isoformat(),
                        "sequence": 1,
                        "exit_status": 0,
                    },
                    fingerprint=f"terminal_command_finished:{window_id}:1",
                    created_at=base_time + timedelta(seconds=2),
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.terminal,
                    source_id=str(window_id),
                    kind="terminal_output",
                    virtual_window_id=window_id,
                    payload_json={"text": "test output"},
                    fingerprint="command-history-output",
                    created_at=base_time + timedelta(seconds=3),
                ),
                Event(
                    client_id=UUID(client_id),
                    source_type=EventSourceType.terminal,
                    source_id=str(window_id),
                    kind="terminal_input_command",
                    virtual_window_id=window_id,
                    payload_json={
                        "command": "git status",
                        "shell": "zsh",
                        "cwd": "/tmp/project",
                        "captured_at": (base_time + timedelta(seconds=4)).isoformat(),
                        "sequence": 2,
                    },
                    fingerprint="command-history-2",
                    created_at=base_time + timedelta(seconds=4),
                ),
            ]
        )
        await session.commit()

    response = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/command-history?commands_limit=1"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["window_id"] == str(window_id)
    assert body["commands_total"] == 2
    assert body["commands_limit"] == 1
    assert body["commands_offset"] == 0
    assert body["commands_has_more"] is True
    assert body["commands"][0]["command"] == "git status"

    second_page = await db_client.get(
        f"/api/clients/{client_id}/windows/{window_id}/command-history?commands_limit=1&commands_offset=1"
    )
    assert second_page.status_code == 200
    command = second_page.json()["commands"][0]
    assert command["command"] == "npm test"
    assert command["shell"] == "bash"
    assert command["cwd"] == "/tmp/project"
    assert command["sequence"] == 1
    assert command["exit_status"] == 0
    assert command["captured_at"] == "2026-05-22T12:00:00Z"
    assert command["finished_at"] == "2026-05-22T12:00:02Z"


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
    projection = response.json()["events"][0]["projection"]
    assert projection | {
        "tone": "assistant",
        "label": "Assistant",
        "body": "fallback body",
        "body_format": "markdown",
        "subtype": "message",
    } == projection


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
    expected = [
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
    for projection, expected_projection in zip(projections, expected, strict=True):
        assert projection | expected_projection == projection


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
    assert work_status["label"] == "Terminal 活跃"
    assert work_status["color"] == "green"
    assert work_status["last_activity_at"] is not None
    assert work_status["last_working_activity_at"] is None


@pytest.mark.asyncio
async def test_get_window_returns_working_for_recent_agent_activity(db_client):
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
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_user_message_payload("fix tests"),
                fingerprint=f"agent_tool_record:{window.id}:recent-user",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=15),
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="assistant_message",
                virtual_window_id=window.id,
                payload_json={"provider": "codex", "role": "assistant", "content": "working"},
                fingerprint=f"agent_tool_record:{window.id}:recent",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            ),
        ])
        window_id = window.id
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")

    assert response.status_code == 200
    work_status = response.json()["work_status"]
    assert work_status["state"] == "WORKING"
    assert work_status["label"] == "Agent 工作中"
    assert work_status["color"] == "orange"
    assert work_status["last_activity_at"] is not None
    assert work_status["last_working_activity_at"] is not None


@pytest.mark.asyncio
async def test_get_window_does_not_mark_empty_agent_launch_as_working(db_client):
    client_id = await get_local_client_id(db_client)
    now = datetime.now(timezone.utc)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        session.add_all(
            [
                Event(
                    client_id=client.id,
                    source_type=EventSourceType.terminal,
                    source_id=str(window.id),
                    kind="terminal_input_command",
                    virtual_window_id=window.id,
                    payload_json={"command": "codex", "sequence": 43},
                    fingerprint=f"terminal_input_command:{window.id}:empty-codex",
                    created_at=now - timedelta(seconds=30),
                ),
                Event(
                    client_id=client.id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="codex-session",
                    kind="session_meta",
                    virtual_window_id=window.id,
                    payload_json={
                        "provider": "codex",
                        "raw_type": "session_meta",
                        "payload": {"id": "codex-session"},
                    },
                    fingerprint=f"agent_tool_record:{window.id}:empty-session-meta",
                    created_at=now - timedelta(seconds=10),
                ),
            ]
        )
        window_id = window.id
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")

    assert response.status_code == 200
    work_status = response.json()["work_status"]
    assert work_status["state"] == "RECENT_ACTIVE"
    assert work_status["last_working_activity_at"] is None


@pytest.mark.asyncio
async def test_get_window_ignores_old_agent_output_written_after_shell_exit(db_client):
    client_id = await get_local_client_id(db_client)
    now = datetime.now(timezone.utc)
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
                payload_json={"command": "codex exec 'done'", "sequence": 43},
                fingerprint=f"terminal_input_command:{window.id}:codex-old-output",
                created_at=now - timedelta(seconds=80),
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"command": "", "sequence": 43, "exit_status": 0},
                fingerprint=f"terminal_command_finished:{window.id}:codex-old-output",
                created_at=now - timedelta(seconds=20),
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_message_payload(
                    "old output inserted late",
                    timestamp=now - timedelta(seconds=40),
                ),
                fingerprint=f"agent_tool_record:{window.id}:codex-old-output",
                created_at=now - timedelta(seconds=5),
            ),
        ])
        window_id = window.id
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")

    assert response.status_code == 200
    work_status = response.json()["work_status"]
    assert work_status["state"] == "RECENT_ACTIVE"
    assert work_status["label"] == "Terminal 活跃"
    assert work_status["last_activity_at"] is not None
    assert work_status["last_working_activity_at"] is None


@pytest.mark.asyncio
async def test_windows_activity_returns_git_worktree_activity_without_remote_probe(db_client):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        session.add(
            WindowGitBinding(
                client_id=client.id,
                virtual_window_id=window.id,
                main_repo_root="/repo",
                worktree_root="/repo/.worktrees/feature",
                branch="feature",
                discovery_method="command",
            )
        )
        session.add(
            GitWorktreeRun(
                client_id=client.id,
                virtual_window_id=window.id,
                command_sequence="1",
                status="completed",
                pending_commit=True,
            )
        )
        window_id = str(window.id)
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert response.status_code == 200
    activity_window = next(
        item for item in response.json()["windows"] if item["window_id"] == window_id
    )
    assert activity_window["git_worktree"] == {
        "worktree_root": "/repo/.worktrees/feature",
        "main_repo_root": "/repo",
        "branch": "feature",
        "pending_commit": True,
    }


@pytest.mark.asyncio
async def test_windows_activity_range_filters_windows_by_recent_activity(db_client):
    client_id = await get_local_client_id(db_client)
    current = datetime.now(timezone.utc)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        old_window = await create_window(session, client.id, cwd="/old", shell_command="/bin/bash")
        recent_output_window = await create_window(
            session,
            client.id,
            cwd="/recent-output",
            shell_command="/bin/bash",
        )
        recent_created_window = await create_window(
            session,
            client.id,
            cwd="/recent-created",
            shell_command="/bin/bash",
        )
        old_window.created_at = current - timedelta(days=20)
        old_window.updated_at = current
        recent_output_window.created_at = current - timedelta(days=20)
        recent_output_window.updated_at = current - timedelta(days=20)
        recent_output_window.terminal_last_output_at = current - timedelta(days=2)
        recent_created_window.created_at = current - timedelta(days=2)
        recent_created_window.updated_at = current - timedelta(days=2)
        expected_window_ids = {str(recent_output_window.id), str(recent_created_window.id)}
        old_window_id = str(old_window.id)
        await session.commit()

    week_response = await db_client.get(f"/api/clients/{client_id}/windows/activity?range=7d")
    all_response = await db_client.get(f"/api/clients/{client_id}/windows/activity?range=all")

    assert week_response.status_code == 200
    assert all_response.status_code == 200
    week_window_ids = {item["window_id"] for item in week_response.json()["windows"]}
    all_window_ids = {item["window_id"] for item in all_response.json()["windows"]}
    assert week_window_ids == expected_window_ids
    assert old_window_id in all_window_ids
    assert expected_window_ids.issubset(all_window_ids)


@pytest.mark.asyncio
async def test_windows_activity_hot_cache_skips_client_and_activity_queries(db_client, monkeypatch):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        await session.commit()

    first_response = await db_client.get(f"/api/clients/{client_id}/windows/activity")
    assert first_response.status_code == 200

    async def fail_require_client(_session, _client_id):
        raise AssertionError("hot activity cache should avoid client lookup")

    async def fail_load_client_windows_activity(
        _session,
        _client_id,
        *,
        include_runtime_tags=False,
        visible_since=None,
        project_path=None,
    ):
        raise AssertionError("hot activity cache should avoid activity query")

    monkeypatch.setattr(folders_router, "_require_client", fail_require_client)
    monkeypatch.setattr(
        folders_router,
        "load_client_windows_activity",
        fail_load_client_windows_activity,
    )

    second_response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()


@pytest.mark.asyncio
async def test_windows_activity_expired_cache_serves_stale_response(db_client, monkeypatch):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        await session.commit()

    first_response = await db_client.get(f"/api/clients/{client_id}/windows/activity")
    assert first_response.status_code == 200
    refreshes = []

    async def fail_load_client_windows_activity(
        _session,
        _client_id,
        *,
        include_runtime_tags=False,
        visible_since=None,
        project_path=None,
    ):
        raise AssertionError("expired activity cache should return stale before refresh")

    monkeypatch.setattr(polling_response_cache, "_CACHE_TTL_SECONDS", -1.0)
    monkeypatch.setattr(
        folders_router,
        "load_client_windows_activity",
        fail_load_client_windows_activity,
    )
    monkeypatch.setattr(
        folders_router,
        "_refresh_response_cache",
        lambda cache_key, refresh: refreshes.append(cache_key),
    )

    second_response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()
    assert refreshes


@pytest.mark.asyncio
async def test_get_window_hot_cache_skips_detail_queries(db_client, monkeypatch):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp", "shell_command": "/bin/bash"},
    )
    window_id = window_response.json()["id"]

    first_response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")
    assert first_response.status_code == 200

    async def fail_require_client(_session, _client_id):
        raise AssertionError("hot window cache should avoid client lookup")

    async def fail_get_window_for_client(_session, _client_id, _window_id):
        raise AssertionError("hot window cache should avoid window lookup")

    async def fail_get_latest_summary_job(_session, _window_id):
        raise AssertionError("hot window cache should avoid summary job lookup")

    async def fail_runtime_tags_for_window_out(_session, _window):
        raise AssertionError("hot window cache should avoid runtime tag lookup")

    async def fail_load_work_status(_session, _client_id, _window_id):
        raise AssertionError("hot window cache should avoid work status lookup")

    monkeypatch.setattr(windows_router, "_require_client", fail_require_client)
    monkeypatch.setattr(windows_router, "get_window_for_client", fail_get_window_for_client)
    monkeypatch.setattr(windows_router, "get_latest_summary_job", fail_get_latest_summary_job)
    monkeypatch.setattr(windows_router, "runtime_tags_for_window_out", fail_runtime_tags_for_window_out)
    monkeypatch.setattr(windows_router, "load_work_status", fail_load_work_status)

    second_response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")

    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()


@pytest.mark.asyncio
async def test_record_terminal_recent_invalidates_window_hot_cache(db_client):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp", "shell_command": "/bin/bash"},
    )
    window_id = window_response.json()["id"]

    first_response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")
    assert first_response.status_code == 200

    recent_response = await db_client.post(
        f"/api/clients/{client_id}/terminal-recents",
        json={"window_id": window_id, "title": window_response.json()["title"]},
    )
    assert recent_response.status_code == 200

    second_response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")
    assert second_response.status_code == 200
    assert parse_response_datetime(second_response.json()["last_active_at"]) >= parse_response_datetime(
        recent_response.json()["last_used_at"]
    )
    assert second_response.json()["last_active_at"] != first_response.json()["last_active_at"]


@pytest.mark.asyncio
async def test_get_window_expired_cache_serves_stale_response(db_client, monkeypatch):
    client_id = await get_local_client_id(db_client)
    window_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp", "shell_command": "/bin/bash"},
    )
    window_id = window_response.json()["id"]

    first_response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")
    assert first_response.status_code == 200
    refreshes = []

    async def fail_get_window_for_client(_session, _client_id, _window_id):
        raise AssertionError("expired window cache should return stale before refresh")

    monkeypatch.setattr(polling_response_cache, "_CACHE_TTL_SECONDS", -1.0)
    monkeypatch.setattr(windows_router, "get_window_for_client", fail_get_window_for_client)
    monkeypatch.setattr(
        windows_router,
        "_refresh_window_response_cache",
        lambda cache_key, client_id, window_id: refreshes.append(cache_key),
    )

    second_response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}")

    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()
    assert refreshes


@pytest.mark.asyncio
async def test_create_window_invalidates_activity_hot_cache(db_client):
    client_id = await get_local_client_id(db_client)
    cached_empty_activity = await db_client.get(f"/api/clients/{client_id}/windows/activity")
    assert cached_empty_activity.status_code == 200
    assert cached_empty_activity.json() == {"windows": []}

    create_response = await db_client.post(
        f"/api/clients/{client_id}/windows",
        json={"cwd": "/tmp", "shell_command": "/bin/bash"},
    )
    assert create_response.status_code == 200

    activity_response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert activity_response.status_code == 200
    assert [
        item["window_id"] for item in activity_response.json()["windows"]
    ] == [create_response.json()["id"]]


@pytest.mark.asyncio
async def test_windows_activity_does_not_scan_agent_records_for_git_worktree_marker(db_client):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        session.add(
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json={
                    "provider": "codex",
                    "type": "function_call_output",
                    "output": f"Registered worktree\n{worktree_marker(window.id)}",
                },
                fingerprint=f"agent_tool_record:{window.id}:worktree-marker",
            )
        )
        window_id = str(window.id)
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert response.status_code == 200
    activity_window = next(
        item for item in response.json()["windows"] if item["window_id"] == window_id
    )
    assert activity_window.get("git_worktree") is None
    async with db_client.session_factory() as session:
        binding = await session.scalar(
            select(WindowGitBinding).where(WindowGitBinding.virtual_window_id == UUID(window_id))
        )
        runs = list(
            await session.scalars(
                select(GitWorktreeRun).where(GitWorktreeRun.virtual_window_id == UUID(window_id))
            )
        )
    assert binding is None
    assert runs == []


@pytest.mark.asyncio
async def test_window_git_runs_materializes_git_worktree_from_agent_record_marker(db_client):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        session.add(
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-2",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json={
                    "provider": "codex",
                    "payload": {
                        "type": "function_call_output",
                        "output": f"Registered worktree\n{worktree_marker(window.id)}",
                    },
                },
                fingerprint=f"agent_tool_record:{window.id}:worktree-git-runs",
            )
        )
        window_id = window.id
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}/git-runs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["supported"] is True
    assert payload["total"] == 1
    assert payload["runs"][0]["run_type"] == "tracking"
    assert payload["runs"][0]["worktree_root"] == "/repo/.worktrees/feature"
    assert payload["runs"][0]["main_repo_root"] == "/repo"
    assert payload["runs"][0]["discovery_method"] == "osc"


@pytest.mark.asyncio
async def test_window_git_runs_returns_commit_file_diff_payload(db_client):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        session.add(
            WindowGitBinding(
                client_id=client.id,
                virtual_window_id=window.id,
                main_repo_root="/repo",
                worktree_root="/repo/.worktrees/feature",
                branch="agent/feature",
                discovery_method="osc",
            )
        )
        session.add(
                GitWorktreeRun(
                    client_id=client.id,
                    virtual_window_id=window.id,
                    command_sequence=tracking_sequence("/repo/.worktrees/feature"),
                status="completed",
                main_repo_root="/repo",
                worktree_root="/repo/.worktrees/feature",
                discovery_method="osc",
                start_snapshot_json={"head_sha": "base"},
                end_snapshot_json={"head_sha": "feature"},
                session_diff_json={
                    "has_changes": True,
                    "head_moved": True,
                    "start_head": "base",
                    "end_head": "feature",
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
                    "files": [
                        {
                            "path": "frontend/src/components/TerminalPane.tsx",
                            "old_path": None,
                            "status": "modified",
                            "additions": 12,
                            "deletions": 4,
                            "commits": ["feature"],
                        }
                    ],
                },
            )
        )
        window_id = window.id
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/{window_id}/git-runs")

    assert response.status_code == 200
    diff = response.json()["runs"][0]["session_diff_json"]
    assert diff["commits"][0]["subject"] == "Fix terminal reload autofocus reconnect"
    assert diff["commits"][0]["files"][0]["path"] == "frontend/src/components/TerminalPane.tsx"
    assert diff["commits"][0]["files"][0]["patch"] == "@@ -1 +1 @@\n-old\n+new\n"
    assert diff["files"][0]["commits"] == ["feature"]


@pytest.mark.asyncio
async def test_windows_activity_returns_work_status_for_windows(db_client):
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
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_user_message_payload("fix tests"),
                fingerprint=f"agent_tool_record:{window.id}:activity-user",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="event_msg",
                virtual_window_id=window.id,
                payload_json={"provider": "codex", "raw_type": "event_msg", "payload": {"type": "agent_message", "message": "Working"}},
                fingerprint=f"agent_tool_record:{window.id}:recent",
                created_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            ),
        ])
        window_id = str(window.id)
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert response.status_code == 200
    activity_window = next(
        item for item in response.json()["windows"] if item["window_id"] == window_id
    )
    assert activity_window["work_status"]["state"] == "WORKING"


@pytest.mark.asyncio
async def test_windows_activity_ignores_old_agent_output_written_after_shell_exit(db_client):
    client_id = await get_local_client_id(db_client)
    now = datetime.now(timezone.utc)
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
                payload_json={"command": "codex exec 'done'", "sequence": 44},
                fingerprint=f"terminal_input_command:{window.id}:codex-old-output-activity",
                created_at=now - timedelta(seconds=80),
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"command": "", "sequence": 44, "exit_status": 0},
                fingerprint=f"terminal_command_finished:{window.id}:codex-old-output-activity",
                created_at=now - timedelta(seconds=20),
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_message_payload(
                    "old output inserted late",
                    timestamp=now - timedelta(seconds=40),
                ),
                fingerprint=f"agent_tool_record:{window.id}:codex-old-output-activity",
                created_at=now - timedelta(seconds=5),
            ),
        ])
        window_id = str(window.id)
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert response.status_code == 200
    activity_window = next(
        item for item in response.json()["windows"] if item["window_id"] == window_id
    )
    assert activity_window["work_status"]["state"] == "RECENT_ACTIVE"
    assert activity_window["work_status"]["last_working_activity_at"] is None
    assert activity_window["last_agent_task_status"] is None


@pytest.mark.asyncio
async def test_windows_activity_returns_to_working_after_completion_in_same_running_session(db_client):
    client_id = await get_local_client_id(db_client)
    now = datetime.now(timezone.utc)
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
                payload_json={"command": "codex", "sequence": 45},
                fingerprint=f"terminal_input_command:{window.id}:codex-multi-turn",
                created_at=now - timedelta(minutes=3),
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="event_msg",
                virtual_window_id=window.id,
                payload_json=codex_completion_payload(timestamp=now - timedelta(seconds=40)),
                fingerprint=f"agent_tool_record:{window.id}:codex-first-turn-complete",
                created_at=now - timedelta(seconds=40),
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_user_message_payload(
                    "second turn",
                    timestamp=now - timedelta(seconds=15),
                ),
                fingerprint=f"agent_tool_record:{window.id}:codex-second-turn-user",
                created_at=now - timedelta(seconds=15),
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_message_payload(
                    "second turn started",
                    timestamp=now - timedelta(seconds=10),
                ),
                fingerprint=f"agent_tool_record:{window.id}:codex-second-turn-output",
                created_at=now - timedelta(seconds=10),
            ),
        ])
        window_id = str(window.id)
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert response.status_code == 200
    activity_window = next(
        item for item in response.json()["windows"] if item["window_id"] == window_id
    )
    assert activity_window["work_status"]["state"] == "WORKING"
    assert activity_window["work_status"]["last_working_activity_at"] is not None
    assert activity_window["last_agent_task_status"] is None


@pytest.mark.asyncio
async def test_windows_activity_does_not_notify_for_agent_open_close_without_result(db_client):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        finished_at = started_at + timedelta(seconds=30)
        session.add_all([
            Event(
                client_id=client.id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "claude", "sequence": 9},
                fingerprint=f"terminal_input_command:{window.id}:claude-open",
                created_at=started_at,
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"command": "", "sequence": 9, "exit_status": 0},
                fingerprint=f"terminal_command_finished:{window.id}:claude-close",
                created_at=finished_at,
            ),
        ])
        window_id = str(window.id)
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert response.status_code == 200
    activity_window = next(
        item for item in response.json()["windows"] if item["window_id"] == window_id
    )
    assert activity_window["last_agent_task_completed_at"] is None
    assert activity_window["last_agent_task_status"] is None


@pytest.mark.asyncio
async def test_windows_activity_notifies_for_explicit_agent_completion(db_client):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        completed_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        session.add(
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="event_msg",
                virtual_window_id=window.id,
                payload_json={
                    "provider": "codex",
                    "raw_type": "event_msg",
                    "payload": {"type": "task_completed"},
                },
                fingerprint=f"agent_tool_record:{window.id}:codex-complete",
                created_at=completed_at,
            )
        )
        window_id = str(window.id)
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert response.status_code == 200
    activity_window = next(
        item for item in response.json()["windows"] if item["window_id"] == window_id
    )
    assert activity_window["work_status"]["state"] == "FINISHED"
    assert activity_window["last_agent_task_status"] == "FINISHED"
    assert activity_window["last_agent_task_status_at"] is not None


@pytest.mark.asyncio
async def test_windows_activity_notifies_for_codex_task_complete_event(db_client):
    client_id = await get_local_client_id(db_client)
    now = datetime.now(timezone.utc)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        completed_at = now - timedelta(seconds=30)
        session.add_all(
            [
                Event(
                    client_id=client.id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="codex-session",
                    kind="response_item",
                    virtual_window_id=window.id,
                    payload_json=codex_message_payload(
                        "finished",
                        timestamp=completed_at - timedelta(milliseconds=45),
                    ),
                    fingerprint=f"agent_tool_record:{window.id}:codex-final-message",
                    created_at=completed_at - timedelta(milliseconds=45),
                ),
                Event(
                    client_id=client.id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="codex-session",
                    kind="event_msg",
                    virtual_window_id=window.id,
                    payload_json=codex_completion_payload(
                        event_type="task_complete",
                        timestamp=completed_at,
                    ),
                    fingerprint=f"agent_tool_record:{window.id}:codex-task-complete",
                    created_at=completed_at,
                ),
            ]
        )
        window_id = str(window.id)
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert response.status_code == 200
    activity_window = next(
        item for item in response.json()["windows"] if item["window_id"] == window_id
    )
    assert activity_window["work_status"]["state"] == "FINISHED"
    assert activity_window["last_agent_task_status"] == "FINISHED"
    assert activity_window["last_agent_task_status_at"] is not None


@pytest.mark.asyncio
async def test_windows_activity_notifies_for_agent_abort(db_client):
    client_id = await get_local_client_id(db_client)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        now = datetime.now(timezone.utc)
        output_at = now - timedelta(minutes=61)
        session.add_all([
            Event(
                client_id=client.id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'hang'", "sequence": 10},
                fingerprint=f"terminal_input_command:{window.id}:codex-hang",
                created_at=now - timedelta(minutes=62),
            ),
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json={
                    "provider": "codex",
                    "raw_type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "still running"}],
                    },
                },
                fingerprint=f"agent_tool_record:{window.id}:codex-hang-output",
                created_at=output_at,
            ),
        ])
        window_id = str(window.id)
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/windows/activity")

    assert response.status_code == 200
    activity_window = next(
        item for item in response.json()["windows"] if item["window_id"] == window_id
    )
    assert activity_window["work_status"]["state"] == "ABORTED"
    assert activity_window["last_agent_task_status"] == "ABORTED"
    assert activity_window["last_agent_task_status_at"] is not None


@pytest.mark.asyncio
async def test_terminal_notifications_are_backend_stateful(db_client):
    client_id = await get_local_client_id(db_client)
    now = datetime.now(timezone.utc)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
        completed_at = now - timedelta(seconds=30)
        session.add(
            Event(
                client_id=client.id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="event_msg",
                virtual_window_id=window.id,
                payload_json=codex_completion_payload(timestamp=completed_at),
                fingerprint=f"agent_tool_record:{window.id}:codex-notification-complete",
                created_at=completed_at,
            )
        )
        window_id = str(window.id)
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/terminal-notifications")

    assert response.status_code == 200
    [notification] = response.json()["notifications"]
    assert notification["window_id"] == window_id
    assert notification["status"] == "FINISHED"
    assert notification["read"] is False

    read_response = await db_client.post(
        f"/api/clients/{client_id}/terminal-notifications/read",
        json={
            "window_id": window_id,
            "completed_at": notification["completed_at"],
        },
    )

    assert read_response.status_code == 200
    [read_notification] = read_response.json()["notifications"]
    assert read_notification["id"] == notification["id"]
    assert read_notification["read"] is True

    dismiss_response = await db_client.post(
        f"/api/clients/{client_id}/terminal-notifications/dismiss",
        json={
            "window_id": window_id,
            "completed_at": notification["completed_at"],
        },
    )

    assert dismiss_response.status_code == 200
    assert dismiss_response.json()["notifications"] == []
    assert (await db_client.get(f"/api/clients/{client_id}/terminal-notifications")).json()["notifications"] == []

    stale_response = await db_client.post(
        f"/api/clients/{client_id}/terminal-notifications/read",
        json={
            "window_id": window_id,
            "completed_at": (now + timedelta(days=1)).isoformat(),
        },
    )
    assert stale_response.status_code == 404


@pytest.mark.asyncio
async def test_terminal_notifications_clear_hides_current_notifications(db_client):
    client_id = await get_local_client_id(db_client)
    now = datetime.now(timezone.utc)
    async with db_client.session_factory() as session:
        client = await ensure_local_client(session)
        first = await create_window(session, client.id, cwd="/tmp/one", shell_command="/bin/bash")
        second = await create_window(session, client.id, cwd="/tmp/two", shell_command="/bin/bash")
        for index, window in enumerate((first, second)):
            completed_at = now - timedelta(seconds=30 + index)
            session.add(
                Event(
                    client_id=client.id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id=f"codex-session-{index}",
                    kind="event_msg",
                    virtual_window_id=window.id,
                    payload_json=codex_completion_payload(timestamp=completed_at),
                    fingerprint=f"agent_tool_record:{window.id}:codex-clear-complete",
                    created_at=completed_at,
                )
            )
        await session.commit()

    response = await db_client.get(f"/api/clients/{client_id}/terminal-notifications")
    assert response.status_code == 200
    assert len(response.json()["notifications"]) == 2

    clear_response = await db_client.delete(f"/api/clients/{client_id}/terminal-notifications")

    assert clear_response.status_code == 200
    assert clear_response.json()["notifications"] == []
    assert (await db_client.get(f"/api/clients/{client_id}/terminal-notifications")).json()["notifications"] == []
