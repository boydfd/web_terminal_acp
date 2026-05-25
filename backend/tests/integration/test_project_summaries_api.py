from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, get_session
from app.main import app
from app.models import LOCAL_CLIENT_ID
from app.repositories.clients import ensure_local_client
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
    database_path = tmp_path / "project-summaries.db"
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
async def test_list_project_summaries_empty(db_client) -> None:
    response = await db_client.get(f"/api/clients/{LOCAL_CLIENT_ID}/project-summaries")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_summarize_project_rejects_relative_path(db_client) -> None:
    response = await db_client.post(
        f"/api/clients/{LOCAL_CLIENT_ID}/project-summaries/summarize",
        json={"project_path": "relative/path"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_summarize_project_persists_display_name(db_client, monkeypatch) -> None:
    async with db_client.session_factory() as session:
        await create_window(session, LOCAL_CLIENT_ID, "/tmp/project", "/bin/bash")
        await session.commit()

    class FakeSummarizer:
        async def summarize(self, context, *, output_language=None):
            from app.services.project_summarizer import ProjectSummaryResult

            assert context["project_path"] == "/tmp/project"
            return ProjectSummaryResult(name="演示项目")

    monkeypatch.setattr(
        "app.routers.project_summaries.ProjectSummarizer",
        lambda: FakeSummarizer(),
    )

    response = await db_client.post(
        f"/api/clients/{LOCAL_CLIENT_ID}/project-summaries/summarize",
        json={"project_path": "/tmp/project", "output_language": "中文"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "演示项目"
    assert body["status"] == "SUCCEEDED"

    listed = await db_client.get(f"/api/clients/{LOCAL_CLIENT_ID}/project-summaries")
    assert listed.status_code == 200
    assert listed.json()[0]["display_name"] == "演示项目"
