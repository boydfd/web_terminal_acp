from contextlib import asynccontextmanager
import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.model_base import Base
from app.models import AiSession, Event, EventSourceType, SummaryJob, VirtualWindow, WindowStatus
from app.repositories.clients import create_client, ensure_local_client
from app.services.ingest.claude_watcher import (
    index_claude_events,
    ingest_claude_jsonl_file,
    initial_jsonl_offsets,
    iter_jsonl_files,
    poll_claude_jsonl_directory_once,
    read_new_jsonl_events,
)
from app.services.ingest.normalizers import normalize_claude_jsonl
from app.services.search_index import AI_EVENTS_INDEX


class FakeElasticsearch:
    def __init__(self):
        self.indexed_documents = []

    async def index(self, **kwargs):
        self.indexed_documents.append(kwargs)
        return {"result": "created"}


class FailingElasticsearch:
    async def index(self, **kwargs):
        raise RuntimeError("search index unavailable")


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


def write_jsonl(path, *objects):
    path.write_text("".join(json.dumps(obj, ensure_ascii=False) + "\n" for obj in objects), encoding="utf-8")


def test_iter_jsonl_files_returns_empty_for_missing_root(tmp_path):
    assert iter_jsonl_files(tmp_path / "missing") == []


def test_iter_jsonl_files_returns_recursive_sorted_jsonl_files(tmp_path):
    root = tmp_path / "claude"
    nested = root / "b"
    nested.mkdir(parents=True)
    root.mkdir(exist_ok=True)
    (nested / "session-2.jsonl").write_text("", encoding="utf-8")
    (root / "0-session.jsonl").write_text("", encoding="utf-8")
    (root / "ignore.txt").write_text("", encoding="utf-8")

    assert iter_jsonl_files(root) == [root / "0-session.jsonl", nested / "session-2.jsonl"]


def test_initial_jsonl_offsets_start_at_existing_file_sizes(tmp_path):
    root = tmp_path / "claude"
    nested = root / "b"
    nested.mkdir(parents=True)
    root.mkdir(exist_ok=True)
    first = root / "0-session.jsonl"
    second = nested / "session-2.jsonl"
    first.write_text('{"type":"user"}\n', encoding="utf-8")
    second.write_text('{"type":"assistant"}\n', encoding="utf-8")

    assert initial_jsonl_offsets(root) == {
        first: first.stat().st_size,
        second: second.stat().st_size,
    }


def test_read_new_jsonl_events_returns_offsets(tmp_path):
    path = tmp_path / "session.jsonl"
    first = {"type": "user", "message": {"content": "hello"}, "sessionId": "s1"}
    second = {"type": "assistant", "message": {"content": "world"}, "sessionId": "s1"}
    path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")

    events, next_offset = read_new_jsonl_events(path, 0)

    assert [event[0]["type"] for event in events] == ["user", "assistant"]
    assert events[0][1] == 0
    assert next_offset == path.stat().st_size


