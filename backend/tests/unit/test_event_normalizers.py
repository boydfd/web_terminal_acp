import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.model_base import Base
from app.models import Event, EventSourceType
import app.repositories.events as events_repository
from app.repositories.events import insert_normalized_event
from app.services.ingest.normalizers import normalize_claude_jsonl, normalize_codex_trace


def stable_hash(value):
    stable_json = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(stable_json.encode("utf-8")).hexdigest()


def test_normalize_claude_user_message():
    raw = {"type": "user", "message": {"content": "fix nginx 403"}, "sessionId": "claude-session-1"}
    event = normalize_claude_jsonl(raw, source_path="/tmp/a.jsonl", offset=12)
    assert event.source_type == EventSourceType.claude_jsonl
    assert event.source_id == "claude-session-1"
    assert event.kind == "user_message"
    assert event.text == "fix nginx 403"
    assert event.fingerprint == f"claude_jsonl:/tmp/a.jsonl:12:{stable_hash(raw)}"
    assert event.payload_json == raw


def test_normalize_claude_assistant_content_list_extracts_text_blocks():
    raw = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "First"},
                {"type": "tool_use", "name": "bash", "input": {"command": "ls"}},
                {"text": "Second"},
                "third",
                {"type": "text", "text": 123},
            ]
        },
        "session_id": "claude-session-2",
    }

    event = normalize_claude_jsonl(raw, source_path="/tmp/b.jsonl", offset=34)

    assert event.source_id == "claude-session-2"
    assert event.kind == "assistant_message"
    assert event.text == "First\nSecond\nthird"


def test_normalize_claude_missing_session_id_uses_source_path():
    raw = {"type": "tool_use", "message": {"content": [{"text": "run tests"}]}}

    event = normalize_claude_jsonl(raw, source_path="/tmp/no-session.jsonl", offset=1)

    assert event.source_id == "/tmp/no-session.jsonl"
    assert event.kind == "tool_call"


def test_normalize_claude_long_session_id_is_bounded_and_deterministic():
    session_id = "claude-session-" + "x" * 900
    raw = {"type": "user", "message": {"content": "hello"}, "sessionId": session_id}

    event_a = normalize_claude_jsonl(raw, source_path="/tmp/a.jsonl", offset=1)
    event_b = normalize_claude_jsonl(raw, source_path="/tmp/a.jsonl", offset=1)

    assert event_a.source_id == event_b.source_id
    assert event_a.source_id.startswith("claude-session-")
    assert len(event_a.source_id) <= 512
    assert event_a.kind == "user_message"


def test_normalize_claude_fingerprint_includes_payload_hash_to_avoid_rotation_collision():
    source_path = "/tmp/session.jsonl"
    first_raw = {"type": "user", "message": {"content": "before rotation"}}
    rotated_raw = {"type": "user", "message": {"content": "after rotation"}}

    first_event = normalize_claude_jsonl(first_raw, source_path=source_path, offset=0)
    rotated_event = normalize_claude_jsonl(rotated_raw, source_path=source_path, offset=0)

    assert first_event.fingerprint != rotated_event.fingerprint
    assert first_event.fingerprint.endswith(stable_hash(first_raw))
    assert rotated_event.fingerprint.endswith(stable_hash(rotated_raw))


def test_normalize_claude_long_path_fingerprint_is_bounded_and_deterministic():
    source_path = f"/tmp/{'nested/' * 30}session.jsonl"
    raw = {"type": "user", "message": {"content": "hello"}}

    event_a = normalize_claude_jsonl(raw, source_path=source_path, offset=99)
    event_b = normalize_claude_jsonl(raw, source_path=source_path, offset=99)

    assert event_a.fingerprint == event_b.fingerprint
    assert event_a.fingerprint.startswith("claude_jsonl:")
    assert len(event_a.fingerprint) <= 128


def test_normalize_codex_tool_call():
    raw = {"trace_id": "trace-1", "span": {"name": "tool_call", "attributes": {"tool": "bash", "input": "ls"}}}
    event = normalize_codex_trace(raw)
    assert event.source_type == EventSourceType.codex_trace
    assert event.source_id == "trace-1"
    assert event.kind == "tool_call"
    assert "bash" in event.text
    assert event.payload_json == raw


def test_normalize_codex_long_trace_id_is_bounded_and_deterministic():
    trace_id = "trace-" + "x" * 900
    raw = {"trace_id": trace_id, "span": {"name": "tool_call", "attributes": {"tool": "bash"}}}

    event_a = normalize_codex_trace(raw)
    event_b = normalize_codex_trace(raw)

    assert event_a.source_id == event_b.source_id
    assert event_a.source_id.startswith("trace-")
    assert len(event_a.source_id) <= 512
    assert event_a.kind == "tool_call"


