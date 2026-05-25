from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import Settings
from app.model_base import Base
from app.models import Event, EventSourceType, SummaryJob, SummaryJobStatus, VirtualWindow, WindowStatus
from app.repositories.summary_jobs import claim_next_summary_job
from app.services.summary_scheduler import schedule_summary_after_terminal_input
from app.services.terminal_output_recorder import record_terminal_output_chunk


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


async def create_window(db_session):
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    return window


async def add_input_event(db_session, window, captured_at, sequence):
    event = Event(
        client_id=window.client_id,
        source_type=EventSourceType.terminal,
        source_id=str(window.id),
        kind="terminal_input_command",
        virtual_window_id=window.id,
        payload_json={
            "command": f"echo {sequence}",
            "shell": "bash",
            "captured_at": captured_at.isoformat(),
            "sequence": sequence,
        },
        fingerprint=f"terminal_input_command:{window.id}:{sequence}",
        created_at=captured_at,
    )
    db_session.add(event)
    await db_session.flush()
    return event


@pytest.mark.asyncio
async def test_settings_include_terminal_summary_defaults(monkeypatch):
    settings = Settings(_env_file=None)

    assert settings.terminal_summary_idle_seconds == 120
    assert settings.terminal_summary_initial_max_wait_seconds == 120
    assert settings.terminal_summary_repeat_seconds == 600


@pytest.mark.asyncio
async def test_first_input_schedules_after_idle_window(db_session):
    window = await create_window(db_session)
    first_input_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    await add_input_event(db_session, window, first_input_at, 1)

    job = await schedule_summary_after_terminal_input(
        db_session,
        window,
        now=first_input_at + timedelta(seconds=1),
    )

    assert job is not None
    assert job.status == SummaryJobStatus.pending
    assert job.run_after == first_input_at + timedelta(minutes=2)
    assert job.trigger_reason == "input_idle"
    assert job.input_generation == 1


@pytest.mark.asyncio
async def test_sustained_input_uses_initial_max_wait(db_session):
    window = await create_window(db_session)
    first_input_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    last_input_at = first_input_at + timedelta(seconds=50)
    await add_input_event(db_session, window, first_input_at, 1)
    await add_input_event(db_session, window, last_input_at, 2)

    job = await schedule_summary_after_terminal_input(
        db_session,
        window,
        now=last_input_at,
    )

    assert job is not None
    assert job.run_after == first_input_at + timedelta(minutes=2)
    assert job.trigger_reason == "input_initial_max_wait"
    assert job.input_generation == 2


@pytest.mark.asyncio
async def test_repeat_after_succeeded_summary_uses_ten_minute_limit(db_session):
    window = await create_window(db_session)
    last_summary_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    last_input_at = last_summary_at + timedelta(minutes=20)
    db_session.add(
        SummaryJob(
            virtual_window_id=window.id,
            status=SummaryJobStatus.succeeded,
            updated_at=last_summary_at,
            created_at=last_summary_at,
        )
    )
    await add_input_event(db_session, window, last_input_at, 1)

    job = await schedule_summary_after_terminal_input(
        db_session,
        window,
        now=last_input_at,
    )

    assert job is not None
    assert job.run_after == last_summary_at + timedelta(seconds=600)
    assert job.trigger_reason == "input_repeat"


@pytest.mark.asyncio
async def test_existing_pending_job_updates_run_after_reason_and_generation(db_session):
    window = await create_window(db_session)
    first_input_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    pending = SummaryJob(
        virtual_window_id=window.id,
        status=SummaryJobStatus.pending,
        run_after=first_input_at + timedelta(minutes=5),
        trigger_reason="old_reason",
        input_generation=0,
    )
    db_session.add(pending)
    await add_input_event(db_session, window, first_input_at, 1)

    job = await schedule_summary_after_terminal_input(db_session, window, now=first_input_at)

    assert job.id == pending.id
    assert job.run_after == first_input_at + timedelta(minutes=2)
    assert job.trigger_reason == "input_idle"
    assert job.input_generation == 1


