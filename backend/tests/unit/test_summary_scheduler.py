from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import Settings
from app.model_base import Base
from app.models import Event, EventSourceType, SummaryJob, SummaryJobStatus, VirtualWindow, WindowStatus
from app.repositories.summary_jobs import claim_next_summary_job
from app.services.summary_scheduler import (
    AGENT_IDLE_REASON,
    schedule_summary_after_agent_activity,
    schedule_summary_after_terminal_input,
)
from app.services.terminal_output_recorder import record_terminal_input_command


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


@pytest.fixture
async def counted_db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session, statements

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


async def add_shell_input(db_session, window, captured_at, sequence):
    return await add_input_event(db_session, window, captured_at, sequence)


async def add_agent_event(db_session, window, created_at, *, fingerprint: str, kind: str = "assistant_message"):
    event = Event(
        client_id=window.client_id,
        source_type=EventSourceType.agent_tool_record,
        source_id="agent-session-1",
        kind=kind,
        virtual_window_id=window.id,
        payload_json={
            "provider": "cursor_cli",
            "role": "user" if kind == "user_message" else "assistant",
            "content": "please summarize this work" if kind == "user_message" else "working",
        },
        fingerprint=fingerprint,
        created_at=created_at,
    )
    db_session.add(event)
    await db_session.flush()
    return event


async def add_agent_command_input(db_session, window, captured_at, sequence):
    event = Event(
        client_id=window.client_id,
        source_type=EventSourceType.terminal,
        source_id=str(window.id),
        kind="terminal_input_command",
        virtual_window_id=window.id,
        payload_json={
            "command": "codex",
            "shell": "bash",
            "captured_at": captured_at.isoformat(),
            "sequence": sequence,
        },
        fingerprint=f"terminal_input_command:{window.id}:agent-{sequence}",
        created_at=captured_at,
    )
    db_session.add(event)
    await db_session.flush()
    return event


@pytest.mark.asyncio
async def test_settings_include_terminal_summary_defaults(monkeypatch):
    settings = Settings(_env_file=None)

    assert settings.terminal_summary_idle_seconds == 20
    assert settings.terminal_summary_initial_max_wait_seconds == 120
    assert settings.terminal_summary_repeat_seconds == 600


@pytest.mark.asyncio
async def test_first_input_schedules_after_idle_window(db_session):
    window = await create_window(db_session)
    first_input_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    await add_input_event(db_session, window, first_input_at, 1)

    job = await schedule_summary_after_terminal_input(db_session, window)

    assert job is not None
    assert job.status == SummaryJobStatus.pending
    assert job.run_after == first_input_at + timedelta(seconds=20)
    assert job.trigger_reason == "input_idle"
    assert job.input_generation == 1


@pytest.mark.asyncio
async def test_sustained_input_reschedules_to_latest_input_plus_idle(db_session):
    window = await create_window(db_session)
    first_input_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    last_input_at = first_input_at + timedelta(seconds=10)
    await add_input_event(db_session, window, first_input_at, 1)
    await add_input_event(db_session, window, last_input_at, 2)

    job = await schedule_summary_after_terminal_input(db_session, window)

    assert job is not None
    assert job.run_after == last_input_at + timedelta(seconds=20)
    assert job.trigger_reason == "input_idle"
    assert job.input_generation == 2


@pytest.mark.asyncio
async def test_sustained_input_uses_initial_max_wait(db_session):
    window = await create_window(db_session)
    first_input_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    last_input_at = first_input_at + timedelta(seconds=110)
    await add_input_event(db_session, window, first_input_at, 1)
    await add_input_event(db_session, window, last_input_at, 2)

    job = await schedule_summary_after_terminal_input(db_session, window)

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

    job = await schedule_summary_after_terminal_input(db_session, window)

    assert job is not None
    assert job.run_after == last_summary_at + timedelta(seconds=600)
    assert job.trigger_reason == "input_repeat"


@pytest.mark.asyncio
async def test_plain_terminal_input_schedules_summary_job(db_session):
    window = await create_window(db_session)
    captured_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    await db_session.commit()

    await record_terminal_input_command(
        db_session,
        client_id=window.client_id,
        window_id=window.id,
        raw_command="echo hello",
        shell="bash",
        cwd="/workspace/project",
        captured_at=captured_at,
        sequence=1,
    )

    jobs = (await db_session.execute(select(SummaryJob))).scalars().all()
    assert len(jobs) == 1
    run_after = jobs[0].run_after
    if run_after.tzinfo is None:
        run_after = run_after.replace(tzinfo=timezone.utc)
    assert run_after == captured_at + timedelta(seconds=20)
    assert jobs[0].trigger_reason == "input_idle"