@pytest.mark.parametrize(
    ("raw", "expected_prefix"),
    [
        (
            {"trace_id": "trace-span", "span": {"name": "span-name-" + "x" * 300}},
            "span-name-",
        ),
        ({"trace_id": "trace-raw", "name": "raw-name-" + "x" * 300}, "raw-name-"),
    ],
)
def test_normalize_codex_long_span_or_raw_name_is_bounded_and_deterministic(raw, expected_prefix):
    event_a = normalize_codex_trace(raw)
    event_b = normalize_codex_trace(raw)

    assert event_a.kind == event_b.kind
    assert event_a.kind.startswith(expected_prefix)
    assert len(event_a.kind) <= 128


def test_normalize_codex_fingerprint_is_deterministic_regardless_dict_order():
    raw_a = {
        "trace_id": "trace-ordered",
        "span": {"name": "tool_call", "attributes": {"tool": "bash", "input": "ls"}},
    }
    raw_b = {
        "span": {"attributes": {"input": "ls", "tool": "bash"}, "name": "tool_call"},
        "trace_id": "trace-ordered",
    }

    event_a = normalize_codex_trace(raw_a)
    event_b = normalize_codex_trace(raw_b)

    assert event_a.fingerprint == event_b.fingerprint
    assert len(event_a.fingerprint) <= 128


@pytest.mark.asyncio
async def test_insert_normalized_event_is_idempotent_by_fingerprint_with_sqlite_create_all():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    raw = {"type": "user", "message": {"content": "fix nginx 403"}, "sessionId": "claude-session-1"}
    event = normalize_claude_jsonl(raw, source_path="/tmp/a.jsonl", offset=12)

    client_id = uuid4()
    window_id = uuid4()

    async with AsyncSession(engine, expire_on_commit=False) as session:
        first = await insert_normalized_event(
            session, event, client_id=client_id, virtual_window_id=window_id
        )
        second = await insert_normalized_event(
            session, event, client_id=client_id, virtual_window_id=window_id
        )
        row_count = await session.scalar(select(func.count()).select_from(Event))

        assert second.id == first.id
        assert row_count == 1
        assert first.client_id == client_id
        assert first.virtual_window_id == window_id
        assert first.payload_json == raw
        assert Event.__table__.c.payload_json.nullable is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_insert_normalized_event_allows_same_fingerprint_for_different_clients():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    raw = {"type": "user", "message": {"content": "fix nginx 403"}, "sessionId": "claude-session-1"}
    event = normalize_claude_jsonl(raw, source_path="/tmp/a.jsonl", offset=12)

    first_client_id = uuid4()
    second_client_id = uuid4()

    async with AsyncSession(engine, expire_on_commit=False) as session:
        first = await insert_normalized_event(session, event, client_id=first_client_id)
        second = await insert_normalized_event(session, event, client_id=second_client_id)
        duplicate_first = await insert_normalized_event(session, event, client_id=first_client_id)
        row_count = await session.scalar(select(func.count()).select_from(Event))

        assert first.id != second.id
        assert duplicate_first.id == first.id
        assert first.client_id == first_client_id
        assert second.client_id == second_client_id
        assert first.fingerprint == second.fingerprint == event.fingerprint
        assert row_count == 2

    await engine.dispose()


@pytest.mark.asyncio
async def test_insert_normalized_event_recovers_from_unique_fingerprint_race(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    raw = {"type": "user", "message": {"content": "fix nginx 403"}, "sessionId": "claude-session-1"}
    event = normalize_claude_jsonl(raw, source_path="/tmp/a.jsonl", offset=12)

    client_id = uuid4()

    async with AsyncSession(engine, expire_on_commit=False) as session:
        existing = await insert_normalized_event(session, event, client_id=client_id)

    original_select = events_repository._select_event_by_fingerprint
    call_count = 0

    async def race_select(session, selected_client_id, fingerprint):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None
        return await original_select(session, selected_client_id, fingerprint)

    monkeypatch.setattr(events_repository, "_select_event_by_fingerprint", race_select)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        raced = await insert_normalized_event(session, event, client_id=client_id)
        row_count = await session.scalar(select(func.count()).select_from(Event))

        assert raced.id == existing.id
        assert raced.client_id == client_id
        assert row_count == 1
        assert call_count == 2

    await engine.dispose()
