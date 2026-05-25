from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.model_base import Base
from app.models import Event, SummaryJob, VirtualWindow, WindowStatus
from app.services.terminal_output_recorder import record_terminal_input_command


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_record_terminal_input_command_redacts_secrets_before_persisting(db_session):
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.commit()
    captured_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)

    event = await record_terminal_input_command(
        db_session,
        client_id,
        window.id,
        "curl -H 'Authorization: Bearer bearer-secret' https://example.test?token=url-secret --password cli-secret password=inline-secret",
        "/bin/bash",
        "/workspace/project",
        captured_at,
        7,
    )

    rows = (await db_session.execute(select(Event))).scalars().all()
    assert rows == [event]
    persisted_command = event.payload_json["command"]
    assert "bearer-secret" not in persisted_command
    assert "url-secret" not in persisted_command
    assert "cli-secret" not in persisted_command
    assert "inline-secret" not in persisted_command
    assert "[REDACTED]" in persisted_command


@pytest.mark.asyncio
async def test_record_terminal_input_command_writes_terminal_input_event(db_session):
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.commit()
    captured_at = datetime(2026, 5, 21, 12, 1, 2, tzinfo=timezone.utc)

    event = await record_terminal_input_command(
        db_session,
        client_id,
        window.id,
        "ls -la",
        "zsh",
        "/workspace/project",
        captured_at,
        12,
    )

    assert event.source_type.value == "terminal"
    assert event.source_id == str(window.id)
    assert event.kind == "terminal_input_command"
    assert event.client_id == client_id
    assert event.virtual_window_id == window.id
    assert event.payload_json == {
        "command": "ls -la",
        "shell": "zsh",
        "cwd": "/workspace/project",
        "captured_at": "2026-05-21T12:01:02+00:00",
        "sequence": 12,
    }
    assert event.fingerprint == f"terminal_input_command:{window.id}:12"
    assert (await db_session.get(VirtualWindow, window.id)).cwd == "/workspace/project"


@pytest.mark.asyncio
async def test_record_terminal_input_command_schedules_summary_job(db_session):
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.commit()
    captured_at = datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc)

    await record_terminal_input_command(
        db_session,
        client_id,
        window.id,
        "echo schedule",
        "bash",
        "/workspace/project",
        captured_at,
        13,
    )

    jobs = (await db_session.execute(select(SummaryJob))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].run_after.replace(tzinfo=timezone.utc) == datetime(
        2026, 5, 21, 12, 4, tzinfo=timezone.utc
    )
    assert jobs[0].trigger_reason == "input_idle"
    assert jobs[0].input_generation == 1


@pytest.mark.asyncio
async def test_record_terminal_input_command_deduplicates_by_sequence_fingerprint(db_session):
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.commit()
    captured_at = datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc)

    first = await record_terminal_input_command(
        db_session,
        client_id,
        window.id,
        "echo first",
        "bash",
        "/workspace/project",
        captured_at,
        99,
    )
    second = await record_terminal_input_command(
        db_session,
        client_id,
        window.id,
        "echo second",
        "bash",
        "/workspace/other",
        captured_at,
        99,
    )

    rows = (await db_session.execute(select(Event))).scalars().all()
    assert rows == [first]
    assert second.id == first.id
    assert second.payload_json["command"] == "echo first"