@pytest.mark.asyncio
async def test_agent_tool_record_activity_does_not_extend_input_idle_window(db_session):
    window = await create_window(db_session)
    first_input_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    agent_activity_at = first_input_at + timedelta(seconds=5)
    await add_input_event(db_session, window, first_input_at, 1)
    await add_agent_event(db_session, window, agent_activity_at, fingerprint="cursor-agent-activity")

    job = await schedule_summary_after_terminal_input(db_session, window)

    assert job is not None
    assert job.run_after == first_input_at + timedelta(seconds=20)
    assert job.trigger_reason == "input_idle"


@pytest.mark.asyncio
async def test_terminal_output_does_not_extend_input_idle_window(db_session):
    window = await create_window(db_session)
    first_input_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    output_at = first_input_at + timedelta(seconds=5)
    await add_input_event(db_session, window, first_input_at, 1)
    db_session.add(
        Event(
            client_id=window.client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_output",
            virtual_window_id=window.id,
            payload_json={"text": "command output\n"},
            fingerprint=f"terminal_output:{window.id}:1",
            created_at=output_at,
        )
    )
    await db_session.flush()

    job = await schedule_summary_after_terminal_input(db_session, window)

    assert job is not None
    assert job.run_after == first_input_at + timedelta(seconds=20)


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

    job = await schedule_summary_after_terminal_input(db_session, window)

    assert job.id == pending.id
    assert job.run_after == first_input_at + timedelta(seconds=20)
    assert job.trigger_reason == "input_idle"
    assert job.input_generation == 1


@pytest.mark.asyncio
async def test_first_agent_activity_after_idle_schedules_summary(db_session):
    window = await create_window(db_session)
    agent_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    event = await add_agent_event(db_session, window, agent_at, fingerprint="agent-1")

    job = await schedule_summary_after_agent_activity(db_session, window, event=event)

    assert job is not None
    assert job.status == SummaryJobStatus.pending
    assert job.run_after == agent_at + timedelta(seconds=20)
    assert job.trigger_reason == AGENT_IDLE_REASON
    assert job.input_generation == 1


@pytest.mark.asyncio
async def test_first_agent_activity_ignores_current_event_in_prior_idle_check(db_session):
    window = await create_window(db_session)
    agent_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    event = await add_agent_event(db_session, window, agent_at, fingerprint="agent-current")

    job = await schedule_summary_after_agent_activity(db_session, window, event=event)

    assert job is not None
    assert job.run_after == agent_at + timedelta(seconds=20)


@pytest.mark.asyncio
async def test_agent_activity_without_prior_idle_does_not_schedule(db_session):
    window = await create_window(db_session)
    shell_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    agent_at = shell_at + timedelta(minutes=2)
    await add_shell_input(db_session, window, shell_at, 1)
    event = await add_agent_event(db_session, window, agent_at, fingerprint="agent-1")

    job = await schedule_summary_after_agent_activity(db_session, window, event=event)

    assert job is None


@pytest.mark.asyncio
async def test_agent_activity_scheduler_avoids_terminal_output_scans(counted_db_session):
    db_session, statements = counted_db_session
    window = await create_window(db_session)
    agent_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    event = await add_agent_event(db_session, window, agent_at, fingerprint="agent-no-output-scan")
    statements.clear()

    job = await schedule_summary_after_agent_activity(db_session, window, event=event)

    assert job is not None
    event_reads = [
        statement
        for statement in statements
        if "FROM events" in statement and "SELECT events" in statement
    ]
    assert event_reads
    assert all(" OR " not in statement.upper() for statement in event_reads)
    assert all("terminal_output" not in statement for statement in event_reads)
    source_time_reads = [
        statement
        for statement in statements
        if "FROM events" in statement
        and "events.created_at" in statement
        and "events.source_type IN" in statement
    ]
    assert len(source_time_reads) == 1
    assert "LIMIT" in source_time_reads[0].upper()


@pytest.mark.asyncio
async def test_duplicate_agent_activity_does_not_advance_generation(db_session):
    window = await create_window(db_session)
    agent_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    event = await add_agent_event(db_session, window, agent_at, fingerprint="agent-repeat")

    first_job = await schedule_summary_after_agent_activity(db_session, window, event=event)
    second_job = await schedule_summary_after_agent_activity(db_session, window, event=event)

    assert first_job is not None
    assert second_job is not None
    assert window.agent_activity_generation == 1
    assert second_job.input_generation == 1


@pytest.mark.asyncio
async def test_agent_completion_updates_window_activity_completion_state(db_session):
    window = await create_window(db_session)
    completed_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    event = Event(
        client_id=window.client_id,
        source_type=EventSourceType.agent_tool_record,
        source_id="codex-session-1",
        kind="event_msg",
        virtual_window_id=window.id,
        payload_json={
            "provider": "codex",
            "raw_type": "event_msg",
            "payload": {"type": "task_completed"},
            "timestamp": completed_at.isoformat(),
        },
        fingerprint="agent-completion-window-state",
        created_at=completed_at + timedelta(milliseconds=30),
    )
    db_session.add(event)
    await db_session.flush()

    await schedule_summary_after_agent_activity(db_session, window, event=event)

    assert window.agent_activity_latest_at == completed_at
    assert window.agent_activity_latest_completed_at == completed_at