def test_read_new_jsonl_events_skips_partial_line(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": "user"}\n{"type": ', encoding="utf-8")

    events, next_offset = read_new_jsonl_events(path, 0)

    assert len(events) == 1
    assert next_offset == len('{"type": "user"}\n'.encode())


def test_read_new_jsonl_events_resumes_from_byte_offset_for_appended_lines(tmp_path):
    path = tmp_path / "session.jsonl"
    first = {"type": "user", "message": {"content": "héllo"}, "sessionId": "s1"}
    second = {"type": "assistant", "message": {"content": "world"}, "sessionId": "s1"}
    write_jsonl(path, first)
    _events, offset = read_new_jsonl_events(path, 0)
    with path.open("ab") as file:
        file.write(json.dumps(second).encode("utf-8") + b"\n")

    events, next_offset = read_new_jsonl_events(path, offset)

    assert [event for event, _line_offset in events] == [second]
    assert events[0][1] == offset
    assert next_offset == path.stat().st_size


def test_read_new_jsonl_events_skips_invalid_complete_json_line_and_advances(tmp_path):
    path = tmp_path / "session.jsonl"
    valid_before = {"type": "user", "sessionId": "s1"}
    valid_after = {"type": "assistant", "sessionId": "s1"}
    path.write_text(
        json.dumps(valid_before) + "\n" + "{not json}\n" + json.dumps(valid_after) + "\n",
        encoding="utf-8",
    )

    events, next_offset = read_new_jsonl_events(path, 0)

    assert [event for event, _offset in events] == [valid_before, valid_after]
    assert next_offset == path.stat().st_size


def test_read_new_jsonl_events_respects_max_events_limit(tmp_path):
    path = tmp_path / "session.jsonl"
    first = {"type": "user", "sessionId": "s1"}
    second = {"type": "assistant", "sessionId": "s1"}
    third = {"type": "tool_use", "sessionId": "s1"}
    write_jsonl(path, first, second, third)

    events, next_offset = read_new_jsonl_events(path, 0, max_events=2)

    assert [event for event, _offset in events] == [first, second]
    assert next_offset == len((json.dumps(first) + "\n" + json.dumps(second) + "\n").encode("utf-8"))


def test_read_new_jsonl_events_skips_complete_line_over_max_bytes(tmp_path):
    path = tmp_path / "session.jsonl"
    huge_event = {"type": "user", "sessionId": "s1", "message": {"content": "x" * 128}}
    path.write_text(json.dumps(huge_event) + "\n", encoding="utf-8")

    events, next_offset = read_new_jsonl_events(path, 0, max_bytes=64)

    assert events == []
    assert next_offset == path.stat().st_size


@pytest.mark.asyncio
async def test_ingest_claude_jsonl_file_persists_without_indexing_before_commit(db_session, tmp_path):
    window = VirtualWindow(id=uuid4(), title="Claude", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    path = tmp_path / "session.jsonl"
    payload = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "world"}]},
        "sessionId": "s1",
        "virtual_window_id": str(window.id),
    }
    write_jsonl(path, payload)
    es_client = FakeElasticsearch()

    first_offset = await ingest_claude_jsonl_file(db_session, path, 0, es_client=es_client)
    second_offset = await ingest_claude_jsonl_file(db_session, path, 0, es_client=es_client)
    await db_session.commit()

    rows = (await db_session.execute(select(Event))).scalars().all()
    ai_sessions = (await db_session.execute(select(AiSession))).scalars().all()
    summary_jobs = (await db_session.execute(select(SummaryJob))).scalars().all()
    assert first_offset == path.stat().st_size
    assert second_offset == path.stat().st_size
    assert len(rows) == 1
    assert rows[0].indexed_at is None
    assert rows[0].client_id == window.client_id
    assert rows[0].virtual_window_id == window.id
    assert len(ai_sessions) == 1
    assert ai_sessions[0].client_id == window.client_id
    assert ai_sessions[0].provider == "claude"
    assert ai_sessions[0].source_id == "s1"
    assert ai_sessions[0].source_path == str(path)
    assert ai_sessions[0].virtual_window_id == window.id
    assert rows[0].ai_session_id == ai_sessions[0].id
    assert len(summary_jobs) == 1
    assert summary_jobs[0].virtual_window_id == window.id
    assert es_client.indexed_documents == []


@pytest.mark.asyncio
async def test_index_claude_events_indexes_committed_rows_with_deterministic_ids(db_session, tmp_path):
    window = VirtualWindow(id=uuid4(), title="Claude", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    client_id = str(window.client_id)
    path = tmp_path / "session.jsonl"
    payload = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "world"}]},
        "sessionId": "s1",
        "virtual_window_id": str(window.id),
    }
    write_jsonl(path, payload)
    await ingest_claude_jsonl_file(db_session, path, 0)
    await db_session.commit()
    es_client = FakeElasticsearch()

    indexed_count = await index_claude_events(db_session, es_client)
    await db_session.commit()

    rows = (await db_session.execute(select(Event))).scalars().all()
    assert indexed_count == 1
    assert rows[0].indexed_at is not None
    assert es_client.indexed_documents == [
        {
            "index": AI_EVENTS_INDEX,
            "id": str(rows[0].id),
            "document": {
                "client_id": client_id,
                "provider": "claude",
                "session_id": "s1",
                "kind": "assistant_message",
                "virtual_window_id": str(window.id),
                "text": "world",
                "raw": payload,
            },
        }
    ]


