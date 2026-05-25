import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base, get_session
from app.main import app
from app.repositories.clients import create_client, ensure_local_client
from app.routers import search as search_router
from app.services.search_index import AI_EVENTS_INDEX, SUMMARIES_INDEX, TERMINAL_INDEX


class FakeElasticsearch:
    def __init__(self):
        self.calls = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return {"hits": {"hits": []}}


@pytest.fixture
async def db_client(tmp_path):
    database_path = tmp_path / "client_scoped_search.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        local_client = await ensure_local_client(session)
        remote_client, _token = await create_client(session, name="Remote Desk")
        await session.commit()
        local_client_id = str(local_client.id)
        remote_client_id = str(remote_client.id)

    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, local_client_id, remote_client_id
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_client_scoped_search_filters_elasticsearch_query_by_client_id(db_client, monkeypatch):
    client, _local_client_id, client_id = db_client
    es_client = FakeElasticsearch()
    monkeypatch.setattr(app.state, "es_indexes_ready", True, raising=False)
    app.dependency_overrides[search_router.get_search_client] = lambda: es_client

    try:
        response = await client.get(f"/api/clients/{client_id}/search", params={"q": "nginx 403"})
    finally:
        app.dependency_overrides.pop(search_router.get_search_client, None)

    assert response.status_code == 200
    assert response.json() == {"query": "nginx 403", "results": []}
    assert es_client.calls == [
        {
            "index": [TERMINAL_INDEX, AI_EVENTS_INDEX, SUMMARIES_INDEX],
            "query": {
                "bool": {
                    "must": [{"multi_match": {"query": "nginx 403", "fields": ["text"]}}],
                    "filter": [{"term": {"client_id": client_id}}],
                }
            },
            "size": 25,
            "source_excludes": ["raw", "source_event_ids", "session_id"],
            "ignore_unavailable": True,
            "allow_no_indices": True,
        }
    ]


@pytest.mark.asyncio
async def test_local_client_scoped_search_includes_legacy_documents_missing_client_id(db_client, monkeypatch):
    client, local_client_id, _remote_client_id = db_client
    es_client = FakeElasticsearch()
    monkeypatch.setattr(app.state, "es_indexes_ready", True, raising=False)
    app.dependency_overrides[search_router.get_search_client] = lambda: es_client

    try:
        response = await client.get(f"/api/clients/{local_client_id}/search", params={"q": "nginx 403"})
    finally:
        app.dependency_overrides.pop(search_router.get_search_client, None)

    assert response.status_code == 200
    assert es_client.calls[0]["query"] == {
        "bool": {
            "must": [{"multi_match": {"query": "nginx 403", "fields": ["text"]}}],
            "filter": [
                {
                    "bool": {
                        "should": [
                            {"term": {"client_id": local_client_id}},
                            {"bool": {"must_not": [{"exists": {"field": "client_id"}}]}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_client_scoped_search_returns_404_for_missing_client(db_client, monkeypatch):
    client, _local_client_id, _remote_client_id = db_client
    missing_client_id = "00000000-0000-0000-0000-000000000000"

    def fail_get_search_client():
        raise AssertionError("missing client should not open Elasticsearch client")

    app.dependency_overrides[search_router.get_search_client] = fail_get_search_client
    try:
        response = await client.get(f"/api/clients/{missing_client_id}/search", params={"q": "nginx"})
    finally:
        app.dependency_overrides.pop(search_router.get_search_client, None)

    assert response.status_code == 404
    assert response.json() == {"detail": "client not found"}
