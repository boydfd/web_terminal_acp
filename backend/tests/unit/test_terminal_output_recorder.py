from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.model_base import Base
from app.models import Event, SummaryJob, VirtualWindow, WindowStatus
from app.services.search_index import TERMINAL_INDEX
from app.services.terminal_output_recorder import record_terminal_input_command, record_terminal_output_chunk


class FakeElasticsearch:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.indexed_documents = []

    async def index(self, **kwargs):
        if self.fail:
            raise RuntimeError("Elasticsearch unavailable")
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


@pytest.mark.asyncio
async def test_record_terminal_output_persists_and_indexes_without_enqueuing_summary_job(db_session):
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.commit()
    es_client = FakeElasticsearch()

    event = await record_terminal_output_chunk(db_session, client_id, window.id, b"hello terminal\n", es_client)

    rows = (await db_session.execute(select(Event))).scalars().all()
    assert rows == [event]
    assert event.source_type.value == "terminal"
    assert event.source_id == str(window.id)
    assert event.kind == "terminal_output"
    assert event.client_id == client_id
    assert event.virtual_window_id == window.id
    assert event.payload_json == {"text": "hello terminal\n"}
    assert event.indexed_at is not None
    assert es_client.indexed_documents == [
        {
            "index": TERMINAL_INDEX,
            "id": str(event.id),
            "document": {
                "client_id": str(client_id),
                "virtual_window_id": str(window.id),
                "text": "hello terminal\n",
                "source_event_ids": [str(event.id)],
            },
        }
    ]


@pytest.mark.asyncio
async def test_record_terminal_output_keeps_unindexed_event_when_indexing_fails(db_session):
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.commit()

    event = await record_terminal_output_chunk(
        db_session,
        client_id,
        window.id,
        b"index later",
        FakeElasticsearch(fail=True),
    )

    rows = (await db_session.execute(select(Event))).scalars().all()
    assert rows == [event]
    assert event.client_id == client_id
    assert event.payload_json == {"text": "index later"}
    assert event.indexed_at is None


@pytest.mark.asyncio
async def test_record_terminal_output_ignores_empty_decoded_chunks(db_session):
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.commit()

    event = await record_terminal_output_chunk(db_session, client_id, window.id, b"", FakeElasticsearch())

    rows = (await db_session.execute(select(Event))).scalars().all()
    assert event is None
    assert rows == []


@pytest.mark.asyncio
async def test_agent_tui_terminal_output_does_not_enqueue_summary_job(db_session):
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.commit()

    await record_terminal_input_command(
        db_session,
        client_id,
        window.id,
        "codex",
        "bash",
        "/workspace/project",
        datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        1,
    )
    await record_terminal_output_chunk(db_session, client_id, window.id, b"codex tui refresh\n")

    jobs = (await db_session.execute(select(SummaryJob))).scalars().all()
    assert jobs == []
