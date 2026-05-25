from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.client_agent.ai_events import managed_event_from_payload
from app.db import Base
from app.models import AiSession, Client, ClientRuntime, ClientStatus, Event, EventSourceType, SummaryJob, VirtualWindow, WindowStatus
from app.repositories.clients import hash_client_token
from app.services import agent_event_ingest
from app.services.agent_event_ingest import persist_managed_agent_event
from app.services.ingest.codex_receiver import receive_managed_codex_trace


class FakeElasticsearch:
    def __init__(self) -> None:
        self.indexed_documents = []

    async def index(self, **kwargs):
        self.indexed_documents.append(kwargs)
        return {"result": "created"}


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


async def create_client_and_window(db_session, *, cwd="/workspace/window"):
    client = Client(
        id=uuid4(),
        name="remote",
        status=ClientStatus.ONLINE,
        token_hash=hash_client_token("token"),
        runtime=ClientRuntime.remote,
    )
    window = VirtualWindow(
        id=uuid4(),
        client_id=client.id,
        title="Terminal",
        status=WindowStatus.active,
        cwd=cwd,
        shell_command="/bin/bash",
    )
    db_session.add_all([client, window])
    await db_session.flush()
    return client, window


@pytest.mark.asyncio
async def test_persist_managed_cursor_event_links_session_window_project_without_indexing(db_session):
    client, window = await create_client_and_window(db_session)
    payload = {
        "client_id": str(client.id),
        "virtual_window_id": str(window.id),
        "agentId": "cursor-agent-1",
        "blob_id": "blob-1",
        "role": "assistant",
        "text": "managed cursor hello",
        "WEB_TERMINAL_PROJECT_PATH": "/workspace/project",
    }
    event = managed_event_from_payload(
        client.id,
        window.id,
        "cursor_cli",
        payload,
        source_path="/home/user/.cursor/store.db",
        cursor="root-1",
    )
    assert event is not None
    es_client = FakeElasticsearch()

    row = await persist_managed_agent_event(db_session, event, es_client=es_client)

    ai_session = await db_session.get(AiSession, row.ai_session_id)
    summary_jobs = (await db_session.execute(select(SummaryJob))).scalars().all()
    assert row.client_id == client.id
    assert row.virtual_window_id == window.id
    assert row.source_type is EventSourceType.agent_tool_record
    assert row.source_id == "cursor-agent-1"
    assert row.indexed_at is None
    assert ai_session is not None
    assert ai_session.provider == "cursor_cli"
    assert ai_session.source_id == "cursor-agent-1"
    assert ai_session.source_path == "/home/user/.cursor/store.db"
    assert ai_session.project_path == "/workspace/project"
    assert ai_session.virtual_window_id == window.id
    assert summary_jobs[0].virtual_window_id == window.id
    assert es_client.indexed_documents == []


@pytest.mark.asyncio
async def test_index_managed_agent_event_if_ready_indexes_after_commit(db_session):
    client, window = await create_client_and_window(db_session)
    payload = {
        "client_id": str(client.id),
        "virtual_window_id": str(window.id),
        "agentId": "cursor-agent-1",
        "blob_id": "blob-1",
        "role": "assistant",
        "text": "managed cursor hello",
        "WEB_TERMINAL_PROJECT_PATH": "/workspace/project",
    }
    event = managed_event_from_payload(
        client.id,
        window.id,
        "cursor_cli",
        payload,
        source_path="/home/user/.cursor/store.db",
        cursor="root-1",
    )
    assert event is not None
    row = await persist_managed_agent_event(db_session, event)
    await db_session.commit()
    es_client = FakeElasticsearch()

    did_index = await agent_event_ingest.index_managed_agent_event_if_ready(db_session, es_client, row)
    await db_session.commit()

    assert did_index is True
    assert row.indexed_at is not None
    assert es_client.indexed_documents[0]["document"]["provider"] == "cursor_cli"
    assert es_client.indexed_documents[0]["document"]["session_id"] == "cursor-agent-1"
    assert es_client.indexed_documents[0]["document"]["text"] == "managed cursor hello"
    assert es_client.indexed_documents[0]["id"] == str(row.id)


@pytest.mark.asyncio
async def test_index_managed_agent_event_if_ready_skips_missing_client_or_already_indexed(db_session):
    client, window = await create_client_and_window(db_session)
    payload = {
        "client_id": str(client.id),
        "virtual_window_id": str(window.id),
        "agentId": "cursor-agent-1",
        "role": "assistant",
        "text": "managed cursor hello",
    }
    event = managed_event_from_payload(client.id, window.id, "cursor_cli", payload)
    assert event is not None
    row = await persist_managed_agent_event(db_session, event)
    await db_session.commit()
    es_client = FakeElasticsearch()

    assert await agent_event_ingest.index_managed_agent_event_if_ready(db_session, None, row) is False
    assert await agent_event_ingest.index_managed_agent_event_if_ready(db_session, es_client, row) is True
    assert await agent_event_ingest.index_managed_agent_event_if_ready(db_session, es_client, row) is False

    assert len(es_client.indexed_documents) == 1


