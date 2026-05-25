from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_session
from app.main import app
from app.model_base import Base
from app.models import AiSession, ClientRuntime, Event, SummaryJob, VirtualWindow
from app.repositories.clients import create_client, ensure_local_client
from app.repositories.windows import create_window
from app.services.ingest.codex_receiver import receive_codex_trace
from app.services.search_index import AI_EVENTS_INDEX


@pytest.fixture
async def db_client(tmp_path):
    database_path = tmp_path / "trace_ingest.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with Session() as session:
        await ensure_local_client(session)
        await session.commit()

    async def override_get_session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, Session
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


class FakeElasticsearch:
    def __init__(self):
        self.indexed_documents = []

    async def index(self, **kwargs):
        self.indexed_documents.append(kwargs)
        return {"result": "created"}


async def create_local_window(session) -> VirtualWindow:
    client = await ensure_local_client(session)
    return await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")


async def payload_for_local_window(Session, trace_id: str) -> tuple[dict, UUID, UUID]:
    async with Session() as session:
        window = await create_local_window(session)
        await session.commit()
        return (
            {
                "trace_id": trace_id,
                "span": {"name": "tool_call", "attributes": {"tool": "bash"}},
                "virtualWindowId": str(window.id),
            },
            window.client_id,
            window.id,
        )