@pytest.mark.asyncio
async def test_index_claude_events_uses_event_row_ids_for_shared_fingerprints_across_clients(db_session):
    local_client = await ensure_local_client(db_session)
    remote_client, _token = await create_client(db_session, name="Remote Desk")
    payload = {"type": "assistant", "message": {"content": "world"}, "sessionId": "s1"}
    first_id = uuid4()
    second_id = uuid4()
    db_session.add_all(
        [
            Event(
                id=first_id,
                client_id=local_client.id,
                source_type=EventSourceType.claude_jsonl,
                source_id="shared-session.jsonl",
                kind="assistant_message",
                payload_json=payload,
                fingerprint="shared-fingerprint",
            ),
            Event(
                id=second_id,
                client_id=remote_client.id,
                source_type=EventSourceType.claude_jsonl,
                source_id="shared-session.jsonl",
                kind="assistant_message",
                payload_json=payload,
                fingerprint="shared-fingerprint",
            ),
        ]
    )
    await db_session.commit()
    es_client = FakeElasticsearch()

    indexed_count = await index_claude_events(db_session, es_client)

    assert indexed_count == 2
    assert {document["id"] for document in es_client.indexed_documents} == {str(first_id), str(second_id)}


@pytest.mark.asyncio
async def test_index_claude_events_leaves_failed_rows_for_retry(db_session, tmp_path):
    path = tmp_path / "session.jsonl"
    payload = {"type": "user", "message": {"content": "hello"}, "sessionId": "s1"}
    write_jsonl(path, payload)
    await ingest_claude_jsonl_file(db_session, path, 0)
    await db_session.commit()

    failed_count = await index_claude_events(db_session, FailingElasticsearch())
    await db_session.commit()
    row = await db_session.scalar(select(Event))
    assert failed_count == 0
    assert row is not None
    assert row.payload_json == payload
    assert row.indexed_at is None

    es_client = FakeElasticsearch()
    retried_count = await index_claude_events(db_session, es_client)
    await db_session.commit()

    assert retried_count == 1
    assert row.indexed_at is not None
    assert len(es_client.indexed_documents) == 1


@pytest.mark.asyncio
async def test_poll_claude_jsonl_directory_once_limits_changed_files_per_pass(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session_factory():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    root = tmp_path / "claude"
    root.mkdir()
    first = root / "1.jsonl"
    second = root / "2.jsonl"
    third = root / "3.jsonl"
    payload = {"type": "user", "message": {"content": "hello"}, "sessionId": "s1"}
    for path in (first, second, third):
        write_jsonl(path, payload | {"sessionId": path.stem})
    offsets = {}

    await poll_claude_jsonl_directory_once(
        session_factory,
        root,
        offsets,
        max_changed_files=2,
    )

    assert set(offsets) == {first, second}

    await engine.dispose()


@pytest.mark.asyncio
async def test_poll_claude_jsonl_directory_once_persists_commits_and_indexes(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session_factory():
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    root = tmp_path / "claude"
    root.mkdir()
    path = root / "session.jsonl"
    payload = {"type": "user", "message": {"content": "hello"}, "sessionId": "s1"}
    write_jsonl(path, payload)
    offsets = {}
    es_client = FakeElasticsearch()

    await poll_claude_jsonl_directory_once(session_factory, root, offsets, es_client=es_client)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        row = await session.scalar(select(Event))
    assert offsets[path] == path.stat().st_size
    assert row is not None
    assert row.indexed_at is not None
    assert len(es_client.indexed_documents) == 1

    await engine.dispose()


def test_long_path_fingerprints_remain_within_event_fingerprint_limit(tmp_path):
    long_root = tmp_path / ("nested" * 30)
    long_root.mkdir()
    source_path = str(long_root / "session.jsonl")
    raw = {"type": "user", "message": {"content": "hello"}}

    event = normalize_claude_jsonl(raw, source_path=source_path, offset=99)

    assert len(event.fingerprint) <= Event.__table__.c.fingerprint.type.length
