import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, get_session
from app.main import app


class DbClient:
    def __init__(self, client: AsyncClient, session_factory: async_sessionmaker):
        self._client = client
        self.session_factory = session_factory

    async def get(self, *args, **kwargs):
        return await self._client.get(*args, **kwargs)

    async def put(self, *args, **kwargs):
        return await self._client.put(*args, **kwargs)


@pytest.fixture
async def db_client(tmp_path):
    database_path = tmp_path / "ui-settings.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

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
async def test_custom_quick_keys_round_trip(db_client):
    initial = await db_client.get("/api/ui-settings/custom-quick-keys")
    assert initial.status_code == 200
    assert initial.json() == {"quick_keys": []}

    payload = {
        "quick_keys": [
            {"id": "status", "label": "Git status", "input": "git status{Enter}"},
            {
                "id": "interrupt",
                "label": "Interrupt",
                "input": "{Ctrl-C}",
                "shortcut": {"key": "c", "alt": True},
            },
        ]
    }
    saved = await db_client.put("/api/ui-settings/custom-quick-keys", json=payload)
    assert saved.status_code == 200
    assert saved.json() == payload

    loaded = await db_client.get("/api/ui-settings/custom-quick-keys")
    assert loaded.status_code == 200
    assert loaded.json() == payload


@pytest.mark.asyncio
async def test_custom_quick_keys_reject_invalid_items(db_client):
    response = await db_client.put(
        "/api/ui-settings/custom-quick-keys",
        json={"quick_keys": [{"id": "bad", "label": "", "input": "pwd"}]},
    )

    assert response.status_code == 422