@pytest.mark.asyncio
async def test_persist_managed_agent_event_rejects_mismatched_payload_window(db_session):
    client, window = await create_client_and_window(db_session)
    event = managed_event_from_payload(
        client.id,
        window.id,
        "cursor_cli",
        {
            "client_id": str(client.id),
            "virtual_window_id": str(window.id),
            "agentId": "cursor-agent-1",
            "role": "assistant",
            "text": "managed cursor hello",
        },
    )
    assert event is not None
    event.payload["virtual_window_id"] = str(uuid4())

    with pytest.raises(ValueError, match="event attribution does not match client/window"):
        await persist_managed_agent_event(db_session, event)

    assert (await db_session.execute(select(Event))).scalars().all() == []


@pytest.mark.asyncio
async def test_persist_managed_agent_event_uses_payload_project_path_when_event_project_missing(db_session):
    client, window = await create_client_and_window(db_session, cwd="/workspace/window-fallback")
    event = managed_event_from_payload(
        client.id,
        window.id,
        "cursor_cli",
        {
            "client_id": str(client.id),
            "virtual_window_id": str(window.id),
            "agentId": "cursor-agent-1",
            "role": "assistant",
            "text": "managed cursor hello",
            "WEB_TERMINAL_PROJECT_PATH": "/workspace/payload-project",
        },
        project_path="/workspace/explicit-project",
    )
    assert event is not None
    event = event.__class__(
        provider=event.provider,
        client_id=event.client_id,
        window_id=event.window_id,
        source_path=event.source_path,
        offset=event.offset,
        cursor=event.cursor,
        project_path=None,
        payload=event.payload,
    )

    row = await persist_managed_agent_event(db_session, event)

    ai_session = await db_session.get(AiSession, row.ai_session_id)
    assert ai_session is not None
    assert ai_session.project_path == "/workspace/payload-project"


@pytest.mark.asyncio
async def test_receive_managed_codex_trace_stores_payload_source_and_project_metadata(db_session):
    client, window = await create_client_and_window(db_session, cwd="/workspace/window-fallback")
    payload = {
        "trace_id": "trace-managed-1",
        "span": {"name": "tool_call", "attributes": {"tool": "bash"}},
        "client_id": str(client.id),
        "virtual_window_id": str(window.id),
        "source_path": "/home/user/.codex/trace.jsonl",
        "project_path": "/workspace/codex-project",
        "cursor": "cursor-42",
    }

    row = await receive_managed_codex_trace(
        db_session,
        payload,
        client_id=client.id,
        window_id=window.id,
    )

    ai_session = await db_session.get(AiSession, row.ai_session_id)
    assert ai_session is not None
    assert ai_session.provider == "codex"
    assert ai_session.source_id == "trace-managed-1"
    assert ai_session.source_path == "/home/user/.codex/trace.jsonl"
    assert ai_session.project_path == "/workspace/codex-project"


@pytest.mark.asyncio
async def test_receive_managed_codex_trace_uses_payload_cursor_or_offset(monkeypatch, db_session):
    captured_events = []

    async def fake_persist_managed_agent_event(session, event, *, es_client=None):  # noqa: ANN001
        assert session is db_session
        assert es_client == "search-client"
        captured_events.append(event)
        return None

    monkeypatch.setattr(
        agent_event_ingest,
        "persist_managed_agent_event",
        fake_persist_managed_agent_event,
    )
    client_id = uuid4()
    window_id = uuid4()
    base_payload = {
        "trace_id": "trace-managed-1",
        "client_id": str(client_id),
        "virtual_window_id": str(window_id),
    }

    await receive_managed_codex_trace(
        db_session,
        {**base_payload, "cursor": "cursor-42", "offset": 37},
        client_id=client_id,
        window_id=window_id,
        es_client="search-client",
    )
    await receive_managed_codex_trace(
        db_session,
        {**base_payload, "offset": 37},
        client_id=client_id,
        window_id=window_id,
        es_client="search-client",
    )

    assert captured_events[0].cursor == "cursor-42"
    assert captured_events[0].offset is None
    assert captured_events[1].cursor == 37
    assert captured_events[1].offset is None


@pytest.mark.asyncio
async def test_persist_managed_legacy_claude_alias_stores_claude_code_session(db_session):
    client, window = await create_client_and_window(db_session, cwd="/workspace/window-fallback")
    payload = {
        "type": "assistant",
        "message": {"role": "assistant", "content": "managed claude hello"},
        "sessionId": "claude-session-1",
        "WEB_TERMINAL_CLIENT_ID": str(client.id),
        "WEB_TERMINAL_WINDOW_ID": str(window.id),
    }
    event = managed_event_from_payload(
        client.id,
        window.id,
        "claude",
        payload,
        source_path="/home/user/.claude/session.jsonl",
        offset=13,
    )
    assert event is not None

    row = await persist_managed_agent_event(db_session, event)

    ai_session = await db_session.get(AiSession, row.ai_session_id)
    assert row.source_type is EventSourceType.agent_tool_record
    assert row.source_id == "claude-session-1"
    assert row.virtual_window_id == window.id
    assert ai_session is not None
    assert ai_session.provider == "claude_code"
    assert ai_session.source_id == "claude-session-1"
    assert ai_session.source_path == "/home/user/.claude/session.jsonl"
    assert ai_session.project_path == "/workspace/window-fallback"
    assert ai_session.virtual_window_id == window.id