@pytest.mark.asyncio
async def test_codex_trace_ingest_creates_client_scoped_event(db_client):
    client, Session = db_client
    payload, client_id, window_id = await payload_for_local_window(Session, "trace-1")

    response = await client.post("/api/traces/codex", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "tool_call"
    assert body["source_type"] == "codex_trace"
    assert body["source_id"] == "trace-1"
    assert "payload_json" not in body
    async with Session() as session:
        rows = (await session.execute(select(Event))).scalars().all()
    assert len(rows) == 1
    assert rows[0].client_id == client_id
    assert rows[0].virtual_window_id == window_id
    assert rows[0].source_id == "trace-1"

    duplicate_response = await client.post("/api/traces/codex", json=payload)
    assert duplicate_response.status_code == 200
    assert duplicate_response.json()["id"] == body["id"]
    async with Session() as session:
        rows = (await session.execute(select(Event))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_receive_codex_trace_links_ai_session_and_enqueues_summary_job(db_client):
    _client, Session = db_client
    async with Session() as session:
        window = await create_local_window(session)
        await session.commit()
        client_id = window.client_id
        window_id = window.id

    payload = {
        "trace_id": "trace-2",
        "span": {"name": "tool_call", "attributes": {"tool": "bash"}},
        "virtualWindowId": str(window_id),
    }
    es_client = FakeElasticsearch()

    async with Session() as session:
        first_row = await receive_codex_trace(session, payload, client_id=client_id, es_client=es_client)
        second_row = await receive_codex_trace(session, payload, client_id=client_id, es_client=es_client)
        await session.commit()

    async with Session() as session:
        rows = (await session.execute(select(Event))).scalars().all()
        ai_sessions = (await session.execute(select(AiSession))).scalars().all()
        summary_jobs = (await session.execute(select(SummaryJob))).scalars().all()

    assert first_row.id == second_row.id
    assert len(rows) == 1
    assert rows[0].indexed_at is None
    assert rows[0].client_id == client_id
    assert rows[0].virtual_window_id == window_id
    assert len(ai_sessions) == 1
    assert ai_sessions[0].client_id == client_id
    assert ai_sessions[0].provider == "codex"
    assert ai_sessions[0].source_id == "trace-2"
    assert ai_sessions[0].virtual_window_id == window_id
    assert rows[0].ai_session_id == ai_sessions[0].id
    assert len(summary_jobs) == 1
    assert summary_jobs[0].virtual_window_id == window_id
    assert es_client.indexed_documents == []


@pytest.mark.asyncio
async def test_receive_codex_trace_rejects_missing_virtual_window_id(db_client):
    _client, Session = db_client
    async with Session() as session:
        local_client = await ensure_local_client(session)
        payload = {"trace_id": "trace-missing-window", "span": {"name": "tool_call"}}

        with pytest.raises(ValueError, match="virtual_window_id is required"):
            await receive_codex_trace(session, payload, client_id=local_client.id)


@pytest.mark.asyncio
async def test_receive_codex_trace_rejects_virtual_window_from_different_client(db_client):
    _client, Session = db_client
    async with Session() as session:
        local_client = await ensure_local_client(session)
        remote_client, _token = await create_client(
            session, name="remote", runtime=ClientRuntime.remote
        )
        remote_window = await create_window(session, remote_client.id, cwd="/tmp", shell_command="/bin/bash")
        await session.commit()

    payload = {
        "trace_id": "trace-wrong-client",
        "span": {"name": "tool_call", "attributes": {"tool": "bash"}},
        "virtualWindowId": str(remote_window.id),
    }

    async with Session() as session:
        with pytest.raises(ValueError, match="virtual_window_id does not belong to client"):
            await receive_codex_trace(session, payload, client_id=local_client.id)


@pytest.mark.asyncio
async def test_codex_trace_route_indexes_with_ready_app_es_client(db_client, monkeypatch):
    client, Session = db_client
    payload, client_id, window_id = await payload_for_local_window(Session, "trace-3")
    es_client = FakeElasticsearch()
    monkeypatch.setattr(app.state, "es_client", es_client, raising=False)
    monkeypatch.setattr(app.state, "es_indexes_ready", True, raising=False)

    response = await client.post("/api/traces/codex", json=payload)

    assert response.status_code == 200
    body = response.json()
    async with Session() as session:
        row = await session.get(Event, UUID(body["id"]))
    assert row is not None
    assert row.indexed_at is not None
    assert es_client.indexed_documents == [
        {
            "index": AI_EVENTS_INDEX,
            "id": body["id"],
            "document": {
                "client_id": str(client_id),
                "provider": "codex",
                "session_id": "trace-3",
                "kind": "tool_call",
                "virtual_window_id": str(window_id),
                "text": '{"tool": "bash"}',
                "raw": payload,
            },
        }
    ]


@pytest.mark.asyncio
async def test_codex_trace_duplicate_uses_stable_es_id_without_reindex(db_client, monkeypatch):
    client, Session = db_client
    payload, _client_id, _window_id = await payload_for_local_window(Session, "trace-4")
    es_client = FakeElasticsearch()
    monkeypatch.setattr(app.state, "es_client", es_client, raising=False)
    monkeypatch.setattr(app.state, "es_indexes_ready", True, raising=False)

    first_response = await client.post("/api/traces/codex", json=payload)
    second_response = await client.post("/api/traces/codex", json=payload)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    event_id = first_response.json()["id"]
    assert second_response.json()["id"] == event_id
    assert [call["id"] for call in es_client.indexed_documents] == [event_id]


@pytest.mark.asyncio
async def test_codex_trace_ingest_keeps_pg_event_when_es_unavailable(db_client, monkeypatch):
    client, Session = db_client
    payload, _client_id, _window_id = await payload_for_local_window(Session, "trace-5")
    monkeypatch.setattr(app.state, "es_client", FakeElasticsearch(), raising=False)
    monkeypatch.setattr(app.state, "es_indexes_ready", False, raising=False)

    response = await client.post("/api/traces/codex", json=payload)

    assert response.status_code == 200
    async with Session() as session:
        row = await session.get(Event, UUID(response.json()["id"]))
    assert row is not None
    assert row.indexed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [["not", "object"], "not-object", 123, None])
async def test_codex_trace_ingest_rejects_non_object_json(db_client, payload):
    client, _Session = db_client

    response = await client.post("/api/traces/codex", json=payload)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_codex_trace_ingest_rejects_oversized_json(db_client):
    client, _Session = db_client
    payload = {"trace_id": "trace-large", "span": {"attributes": {"text": "x" * (256 * 1024)}}}

    response = await client.post("/api/traces/codex", json=payload)

    assert response.status_code == 413
