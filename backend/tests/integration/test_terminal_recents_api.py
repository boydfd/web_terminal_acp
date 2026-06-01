import asyncio
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, get_session
from app.main import app
from app.models import LOCAL_CLIENT_ID, TerminalRecentUsage
from app.repositories.clients import create_client, ensure_local_client
from app.repositories.windows import create_window


class DbClient:
    def __init__(self, client: AsyncClient, session_factory: async_sessionmaker):
        self._client = client
        self.session_factory = session_factory

    async def get(self, *args, **kwargs):
        return await self._client.get(*args, **kwargs)

    async def post(self, *args, **kwargs):
        return await self._client.post(*args, **kwargs)


@pytest.fixture
async def db_client(tmp_path):
    database_path = tmp_path / "terminal-recents.db"
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
        await engine.dispose()


@pytest.mark.asyncio
async def test_record_and_list_terminal_recents(db_client):
    client_id = str(LOCAL_CLIENT_ID)
    async with db_client.session_factory() as session:
        first = await create_window(session, LOCAL_CLIENT_ID, None, None)
        first.title = "Alpha"
        second = await create_window(session, LOCAL_CLIENT_ID, None, None)
        second.title = "Beta"
        await session.commit()

    record_first = await db_client.post(
        f"/api/clients/{client_id}/terminal-recents",
        json={"window_id": str(first.id), "title": "Alpha"},
    )
    assert record_first.status_code == 200

    record_second = await db_client.post(
        f"/api/clients/{client_id}/terminal-recents",
        json={"window_id": str(second.id), "title": "Beta"},
    )
    assert record_second.status_code == 200

    record_first_again = await db_client.post(
        f"/api/clients/{client_id}/terminal-recents",
        json={"window_id": str(first.id), "title": "Alpha updated"},
    )
    assert record_first_again.status_code == 200

    listing = await db_client.get(f"/api/clients/{client_id}/terminal-recents?page=1&page_size=20")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["total_pages"] == 1
    assert [item["window_id"] for item in body["items"]] == [str(first.id), str(second.id)]
    assert body["items"][0]["title"] == "Alpha"


@pytest.mark.asyncio
async def test_record_terminal_recent_is_idempotent_under_concurrent_posts(db_client):
    client_id = str(LOCAL_CLIENT_ID)
    async with db_client.session_factory() as session:
        window = await create_window(session, LOCAL_CLIENT_ID, None, None)
        window.title = "Concurrent"
        await session.commit()

    responses = await asyncio.gather(
        *[
            db_client.post(
                f"/api/clients/{client_id}/terminal-recents",
                json={"window_id": str(window.id), "title": f"Concurrent {index}"},
            )
            for index in range(12)
        ]
    )

    assert {response.status_code for response in responses} == {200}
    async with db_client.session_factory() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(TerminalRecentUsage)
            .where(
                TerminalRecentUsage.client_id == LOCAL_CLIENT_ID,
                TerminalRecentUsage.window_id == window.id,
            )
        )
        assert total == 1


@pytest.mark.asyncio
async def test_terminal_recents_trim_to_max_entries(db_client, monkeypatch):
    monkeypatch.setattr("app.repositories.terminal_recents.MAX_TERMINAL_RECENTS", 10)
    client_id = str(LOCAL_CLIENT_ID)
    window_ids: list[UUID] = []
    async with db_client.session_factory() as session:
        for index in range(15):
            window = await create_window(session, LOCAL_CLIENT_ID, None, None)
            window.title = f"Window {index}"
            window_ids.append(window.id)
        await session.commit()

    for index, window_id in enumerate(window_ids):
        response = await db_client.post(
            f"/api/clients/{client_id}/terminal-recents",
            json={"window_id": str(window_id), "title": f"Window {index}"},
        )
        assert response.status_code == 200

    async with db_client.session_factory() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(TerminalRecentUsage)
            .where(TerminalRecentUsage.client_id == LOCAL_CLIENT_ID)
        )
        assert total == 10

    listing = await db_client.get(f"/api/clients/{client_id}/terminal-recents?page=1&page_size=20")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 10
    assert body["total_pages"] == 1
    assert len(body["items"]) == 10
    assert body["items"][0]["window_id"] == str(window_ids[-1])


@pytest.mark.asyncio
async def test_search_terminal_recents_by_query(db_client):
    client_id = str(LOCAL_CLIENT_ID)
    async with db_client.session_factory() as session:
        alpha = await create_window(session, LOCAL_CLIENT_ID, None, None)
        alpha.title = "Alpha workspace"
        beta = await create_window(session, LOCAL_CLIENT_ID, None, None)
        beta.title = "Beta workspace"
        await session.commit()

    await db_client.post(
        f"/api/clients/{client_id}/terminal-recents",
        json={"window_id": str(alpha.id), "title": "stale alpha"},
    )
    await db_client.post(
        f"/api/clients/{client_id}/terminal-recents",
        json={"window_id": str(beta.id), "title": "Beta workspace"},
    )

    search = await db_client.get(f"/api/clients/{client_id}/terminal-recents?q=alpha&page=1&page_size=20")
    assert search.status_code == 200
    body = search.json()
    assert body["total"] == 1
    assert body["items"][0]["window_id"] == str(alpha.id)
    assert body["items"][0]["title"] == "Alpha workspace"


@pytest.mark.asyncio
async def test_list_global_terminal_recents_across_clients(db_client):
    local_client_id = str(LOCAL_CLIENT_ID)
    async with db_client.session_factory() as session:
        remote_client, _token = await create_client(session, name="remote-alpha")
        local_window = await create_window(session, LOCAL_CLIENT_ID, None, None)
        local_window.title = "Local terminal"
        remote_window = await create_window(session, remote_client.id, None, None)
        remote_window.title = "Remote terminal"
        remote_client_id = str(remote_client.id)
        await session.commit()

    local_recent = await db_client.post(
        f"/api/clients/{local_client_id}/terminal-recents",
        json={"window_id": str(local_window.id), "title": "Local terminal"},
    )
    assert local_recent.status_code == 200
    remote_recent = await db_client.post(
        f"/api/clients/{remote_client_id}/terminal-recents",
        json={"window_id": str(remote_window.id), "title": "Remote terminal"},
    )
    assert remote_recent.status_code == 200

    listing = await db_client.get("/api/terminal-recents?page=1&page_size=20")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 2
    assert [item["client_id"] for item in body["items"]] == [remote_client_id, local_client_id]
    assert [item["window_id"] for item in body["items"]] == [str(remote_window.id), str(local_window.id)]
    assert body["items"][0]["client_name"] == "remote-alpha"

    search = await db_client.get("/api/terminal-recents?q=remote-alpha&page=1&page_size=20")
    assert search.status_code == 200
    search_body = search.json()
    assert search_body["total"] == 1
    assert search_body["items"][0]["client_id"] == remote_client_id


@pytest.mark.asyncio
async def test_terminal_recents_rejects_oversized_page_size(db_client):
    client_id = str(LOCAL_CLIENT_ID)
    response = await db_client.get(f"/api/clients/{client_id}/terminal-recents?page=1&page_size=1000")
    assert response.status_code == 422