@pytest.mark.asyncio
async def test_late_completion_updates_completion_state_without_rewinding_activity(db_session):
    window = await create_window(db_session)
    output_at = datetime(2026, 5, 21, 12, 0, 10, tzinfo=timezone.utc)
    completed_at = output_at - timedelta(seconds=10)
    window.agent_activity_latest_at = output_at
    event = Event(
        client_id=window.client_id,
        source_type=EventSourceType.agent_tool_record,
        source_id="codex-session-1",
        kind="event_msg",
        virtual_window_id=window.id,
        payload_json={
            "provider": "codex",
            "raw_type": "event_msg",
            "payload": {"type": "task_completed"},
            "timestamp": completed_at.isoformat(),
        },
        fingerprint="agent-completion-window-state-late",
        created_at=output_at + timedelta(milliseconds=30),
    )
    db_session.add(event)
    await db_session.flush()

    await schedule_summary_after_agent_activity(db_session, window, event=event)

    assert window.agent_activity_latest_at == output_at
    assert window.agent_activity_latest_completed_at == completed_at


@pytest.mark.asyncio
async def test_agent_user_message_after_agent_command_schedules_summary_after_idle(db_session):
    window = await create_window(db_session)
    command_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    user_message_at = command_at + timedelta(seconds=2)
    await add_agent_command_input(db_session, window, command_at, 1)
    event = await add_agent_event(db_session, window, user_message_at, fingerprint="agent-user-1", kind="user_message")

    job = await schedule_summary_after_agent_activity(db_session, window, event=event)

    assert job is not None
    assert job.status == SummaryJobStatus.pending
    assert job.run_after == user_message_at + timedelta(seconds=20)
    assert job.trigger_reason == AGENT_IDLE_REASON


@pytest.mark.asyncio
async def test_agent_user_message_after_recent_terminal_activity_schedules_summary_after_idle(db_session):
    window = await create_window(db_session)
    shell_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    user_message_at = shell_at + timedelta(minutes=2)
    await add_shell_input(db_session, window, shell_at, 1)
    event = await add_agent_event(db_session, window, user_message_at, fingerprint="agent-user-1", kind="user_message")

    job = await schedule_summary_after_agent_activity(db_session, window, event=event)

    assert job is not None
    assert job.run_after == user_message_at + timedelta(seconds=20)
    assert job.trigger_reason == AGENT_IDLE_REASON


@pytest.mark.asyncio
async def test_repeat_agent_events_in_same_burst_do_not_reschedule_after_summary(db_session):
    window = await create_window(db_session)
    first_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    second_at = first_at + timedelta(seconds=30)
    db_session.add(
        SummaryJob(
            virtual_window_id=window.id,
            status=SummaryJobStatus.succeeded,
            updated_at=first_at + timedelta(minutes=3),
            created_at=first_at + timedelta(minutes=3),
        )
    )
    await add_agent_event(db_session, window, first_at, fingerprint="agent-1")
    first_event = await add_agent_event(db_session, window, first_at, fingerprint="agent-3")
    await schedule_summary_after_agent_activity(db_session, window, event=first_event)
    event = await add_agent_event(db_session, window, second_at, fingerprint="agent-2")

    job = await schedule_summary_after_agent_activity(db_session, window, event=event)

    assert job is None


@pytest.mark.asyncio
async def test_new_agent_burst_after_idle_schedules_again(db_session):
    window = await create_window(db_session)
    first_burst = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    second_burst = first_burst + timedelta(minutes=10)
    db_session.add(
        SummaryJob(
            virtual_window_id=window.id,
            status=SummaryJobStatus.succeeded,
            updated_at=first_burst + timedelta(minutes=3),
            created_at=first_burst + timedelta(minutes=3),
        )
    )
    first_event = await add_agent_event(db_session, window, first_burst, fingerprint="agent-1")
    await schedule_summary_after_agent_activity(db_session, window, event=first_event)
    event = await add_agent_event(db_session, window, second_burst, fingerprint="agent-2")

    job = await schedule_summary_after_agent_activity(db_session, window, event=event)

    assert job is not None
    assert job.run_after == second_burst + timedelta(seconds=20)


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

    job = await schedule_summary_after_terminal_input(db_session, window)

    assert job is not None
    assert job.id != running.id
    assert job.status == SummaryJobStatus.pending
    assert job.run_after == now + timedelta(seconds=20)
    assert running.status == SummaryJobStatus.running
