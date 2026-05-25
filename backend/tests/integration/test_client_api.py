import traceback
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, get_session
from app.main import app
from app.models import ClientRuntime
from app.routers import clients as clients_router
from app.repositories.clients import create_client, ensure_local_client
from app.schemas import BootstrapClientIn
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


@pytest.fixture
async def db_client(tmp_path):
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
async def test_missing_client_returns_404(db_client):
    missing_id = "00000000-0000-0000-0000-000000000000"

    get_response = await db_client.get(f"/api/clients/{missing_id}")
    patch_response = await db_client.patch(f"/api/clients/{missing_id}", json={"name": "missing"})

    assert get_response.status_code == 404
    assert patch_response.status_code == 404


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