@pytest.mark.asyncio
async def test_no_input_command_does_not_schedule(db_session):
    window = await create_window(db_session)

    job = await schedule_summary_after_terminal_input(
        db_session,
        window,
        now=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    summary_jobs = (await db_session.execute(select(SummaryJob))).scalars().all()
    assert job is None
    assert summary_jobs == []


@pytest.mark.asyncio
async def test_terminal_output_only_does_not_create_summary_job(db_session):
    window = await create_window(db_session)
    await db_session.commit()

    await record_terminal_output_chunk(db_session, window.client_id, window.id, b"output only\n")

    summary_jobs = (await db_session.execute(select(SummaryJob))).scalars().all()
    assert summary_jobs == []


@pytest.mark.asyncio
async def test_agent_command_defers_terminal_input_summary(db_session):
    window = await create_window(db_session)
    first_input_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    event = Event(
        client_id=window.client_id,
        source_type=EventSourceType.terminal,
        source_id=str(window.id),
        kind="terminal_input_command",
        virtual_window_id=window.id,
        payload_json={
            "command": "agent -p 'fix tests'",
            "shell": "bash",
            "captured_at": first_input_at.isoformat(),
            "sequence": 1,
        },
        fingerprint=f"terminal_input_command:{window.id}:agent",
        created_at=first_input_at,
    )
    db_session.add(event)
    await db_session.flush()

    job = await schedule_summary_after_terminal_input(db_session, window, now=first_input_at)

    assert job is None


@pytest.mark.asyncio
async def test_agent_tool_record_activity_extends_idle_window(db_session):
    window = await create_window(db_session)
    first_input_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    agent_activity_at = first_input_at + timedelta(seconds=30)
    await add_input_event(db_session, window, first_input_at, 1)
    db_session.add(
        Event(
            client_id=window.client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="cursor-session-1",
            kind="assistant_message",
            virtual_window_id=window.id,
            payload_json={"provider": "cursor_cli", "role": "assistant", "content": "working"},
            fingerprint="cursor-agent-activity",
            created_at=agent_activity_at,
        )
    )
    await db_session.flush()

    job = await schedule_summary_after_terminal_input(db_session, window, now=agent_activity_at)

    assert job is not None
    assert job.run_after == first_input_at + timedelta(minutes=2)
    assert job.trigger_reason == "input_initial_max_wait"


@pytest.mark.asyncio
async def test_pending_followup_waits_until_running_job_completes(db_session):
    window = await create_window(db_session)
    now = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    running = SummaryJob(virtual_window_id=window.id, status=SummaryJobStatus.running)
    pending = SummaryJob(
        virtual_window_id=window.id,
        status=SummaryJobStatus.pending,
        run_after=now - timedelta(seconds=1),
        trigger_reason="input_idle",
        input_generation=2,
    )
    db_session.add_all([running, pending])
    await db_session.flush()

    blocked_claim = await claim_next_summary_job(db_session)
    assert blocked_claim is None

    running.status = SummaryJobStatus.succeeded
    await db_session.flush()
    followup_claim = await claim_next_summary_job(db_session)

    assert followup_claim is not None
    assert followup_claim.id == pending.id
    assert followup_claim.status == SummaryJobStatus.running


@pytest.mark.asyncio
async def test_running_job_is_not_preempted_and_new_pending_job_can_follow(db_session):
    window = await create_window(db_session)
    now = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    running = SummaryJob(
        virtual_window_id=window.id,
        status=SummaryJobStatus.running,
        run_after=None,
        trigger_reason="input_idle",
        input_generation=1,
    )
    db_session.add(running)
    await add_input_event(db_session, window, now, 2)

    job = await schedule_summary_after_terminal_input(db_session, window, now=now)

    assert job is not None
    assert job.id != running.id
    assert job.status == SummaryJobStatus.pending
    assert job.run_after == now + timedelta(minutes=2)
    assert running.status == SummaryJobStatus.running
