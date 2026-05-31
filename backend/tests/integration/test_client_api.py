import traceback
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select

from app.db import Base, get_session
from app.main import app
from app.models import (
    AiSession,
    Client,
    ClientRegistrationKey,
    ClientRegistrationKeyStatus,
    ClientRuntime,
    Event,
    EventSourceType,
    Folder,
    ProjectSummary,
    SummaryJob,
    TerminalNotificationState,
    TerminalRecentUsage,
    VirtualWindow,
)
from app.repositories.client_registration_keys import create_registration_key
from app.routers import clients as clients_router
from app.repositories.clients import create_client, ensure_local_client
from app.repositories.folders import get_or_create_folder_by_path
from app.repositories.windows import create_window
from app.schemas import BootstrapClientIn
from app.services import polling_response_cache
from app.services.polling_response_cache import clear_polling_response_cache
from app.services.bootstrap.installer import BootstrapConnectionError, BootstrapDependencyError
from app.version import __version__


class DbClient:
    def __init__(self, client: AsyncClient, session_factory: async_sessionmaker):
        self._client = client
        self.session_factory = session_factory

    async def get(self, *args, **kwargs):
        return await self._client.get(*args, **kwargs)

    async def patch(self, *args, **kwargs):
        return await self._client.patch(*args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self._client.post(*args, **kwargs)

    async def delete(self, *args, **kwargs):
        return await self._client.delete(*args, **kwargs)


class FakeClientConnection:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeClientConnectionRegistry:
    def __init__(self, connection: FakeClientConnection | None) -> None:
        self.connection = connection
        self.unregistered: list[tuple[object, object]] = []

    def get(self, _client_id):
        return self.connection

    async def unregister(self, client_id, connection) -> None:
        self.unregistered.append((client_id, connection))
        if connection is self.connection:
            self.connection = None


@pytest.fixture
async def db_client(tmp_path):
    clear_polling_response_cache()
    database_path = tmp_path / "clients.db"
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

    app.dependency_overrides[get_session] = override_get_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as test_client:
            yield DbClient(test_client, session_factory)
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(clients_router.get_bootstrap_runner, None)
        app.dependency_overrides.pop(clients_router.get_update_runner, None)
        if hasattr(app.state, "client_connections"):
            delattr(app.state, "client_connections")
        clear_polling_response_cache()
        await engine.dispose()


BOOTSTRAP_PAYLOAD = {
    "name": "Remote Dev",
    "host": "dev.example.com",
    "port": 22,
    "username": "alice",
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n-----END OPENSSH PRIVATE KEY-----",
    "passphrase": "ssh-passphrase",
    "server_url": "https://control.example.com",
}
REPO_ROOT = Path(__file__).resolve().parents[3]


def _formatted_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


class CommitRecorder:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_list_clients_returns_local_client(db_client):
    response = await db_client.get("/api/clients")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "local"
    assert body[0]["status"] == "ONLINE"
    assert body[0]["runtime"] == "local"
    assert body[0]["version"] == __version__
    assert body[0]["last_update_at"] is not None
    assert "token_hash" not in body[0]


@pytest.mark.asyncio
async def test_list_clients_hot_cache_skips_repository(db_client, monkeypatch):
    first_response = await db_client.get("/api/clients")
    assert first_response.status_code == 200

    async def fail_list_clients(_session):
        raise AssertionError("hot clients cache should avoid the repository")

    monkeypatch.setattr(clients_router, "list_clients", fail_list_clients)

    second_response = await db_client.get("/api/clients")

    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()


@pytest.mark.asyncio
async def test_list_clients_expired_cache_serves_stale_response(db_client, monkeypatch):
    first_response = await db_client.get("/api/clients")
    assert first_response.status_code == 200
    refreshes = []

    async def fail_list_clients(_session):
        raise AssertionError("expired clients cache should return stale before refresh")

    monkeypatch.setattr(polling_response_cache, "_CACHE_TTL_SECONDS", -1.0)
    monkeypatch.setattr(clients_router, "list_clients", fail_list_clients)
    monkeypatch.setattr(clients_router, "_refresh_clients_cache", lambda cache_key: refreshes.append(cache_key))

    second_response = await db_client.get("/api/clients")

    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()
    assert refreshes


@pytest.mark.asyncio
async def test_get_client_returns_metadata(db_client):
    list_response = await db_client.get("/api/clients")
    client_id = list_response.json()[0]["id"]

    response = await db_client.get(f"/api/clients/{client_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == client_id
    assert body["name"] == "local"
    assert body["status"] == "ONLINE"
    assert body["runtime"] == "local"
    assert body["version"] == __version__
    assert body["last_update_at"] is not None
    assert body["connected_at"] is not None
    assert body["last_seen_at"] is not None


@pytest.mark.asyncio
async def test_patch_client_renames_client(db_client):
    list_response = await db_client.get("/api/clients")
    client_id = list_response.json()[0]["id"]

    response = await db_client.patch(f"/api/clients/{client_id}", json={"name": "Desk Mini"})

    assert response.status_code == 200
    assert response.json()["id"] == client_id
    assert response.json()["name"] == "Desk Mini"

    get_response = await db_client.get(f"/api/clients/{client_id}")
    assert get_response.json()["name"] == "Desk Mini"


@pytest.mark.asyncio
async def test_patch_client_invalidates_clients_hot_cache(db_client):
    list_response = await db_client.get("/api/clients")
    client_id = list_response.json()[0]["id"]

    response = await db_client.patch(f"/api/clients/{client_id}", json={"name": "Desk Mini"})
    assert response.status_code == 200

    list_after_patch = await db_client.get("/api/clients")

    assert list_after_patch.json()[0]["name"] == "Desk Mini"


@pytest.mark.asyncio
async def test_patch_client_rejects_duplicate_name(db_client):
    async with db_client.session_factory() as session:
        first, _first_token = await create_client(session, name="First", runtime=ClientRuntime.remote)
        second, _second_token = await create_client(session, name="Second", runtime=ClientRuntime.remote)
        await session.commit()

    response = await db_client.patch(f"/api/clients/{second.id}", json={"name": "First"})

    assert response.status_code == 409
    assert response.json()["detail"] == "client name already exists"

    get_first = await db_client.get(f"/api/clients/{first.id}")
    get_second = await db_client.get(f"/api/clients/{second.id}")
    assert get_first.json()["name"] == "First"
    assert get_second.json()["name"] == "Second"


@pytest.mark.asyncio
async def test_missing_client_returns_404(db_client):
    missing_id = "00000000-0000-0000-0000-000000000000"

    get_response = await db_client.get(f"/api/clients/{missing_id}")
    patch_response = await db_client.patch(f"/api/clients/{missing_id}", json={"name": "missing"})
    delete_response = await db_client.delete(f"/api/clients/{missing_id}")

    assert get_response.status_code == 404
    assert patch_response.status_code == 404
    assert delete_response.status_code == 404


@pytest.mark.asyncio
async def test_delete_remote_client_removes_client_graph_and_invalidates_cache(db_client):
    async with db_client.session_factory() as session:
        remote_client, _token = await create_client(session, name="Remote", runtime=ClientRuntime.remote)
        folder = await get_or_create_folder_by_path(session, remote_client.id, "/project")
        window = await create_window(
            session,
            remote_client.id,
            cwd="/project",
            shell_command="/bin/bash",
        )
        window.folder_id = folder.id
        summary_job = SummaryJob(virtual_window_id=window.id)
        session.add(summary_job)
        terminal_recent = TerminalRecentUsage(
            client_id=remote_client.id,
            window_id=window.id,
            title=window.title,
        )
        session.add(terminal_recent)
        notification_state = TerminalNotificationState(
            client_id=remote_client.id,
            window_id=window.id,
        )
        session.add(notification_state)
        ai_session = AiSession(
            client_id=remote_client.id,
            provider="codex",
            source_id="session-1",
            virtual_window_id=window.id,
        )
        session.add(ai_session)
        await session.flush()
        event = Event(
            client_id=remote_client.id,
            source_type=EventSourceType.codex_trace,
            source_id="trace-1",
            kind="agent_message",
            virtual_window_id=window.id,
            ai_session_id=ai_session.id,
            payload_json={},
            fingerprint="trace-1",
        )
        project_summary = ProjectSummary(client_id=remote_client.id, project_path="/project")
        session.add_all([event, project_summary])
        await session.commit()
        remote_client_id = remote_client.id
        window_id = window.id
        folder_id = folder.id
        ai_session_id = ai_session.id

    cached_clients = await db_client.get("/api/clients")
    assert cached_clients.status_code == 200
    assert any(client["id"] == str(remote_client_id) for client in cached_clients.json())

    response = await db_client.delete(f"/api/clients/{remote_client_id}")

    assert response.status_code == 204
    assert (await db_client.get(f"/api/clients/{remote_client_id}")).status_code == 404
    list_after_delete = await db_client.get("/api/clients")
    assert all(client["id"] != str(remote_client_id) for client in list_after_delete.json())

    async with db_client.session_factory() as session:
        assert await session.get(Client, remote_client_id) is None
        assert await session.get(Folder, folder_id) is None
        assert await session.get(VirtualWindow, window_id) is None
        assert await session.get(AiSession, ai_session_id) is None
        assert (await session.scalars(select(Event).where(Event.client_id == remote_client_id))).first() is None
        assert (
            await session.scalars(
                select(TerminalRecentUsage).where(TerminalRecentUsage.client_id == remote_client_id)
            )
        ).first() is None
        assert (
            await session.scalars(
                select(TerminalNotificationState).where(
                    TerminalNotificationState.client_id == remote_client_id
                )
            )
        ).first() is None
        assert (
            await session.scalars(
                select(ProjectSummary).where(ProjectSummary.client_id == remote_client_id)
            )
        ).first() is None


@pytest.mark.asyncio
async def test_delete_remote_client_closes_active_connection(db_client):
    async with db_client.session_factory() as session:
        remote_client, _token = await create_client(session, name="Remote", runtime=ClientRuntime.remote)
        await session.commit()

    connection = FakeClientConnection()
    registry = FakeClientConnectionRegistry(connection)
    app.state.client_connections = registry

    response = await db_client.delete(f"/api/clients/{remote_client.id}")

    assert response.status_code == 204
    assert connection.closed is True
    assert registry.unregistered == [(remote_client.id, connection)]


@pytest.mark.asyncio
async def test_delete_local_client_is_rejected(db_client):
    list_response = await db_client.get("/api/clients")
    local_client_id = list_response.json()[0]["id"]

    response = await db_client.delete(f"/api/clients/{local_client_id}")

    assert response.status_code == 400
    assert response.json()["detail"] == "local client deletion unsupported"


@pytest.mark.asyncio
async def test_bootstrap_client_route_returns_runner_result(db_client):
    client_id = uuid4()

    async def fake_runner(_session, payload):
        assert payload.private_key == BOOTSTRAP_PAYLOAD["private_key"]
        return clients_router.BootstrapResult(
            client_id=client_id,
            name=payload.name,
            status="OFFLINE",
            reused=False,
        )

    app.dependency_overrides[clients_router.get_bootstrap_runner] = lambda: fake_runner

    response = await db_client.post("/api/clients/bootstrap", json=BOOTSTRAP_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {
        "client_id": str(client_id),
        "name": "Remote Dev",
        "status": "OFFLINE",
        "reused": False,
    }


@pytest.mark.asyncio
async def test_update_client_route_returns_started_result(db_client):
    async with db_client.session_factory() as session:
        remote_client, _token = await create_client(session, name="Remote", runtime=ClientRuntime.remote)
        await session.commit()

    async def fake_runner(client_id, _registry):
        return clients_router.ClientUpdateStartResult(
            client_id=client_id,
            job_id="job-1",
            method="agent_message",
        )

    app.dependency_overrides[clients_router.get_update_runner] = lambda: fake_runner

    response = await db_client.post(f"/api/clients/{remote_client.id}/update")

    assert response.status_code == 202
    assert response.json() == {
        "client_id": str(remote_client.id),
        "job_id": "job-1",
        "status": "STARTED",
        "method": "agent_message",
    }


@pytest.mark.asyncio
async def test_update_package_requires_client_token(db_client):
    async with db_client.session_factory() as session:
        remote_client, token = await create_client(session, name="Remote", runtime=ClientRuntime.remote)
        await session.commit()

    rejected = await db_client.get(f"/api/clients/{remote_client.id}/update/package")
    accepted = await db_client.get(
        f"/api/clients/{remote_client.id}/update/package?job_id=job-1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["job_id"] == "job-1"
    assert "client_agent/updater.py" in accepted.json()["files"]


@pytest.mark.asyncio
async def test_update_complete_records_completion_time_after_client_callback(db_client):
    async with db_client.session_factory() as session:
        remote_client, token = await create_client(session, name="Remote", runtime=ClientRuntime.remote)
        await session.commit()

    rejected = await db_client.post(
        f"/api/clients/{remote_client.id}/update/complete",
        json={"job_id": "job-1"},
    )
    accepted = await db_client.post(
        f"/api/clients/{remote_client.id}/update/complete",
        json={"job_id": "job-1"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["job_id"] == "job-1"

    response = await db_client.get(f"/api/clients/{remote_client.id}")
    assert response.json()["last_update_at"] is not None


@pytest.mark.asyncio
async def test_bootstrap_client_route_maps_dependency_error_to_400_and_redacts(db_client):
    async def fake_runner(_session, _payload):
        raise BootstrapDependencyError(
            "missing tmux "
            + BOOTSTRAP_PAYLOAD["private_key"]
            + " ssh-passphrase plain-client-token"
        )

    app.dependency_overrides[clients_router.get_bootstrap_runner] = lambda: fake_runner

    response = await db_client.post("/api/clients/bootstrap", json=BOOTSTRAP_PAYLOAD)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "missing tmux" in detail
    assert BOOTSTRAP_PAYLOAD["private_key"] not in detail
    assert BOOTSTRAP_PAYLOAD["passphrase"] not in detail
    assert "plain-client-token" not in detail


@pytest.mark.asyncio
async def test_bootstrap_client_route_dependency_error_traceback_drops_secret_cause():
    async def fake_runner(_session, _payload):
        try:
            raise ValueError(
                "dependency cause "
                + BOOTSTRAP_PAYLOAD["private_key"]
                + " ssh-passphrase plain-client-token"
            )
        except ValueError as exc:
            raise BootstrapDependencyError("missing tmux plain-client-token") from exc

    session = CommitRecorder()
    payload = BootstrapClientIn(**BOOTSTRAP_PAYLOAD)

    with pytest.raises(HTTPException) as exc_info:
        await clients_router.bootstrap_remote_client(payload, session=session, runner=fake_runner)

    formatted = _formatted_exception(exc_info.value)
    assert exc_info.value.status_code == 400
    assert session.committed is False
    assert BOOTSTRAP_PAYLOAD["private_key"] not in formatted
    assert BOOTSTRAP_PAYLOAD["passphrase"] not in formatted
    assert "plain-client-token" not in formatted


@pytest.mark.asyncio
async def test_bootstrap_client_route_maps_connection_error_to_502_and_redacts(db_client):
    async def fake_runner(_session, _payload):
        raise BootstrapConnectionError(
            "auth failed " + BOOTSTRAP_PAYLOAD["private_key"] + " ssh-passphrase"
        )

    app.dependency_overrides[clients_router.get_bootstrap_runner] = lambda: fake_runner

    response = await db_client.post("/api/clients/bootstrap", json=BOOTSTRAP_PAYLOAD)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "auth failed" in detail
    assert BOOTSTRAP_PAYLOAD["private_key"] not in detail
    assert BOOTSTRAP_PAYLOAD["passphrase"] not in detail


@pytest.mark.asyncio
async def test_bootstrap_client_route_connection_error_traceback_drops_secret_cause():
    async def fake_runner(_session, _payload):
        try:
            raise RuntimeError(
                "connection cause "
                + BOOTSTRAP_PAYLOAD["private_key"]
                + " ssh-passphrase plain-client-token"
            )
        except RuntimeError as exc:
            raise BootstrapConnectionError("auth failed plain-client-token") from exc

    session = CommitRecorder()
    payload = BootstrapClientIn(**BOOTSTRAP_PAYLOAD)

    with pytest.raises(HTTPException) as exc_info:
        await clients_router.bootstrap_remote_client(payload, session=session, runner=fake_runner)

    formatted = _formatted_exception(exc_info.value)
    assert exc_info.value.status_code == 502
    assert session.committed is False
    assert BOOTSTRAP_PAYLOAD["private_key"] not in formatted
    assert BOOTSTRAP_PAYLOAD["passphrase"] not in formatted
    assert "plain-client-token" not in formatted


@pytest.mark.asyncio
async def test_bootstrap_client_route_rejects_invalid_port(db_client):
    response = await db_client.post(
        "/api/clients/bootstrap", json={**BOOTSTRAP_PAYLOAD, "port": 70000}
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_registration_key_is_single_use_for_direct_client_registration(db_client):
    async with db_client.session_factory() as session:
        registration_key, key = await create_registration_key(session, label="dev box")
        key_id = registration_key.id
        await session.commit()

    payload = {
        "registration_key": key,
        "name": "Direct Dev",
        "hostname": "direct-host",
        "install_path": "/home/alice/.web-terminal-acp",
        "server_url": "https://control.example.com",
    }
    first = await db_client.post("/api/clients/register", json=payload)
    second = await db_client.post("/api/clients/register", json=payload)

    assert first.status_code == 200
    assert second.status_code == 401
    body = first.json()
    assert body["name"] == "Direct Dev"
    assert body["token"]
    assert body["config"]["client_id"] == body["client_id"]
    assert body["config"]["token"] == body["token"]
    assert "client_agent/runner.py" in body["package"]["files"]

    async with db_client.session_factory() as session:
        used_key = await session.get(ClientRegistrationKey, key_id)
        assert used_key is not None
        assert used_key.status is ClientRegistrationKeyStatus.used
        assert str(used_key.used_client_id) == body["client_id"]


@pytest.mark.asyncio
async def test_direct_client_registration_reuses_existing_client_with_same_name(db_client):
    async with db_client.session_factory() as session:
        first_registration_key, first_key = await create_registration_key(session, label="desk")
        second_registration_key, second_key = await create_registration_key(session, label="desk")
        await session.commit()

    first_payload = {
        "registration_key": first_key,
        "name": "Office Mac Mini",
        "hostname": "old-host",
        "install_path": "/home/alice/.web-terminal-acp",
        "server_url": "https://control.example.com",
    }
    second_payload = {
        **first_payload,
        "registration_key": second_key,
        "hostname": "new-host",
        "install_path": "/srv/web-terminal-acp",
    }

    first = await db_client.post("/api/clients/register", json=first_payload)
    second = await db_client.post("/api/clients/register", json=second_payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert second_body["client_id"] == first_body["client_id"]
    assert second_body["token"] != first_body["token"]
    assert second_body["config"]["install_path"] == "/srv/web-terminal-acp"

    async with db_client.session_factory() as session:
        clients = list(await session.scalars(select(Client)))
        remote_clients = [client for client in clients if client.runtime is ClientRuntime.remote]
        first_used_key = await session.get(ClientRegistrationKey, first_registration_key.id)
        second_used_key = await session.get(ClientRegistrationKey, second_registration_key.id)
        assert len(remote_clients) == 1
        assert remote_clients[0].hostname == "new-host"
        assert str(first_used_key.used_client_id) == first_body["client_id"]
        assert str(second_used_key.used_client_id) == first_body["client_id"]


@pytest.mark.asyncio
async def test_create_registration_key_returns_plain_key_once(db_client):
    response = await db_client.post("/api/clients/registration-keys", json={"label": "desk"})

    assert response.status_code == 200
    body = response.json()
    assert body["key"].startswith("wtr_")
    assert body["label"] == "desk"
    assert body["created_at"] is not None


@pytest.mark.asyncio
async def test_read_registration_script_returns_direct_client_installer(db_client):
    response = await db_client.get("/api/clients/register-script")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-shellscript")
    assert "WEB_TERMINAL_REGISTRATION_KEY" in response.text
    assert "/api/clients/register" in response.text
    assert "raw.githubusercontent.com" not in response.text
    assert response.text == (REPO_ROOT / "scripts/register-client-direct.sh").read_text(encoding="utf-8")
