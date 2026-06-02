import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import app
from app.models import ClientRuntime, VirtualWindow, WindowStatus
from app.routers import folders as folders_router
from app.repositories.clients import create_client, ensure_local_client
from app.repositories.folders import get_or_create_folder_by_path
from app.services import polling_response_cache
from app.services.polling_response_cache import clear_polling_response_cache

try:
    from app.db import Base
except ImportError:  # pragma: no cover - compatibility with alternate app layout
    from app.model_base import Base


class FolderApiClient:
    def __init__(self, client: AsyncClient, session_factory: async_sessionmaker):
        self._client = client
        self.session_factory = session_factory

    async def get(self, *args, **kwargs):
        return await self._client.get(*args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self._client.post(*args, **kwargs)


@pytest.fixture
async def sqlite_client(tmp_path):
    clear_polling_response_cache()
    database_path = tmp_path / "folders.db"
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
            yield FolderApiClient(test_client, session_factory)
    finally:
        app.dependency_overrides.pop(get_session, None)
        clear_polling_response_cache()
        await engine.dispose()


async def get_local_client_id(sqlite_client: FolderApiClient) -> str:
    response = await sqlite_client.get("/api/clients")
    assert response.status_code == 200
    local_clients = [client for client in response.json() if client["runtime"] == "local"]
    assert len(local_clients) == 1
    return local_clients[0]["id"]


async def create_remote_client_id(sqlite_client: FolderApiClient) -> str:
    async with sqlite_client.session_factory() as session:
        client, _token = await create_client(
            session,
            name="remote-a",
            runtime=ClientRuntime.remote,
        )
        client_id = str(client.id)
        await session.commit()
    return client_id


@pytest.mark.asyncio
async def test_folder_api_creates_path_and_returns_nested_tree(sqlite_client):
    client_id = await get_local_client_id(sqlite_client)
    create_response = await sqlite_client.post(
        f"/api/clients/{client_id}/folders", json={"path": "/2026-05/生产排障"}
    )

    assert create_response.status_code == 200
    assert create_response.json()["name"] == "生产排障"
    assert create_response.json()["path"] == "/2026-05/生产排障"

    tree_response = await sqlite_client.get(f"/api/clients/{client_id}/tree")

    assert tree_response.status_code == 200
    tree = tree_response.json()
    assert len(tree) == 1

    root = tree[0]
    assert root["name"] == "2026-05"
    assert root["path"] == "/2026-05"
    assert root["windows"] == []
    assert len(root["folders"]) == 1

    child = root["folders"][0]
    assert child == {
        "id": create_response.json()["id"],
        "name": "生产排障",
        "path": "/2026-05/生产排障",
        "folders": [],
        "windows": [],
    }


@pytest.mark.asyncio
async def test_tree_hot_cache_skips_client_and_tree_queries(sqlite_client, monkeypatch):
    client_id = await get_local_client_id(sqlite_client)
    create_response = await sqlite_client.post(
        f"/api/clients/{client_id}/folders", json={"path": "/2026-05/生产排障"}
    )
    assert create_response.status_code == 200
    first_tree = await sqlite_client.get(f"/api/clients/{client_id}/tree")
    assert first_tree.status_code == 200

    async def fail_require_client(_session, _client_id):
        raise AssertionError("hot tree cache should avoid client lookup")

    async def fail_build_tree(_session, _client_id, **_kwargs):
        raise AssertionError("hot tree cache should avoid tree query")

    monkeypatch.setattr(folders_router, "_require_client", fail_require_client)
    monkeypatch.setattr(folders_router, "build_tree", fail_build_tree)

    second_tree = await sqlite_client.get(f"/api/clients/{client_id}/tree")

    assert second_tree.status_code == 200
    assert second_tree.json() == first_tree.json()


@pytest.mark.asyncio
async def test_tree_expired_cache_serves_stale_response(sqlite_client, monkeypatch):
    client_id = await get_local_client_id(sqlite_client)
    create_response = await sqlite_client.post(
        f"/api/clients/{client_id}/folders", json={"path": "/2026-05/生产排障"}
    )
    assert create_response.status_code == 200
    first_tree = await sqlite_client.get(f"/api/clients/{client_id}/tree")
    assert first_tree.status_code == 200
    refreshes = []

    async def fail_build_tree(_session, _client_id, **_kwargs):
        raise AssertionError("expired tree cache should return stale before refresh")

    monkeypatch.setattr(polling_response_cache, "_CACHE_TTL_SECONDS", -1.0)
    monkeypatch.setattr(folders_router, "build_tree", fail_build_tree)
    monkeypatch.setattr(
        folders_router,
        "_refresh_response_cache",
        lambda cache_key, refresh: refreshes.append(cache_key),
    )

    second_tree = await sqlite_client.get(f"/api/clients/{client_id}/tree")

    assert second_tree.status_code == 200
    assert second_tree.json() == first_tree.json()
    assert refreshes


@pytest.mark.asyncio
async def test_create_folder_invalidates_tree_hot_cache(sqlite_client):
    client_id = await get_local_client_id(sqlite_client)
    cached_empty_tree = await sqlite_client.get(f"/api/clients/{client_id}/tree")
    assert cached_empty_tree.status_code == 200
    assert cached_empty_tree.json() == []

    create_response = await sqlite_client.post(
        f"/api/clients/{client_id}/folders", json={"path": "/2026-05/生产排障"}
    )
    assert create_response.status_code == 200

    tree_response = await sqlite_client.get(f"/api/clients/{client_id}/tree")

    assert tree_response.status_code == 200
    assert tree_response.json()[0]["path"] == "/2026-05"


@pytest.mark.asyncio
async def test_folder_api_rejects_empty_path(sqlite_client):
    client_id = await get_local_client_id(sqlite_client)
    response = await sqlite_client.post(f"/api/clients/{client_id}/folders", json={"path": "/"})

    assert response.status_code == 400
    assert response.json() == {"detail": "folder path must contain at least one segment"}


@pytest.mark.asyncio
async def test_folder_api_returns_existing_folder_for_duplicate_path(sqlite_client):
    client_id = await get_local_client_id(sqlite_client)
    first_response = await sqlite_client.post(
        f"/api/clients/{client_id}/folders", json={"path": "/2026-05/生产排障"}
    )
    second_response = await sqlite_client.post(
        f"/api/clients/{client_id}/folders", json={"path": "/2026-05/生产排障"}
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json() == first_response.json()


@pytest.mark.asyncio
async def test_folder_api_handles_concurrent_duplicate_path_creates(sqlite_client):
    client_id = await get_local_client_id(sqlite_client)
    responses = await asyncio.gather(
        *[
            sqlite_client.post(f"/api/clients/{client_id}/folders", json={"path": "/2026-05/生产排障"})
            for _ in range(8)
        ]
    )

    assert {response.status_code for response in responses} == {200}
    response_bodies = [response.json() for response in responses]
    assert {body["path"] for body in response_bodies} == {"/2026-05/生产排障"}
    assert len({body["id"] for body in response_bodies}) == 1


@pytest.mark.asyncio
async def test_folder_api_canonicalizes_duplicate_slashes_and_whitespace(sqlite_client):
    client_id = await get_local_client_id(sqlite_client)
    response = await sqlite_client.post(
        f"/api/clients/{client_id}/folders", json={"path": " //2026-05//ACAS项目/ "}
    )

    assert response.status_code == 200
    assert response.json()["path"] == "/2026-05/ACAS项目"


@pytest.mark.asyncio
async def test_folder_api_allows_same_path_under_different_clients(sqlite_client):
    local_client_id = await get_local_client_id(sqlite_client)
    remote_client_id = await create_remote_client_id(sqlite_client)

    local_response = await sqlite_client.post(
        f"/api/clients/{local_client_id}/folders", json={"path": "/2026-05/生产排障"}
    )
    remote_response = await sqlite_client.post(
        f"/api/clients/{remote_client_id}/folders", json={"path": "/2026-05/生产排障"}
    )

    assert local_response.status_code == 200
    assert remote_response.status_code == 200
    assert local_response.json()["path"] == remote_response.json()["path"]
    assert local_response.json()["id"] != remote_response.json()["id"]

    local_tree_response = await sqlite_client.get(f"/api/clients/{local_client_id}/tree")
    remote_tree_response = await sqlite_client.get(f"/api/clients/{remote_client_id}/tree")

    assert local_tree_response.status_code == 200
    assert remote_tree_response.status_code == 200
    assert local_tree_response.json()[0]["folders"][0]["id"] == local_response.json()["id"]
    assert remote_tree_response.json()[0]["folders"][0]["id"] == remote_response.json()["id"]


@pytest.mark.asyncio
async def test_tree_range_filters_windows_by_recent_activity(sqlite_client):
    client_id = await get_local_client_id(sqlite_client)
    client_uuid = UUID(client_id)
    current = datetime.now(UTC)
    async with sqlite_client.session_factory() as session:
        folder = await get_or_create_folder_by_path(session, client_uuid, "/range")
        old_window = VirtualWindow(
            client_id=client_uuid,
            folder_id=folder.id,
            title="old terminal",
            status=WindowStatus.active,
            created_at=current - timedelta(days=20),
            updated_at=current,
        )
        recent_output_window = VirtualWindow(
            client_id=client_uuid,
            folder_id=folder.id,
            title="recent output",
            status=WindowStatus.active,
            created_at=current - timedelta(days=20),
            updated_at=current - timedelta(days=20),
            terminal_last_output_at=current - timedelta(days=2),
        )
        recent_created_window = VirtualWindow(
            client_id=client_uuid,
            folder_id=folder.id,
            title="recent created",
            status=WindowStatus.active,
            created_at=current - timedelta(days=2),
            updated_at=current - timedelta(days=2),
        )
        session.add_all([old_window, recent_output_window, recent_created_window])
        await session.commit()

    week_response = await sqlite_client.get(f"/api/clients/{client_id}/tree?range=7d")
    all_response = await sqlite_client.get(f"/api/clients/{client_id}/tree?range=all")

    assert week_response.status_code == 200
    assert all_response.status_code == 200
    week_titles = [
        window["title"]
        for root in week_response.json()
        for window in root["windows"]
    ]
    all_titles = [
        window["title"]
        for root in all_response.json()
        for window in root["windows"]
    ]
    assert week_titles == ["recent output", "recent created"]
    assert all_titles == ["old terminal", "recent output", "recent created"]


@pytest.mark.asyncio
async def test_folder_api_legacy_routes_use_local_client(sqlite_client):
    create_response = await sqlite_client.post("/api/folders", json={"path": "/legacy/local"})

    assert create_response.status_code == 200

    local_client_id = await get_local_client_id(sqlite_client)
    tree_response = await sqlite_client.get(f"/api/clients/{local_client_id}/tree")

    assert tree_response.status_code == 200
    assert tree_response.json()[0]["folders"][0]["id"] == create_response.json()["id"]


@pytest.mark.asyncio
async def test_folder_api_returns_404_for_missing_client(sqlite_client):
    missing_client_id = "00000000-0000-0000-0000-000000000000"

    tree_response = await sqlite_client.get(f"/api/clients/{missing_client_id}/tree")
    create_response = await sqlite_client.post(
        f"/api/clients/{missing_client_id}/folders", json={"path": "/2026-05/生产排障"}
    )

    assert tree_response.status_code == 404
    assert create_response.status_code == 404
    assert tree_response.json() == {"detail": "client not found"}
    assert create_response.json() == {"detail": "client not found"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "detail"),
    [
        ("relative/path", "folder path must be absolute"),
        ("/2026-05/../生产排障", "folder path must not contain . or .. segments"),
        (f"/{'a' * 256}", "folder path segment exceeds 255 characters"),
        ("/" + "/".join(["a" * 250] * 5), "folder path exceeds 1024 characters"),
    ],
)
async def test_folder_api_rejects_invalid_paths(sqlite_client, path, detail):
    client_id = await get_local_client_id(sqlite_client)
    response = await sqlite_client.post(f"/api/clients/{client_id}/folders", json={"path": path})

    assert response.status_code == 400
    assert response.json() == {"detail": detail}
