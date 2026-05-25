from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.agent_tools import agent_activity_source_types
from app.models import Event, SummaryJob, SummaryJobStatus, VirtualWindow
from app.services.window_runtime_tags import agent_from_command

INPUT_IDLE_REASON = "input_idle"
INPUT_INITIAL_MAX_WAIT_REASON = "input_initial_max_wait"
INPUT_REPEAT_REASON = "input_repeat"
TERMINAL_INPUT_COMMAND_KIND = "terminal_input_command"
TERMINAL_OUTPUT_KIND = "terminal_output"


async def schedule_summary_after_terminal_input(
    session: AsyncSession,
    window: VirtualWindow,
    *,
    now: datetime | None = None,
) -> SummaryJob | None:
    del now  # Scheduling is based on captured input and summary timestamps, not wall-clock polling time.

    input_events = await _terminal_input_events(session, window.id)
    input_times = [_event_input_time(event) for event in input_events]
    if not input_times:
        return None
    if _latest_command_agent(input_events) is not None:
        return None

    settings = get_settings()
    first_input_at = input_times[0]
    last_activity_at = await _last_summary_activity_at(session, window, input_events)
    last_summary_at = await _last_summary_at(session, window.id)

    if last_summary_at is None:
        idle_run_after = last_activity_at + timedelta(seconds=settings.terminal_summary_idle_seconds)
        max_wait_run_after = first_input_at + timedelta(
            seconds=settings.terminal_summary_initial_max_wait_seconds
        )
        run_after = min(max_wait_run_after, idle_run_after)
        trigger_reason = (
            INPUT_INITIAL_MAX_WAIT_REASON if max_wait_run_after < idle_run_after else INPUT_IDLE_REASON
        )
    else:
        idle_run_after = last_activity_at + timedelta(seconds=settings.terminal_summary_idle_seconds)
        repeat_run_after = last_summary_at + timedelta(seconds=settings.terminal_summary_repeat_seconds)
        run_after = min(repeat_run_after, idle_run_after)
        trigger_reason = INPUT_REPEAT_REASON if repeat_run_after <= idle_run_after else INPUT_IDLE_REASON

    input_generation = len(input_times)
    pending_job = await _pending_summary_job(session, window.id)
    if pending_job is None:
        pending_job = SummaryJob(
            virtual_window_id=window.id,
            status=SummaryJobStatus.pending,
        )
        session.add(pending_job)

    pending_job.run_after = run_after
    pending_job.trigger_reason = trigger_reason
    pending_job.input_generation = input_generation
    await session.flush()
    return pending_job


async def _terminal_input_events(session: AsyncSession, window_id: UUID) -> list[Event]:
    return list(
        await session.scalars(
            select(Event)
            .where(
                Event.virtual_window_id == window_id,
                Event.kind == TERMINAL_INPUT_COMMAND_KIND,
            )
            .order_by(Event.created_at, Event.id)
        )
    )


async def _last_summary_activity_at(
    session: AsyncSession,
    window: VirtualWindow,
    input_events: list[Event],
) -> datetime:
    input_times = [_event_input_time(event) for event in input_events]
    latest_input_at = input_times[-1]

    latest_activity = await session.scalar(
        select(Event.created_at)
        .where(
            Event.virtual_window_id == window.id,
            or_(
                Event.kind.in_([TERMINAL_INPUT_COMMAND_KIND, TERMINAL_OUTPUT_KIND]),
                Event.source_type.in_(agent_activity_source_types()),
            ),
        )
        .order_by(desc(Event.created_at), desc(Event.id))
        .limit(1)
    )
    return _ensure_aware(latest_activity) if latest_activity is not None else latest_input_at


def _latest_command_agent(input_events: list[Event]) -> str | None:
    if not input_events:
        return None
    command = input_events[-1].payload_json.get("command")
    return agent_from_command(command if isinstance(command, str) else None)


def _event_input_time(event: Event) -> datetime:
    captured_at = event.payload_json.get("captured_at")
    if isinstance(captured_at, str):
        try:
            return _ensure_aware(datetime.fromisoformat(captured_at))
        except ValueError:
            pass
    return _ensure_aware(event.created_at)


async def _last_summary_at(session: AsyncSession, window_id: UUID) -> datetime | None:
    job = await session.scalar(
        select(SummaryJob)
        .where(
            SummaryJob.virtual_window_id == window_id,
            SummaryJob.status == SummaryJobStatus.succeeded,
        )
        .order_by(desc(SummaryJob.updated_at), desc(SummaryJob.created_at), desc(SummaryJob.id))
        .limit(1)
    )
    if job is None:
        return None
    return _ensure_aware(job.updated_at or job.created_at)


async def _pending_summary_job(session: AsyncSession, window_id: UUID) -> SummaryJob | None:
    return await session.scalar(
        select(SummaryJob)
        .where(
            SummaryJob.virtual_window_id == window_id,
            SummaryJob.status == SummaryJobStatus.pending,
        )
        .order_by(SummaryJob.created_at, SummaryJob.id)
        .limit(1)
    )


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
