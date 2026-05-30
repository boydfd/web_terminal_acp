from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_tools import agent_activity_source_types, get_agent_tool_registry
from app.config import get_settings
from app.models import Event, SummaryJob, SummaryJobStatus, VirtualWindow
from app.services.agent_activity_projection import event_activity_time, event_is_agent_completion
from app.services.window_runtime_tags import agent_from_command

INPUT_IDLE_REASON = "input_idle"
INPUT_INITIAL_MAX_WAIT_REASON = "input_initial_max_wait"
INPUT_REPEAT_REASON = "input_repeat"
AGENT_IDLE_REASON = "agent_idle"
TERMINAL_INPUT_COMMAND_KIND = "terminal_input_command"
TERMINAL_COMMAND_FINISHED_KIND = "terminal_command_finished"
AGENT_WORK_PRESENCE_KIND = "agent_work_presence"
SUMMARY_AGENT_BURST_GAP_SECONDS = 5 * 60


@dataclass(frozen=True)
class _TerminalInputActivity:
    first_event: Event
    latest_event: Event
    total: int


async def schedule_summary_after_terminal_input(
    session: AsyncSession,
    window: VirtualWindow,
    *,
    now: datetime | None = None,
) -> SummaryJob | None:
    """Schedule a summary after shell user input goes idle."""
    del now

    input_activity = await _terminal_input_activity(session, window)
    if input_activity is None:
        return None
    if _command_agent(input_activity.latest_event) is not None:
        return None

    settings = get_settings()
    first_input_at = _event_input_time(input_activity.first_event)
    last_input_at = _event_input_time(input_activity.latest_event)
    last_summary_at = await _last_summary_at(session, window.id)

    if last_summary_at is None:
        idle_run_after = last_input_at + timedelta(seconds=settings.terminal_summary_idle_seconds)
        max_wait_run_after = first_input_at + timedelta(
            seconds=settings.terminal_summary_initial_max_wait_seconds
        )
        run_after = min(max_wait_run_after, idle_run_after)
        trigger_reason = (
            INPUT_INITIAL_MAX_WAIT_REASON if max_wait_run_after < idle_run_after else INPUT_IDLE_REASON
        )
    else:
        idle_run_after = last_input_at + timedelta(seconds=settings.terminal_summary_idle_seconds)
        repeat_run_after = last_summary_at + timedelta(seconds=settings.terminal_summary_repeat_seconds)
        run_after = min(repeat_run_after, idle_run_after)
        trigger_reason = INPUT_REPEAT_REASON if repeat_run_after <= idle_run_after else INPUT_IDLE_REASON

    input_generation = input_activity.total
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


async def schedule_summary_after_agent_activity(
    session: AsyncSession,
    window: VirtualWindow,
    *,
    event: Event | None = None,
    now: datetime | None = None,
) -> SummaryJob | None:
    """Schedule a summary after user agent chat or long-idle assistant activity."""
    del now

    activity_at = _agent_activity_time_from_event(event) if event is not None else None
    if activity_at is not None:
        _touch_agent_activity_state(window, event, activity_at)

    user_message_job = await _schedule_after_latest_agent_user_message(session, window, event)
    if user_message_job is not None:
        return user_message_job

    burst_start = _ensure_aware(window.agent_activity_burst_start_at) if window.agent_activity_burst_start_at else None
    last_activity_at = _ensure_aware(window.agent_activity_latest_at) if window.agent_activity_latest_at else None
    if burst_start is None or last_activity_at is None:
        return None
    if not await _was_idle_before(
        session,
        window.id,
        burst_start,
        current_event_id=event.id if event is not None else None,
    ):
        return None

    last_summary_at = await _last_summary_at(session, window.id)
    if last_summary_at is not None and last_summary_at >= burst_start:
        return None

    settings = get_settings()
    run_after = last_activity_at + timedelta(seconds=settings.terminal_summary_idle_seconds)

    pending_job = await _pending_summary_job(session, window.id)
    if pending_job is None:
        pending_job = SummaryJob(
            virtual_window_id=window.id,
            status=SummaryJobStatus.pending,
        )
        session.add(pending_job)

    pending_job.run_after = run_after
    pending_job.trigger_reason = AGENT_IDLE_REASON
    pending_job.input_generation = window.agent_activity_generation
    await session.flush()
    return pending_job


async def _schedule_after_latest_agent_user_message(
    session: AsyncSession,
    window: VirtualWindow,
    event: Event | None = None,
) -> SummaryJob | None:
    user_message_event = _current_user_message_event(window, event)
    if user_message_event is None:
        return None

    latest_user_message_at = _ensure_aware(user_message_event.created_at)
    last_summary_at = await _last_summary_at(session, window.id)
    if last_summary_at is not None and last_summary_at >= latest_user_message_at:
        return None

    settings = get_settings()
    run_after = latest_user_message_at + timedelta(seconds=settings.terminal_summary_idle_seconds)

    pending_job = await _pending_summary_job(session, window.id)
    if pending_job is None:
        pending_job = SummaryJob(
            virtual_window_id=window.id,
            status=SummaryJobStatus.pending,
        )
        session.add(pending_job)

    pending_job.run_after = run_after
    pending_job.trigger_reason = AGENT_IDLE_REASON
    pending_job.input_generation = window.agent_activity_generation
    await session.flush()
    return pending_job


async def _terminal_input_activity(
    session: AsyncSession,
    window: VirtualWindow,
) -> _TerminalInputActivity | None:
    filters = (
        Event.client_id == window.client_id,
        Event.virtual_window_id == window.id,
        Event.kind == TERMINAL_INPUT_COMMAND_KIND,
    )
    total = await session.scalar(select(func.count()).select_from(Event).where(*filters))
    if not total:
        return None

    first_event = await session.scalar(
        select(Event)
        .where(*filters)
        .order_by(Event.created_at, Event.id)
        .limit(1)
    )
    latest_event = await session.scalar(
        select(Event)
        .where(*filters)
        .order_by(desc(Event.created_at), desc(Event.id))
        .limit(1)
    )
    if first_event is None or latest_event is None:
        return None

    return _TerminalInputActivity(
        first_event=first_event,
        latest_event=latest_event,
        total=int(total),
    )


def _current_user_message_event(window: VirtualWindow, event: Event | None) -> Event | None:
    if event is None or not _is_agent_user_message(event):
        return None
    if window.agent_activity_latest_event_id is not None and event.id != window.agent_activity_latest_event_id:
        return None
    return event


def _is_agent_user_message(event: Event) -> bool:
    provider = event.payload_json.get("provider")
    provider_name = provider.strip() if isinstance(provider, str) else None
    try:
        adapter = get_agent_tool_registry().by_source_type(event.source_type, provider_name)
    except (KeyError, ValueError):
        return event.kind in {"user", "user_message"} or _payload_role(event.payload_json) == "user"

    chat = adapter.project_chat(event)
    return chat is not None and chat.role == "user"


def _payload_role(payload: dict) -> str | None:
    role = payload.get("role")
    if isinstance(role, str):
        return role
    message = payload.get("message")
    if isinstance(message, dict):
        message_role = message.get("role")
        if isinstance(message_role, str):
            return message_role
    return None


def _command_agent(event: Event) -> str | None:
    command = event.payload_json.get("command")
    return agent_from_command(command if isinstance(command, str) else None)


def _event_input_time(event: Event) -> datetime:
    captured_at = event.payload_json.get("captured_at")
    if isinstance(captured_at, str):
        try:
            return _ensure_aware(datetime.fromisoformat(captured_at))
        except ValueError:
            pass
    return _ensure_aware(event.created_at)


def _touch_agent_activity_state(window: VirtualWindow, event: Event, activity_at: datetime) -> None:
    current = _ensure_aware(activity_at)
    latest = _ensure_aware(window.agent_activity_latest_at) if window.agent_activity_latest_at else None
    if latest is not None and current < latest:
        if event_is_agent_completion(event):
            _touch_agent_completion_state(window, event)
        return
    if latest is not None and current == latest and event.id == window.agent_activity_latest_event_id:
        return
    gap = timedelta(seconds=SUMMARY_AGENT_BURST_GAP_SECONDS)
    if latest is None or current - latest > gap:
        window.agent_activity_burst_start_at = current
    window.agent_activity_latest_at = current
    if event.id is not None:
        window.agent_activity_latest_event_id = event.id
    if event_is_agent_completion(event):
        _touch_agent_completion_state(window, event)
    window.agent_activity_generation += 1


def _touch_agent_completion_state(window: VirtualWindow, event: Event) -> None:
    completed_at = event_activity_time(event)
    latest_completed = (
        _ensure_aware(window.agent_activity_latest_completed_at)
        if window.agent_activity_latest_completed_at
        else None
    )
    if latest_completed is None or completed_at >= latest_completed:
        window.agent_activity_latest_completed_at = completed_at


def _agent_activity_time_from_event(event: Event | None) -> datetime | None:
    if event is None or event.created_at is None:
        return None
    if event.kind == AGENT_WORK_PRESENCE_KIND:
        return _ensure_aware(event.created_at)
    if event.source_type in agent_activity_source_types():
        return event_activity_time(event)
    if event.kind == TERMINAL_INPUT_COMMAND_KIND and _command_agent(event) is not None:
        return _event_input_time(event)
    return None


async def _was_idle_before(
    session: AsyncSession,
    window_id: UUID,
    moment: datetime,
    *,
    current_event_id: UUID | None = None,
) -> bool:
    window = await session.get(VirtualWindow, window_id)
    if window is None:
        return True
    moment_aware = _ensure_aware(moment)
    activity_times = await _event_activity_times_before(
        session,
        window,
        moment_aware,
        current_event_id=current_event_id,
    )
    if window.terminal_last_output_at is not None:
        terminal_output_at = _ensure_aware(window.terminal_last_output_at)
        if terminal_output_at < moment_aware:
            activity_times.append(terminal_output_at)
    if not activity_times:
        return True
    latest_prior = max(activity_times)
    return moment_aware - latest_prior > timedelta(seconds=SUMMARY_AGENT_BURST_GAP_SECONDS)


async def _event_activity_times_before(
    session: AsyncSession,
    window: VirtualWindow,
    moment: datetime,
    *,
    current_event_id: UUID | None = None,
) -> list[datetime]:
    times: list[datetime] = []
    for kind in (
        TERMINAL_INPUT_COMMAND_KIND,
        TERMINAL_COMMAND_FINISHED_KIND,
        AGENT_WORK_PRESENCE_KIND,
    ):
        filters = [
            Event.client_id == window.client_id,
            Event.virtual_window_id == window.id,
            Event.kind == kind,
            Event.created_at < moment,
        ]
        if current_event_id is not None:
            filters.append(Event.id != current_event_id)
        created_at = await session.scalar(
            select(Event.created_at)
            .where(*filters)
            .order_by(desc(Event.created_at), desc(Event.id))
            .limit(1)
        )
        if created_at is not None:
            times.append(_ensure_aware(created_at))
    times.extend(
        await _agent_activity_times_before(
            session,
            window,
            moment,
            current_event_id=current_event_id,
        )
    )
    return sorted(set(times))


async def _agent_activity_times_before(
    session: AsyncSession,
    window: VirtualWindow,
    moment: datetime,
    *,
    current_event_id: UUID | None = None,
) -> list[datetime]:
    filters = [
        Event.client_id == window.client_id,
        Event.virtual_window_id == window.id,
        Event.source_type.in_(agent_activity_source_types()),
        Event.created_at < moment,
    ]
    if current_event_id is not None:
        filters.append(Event.id != current_event_id)
    created_at = await session.scalar(
        select(Event.created_at)
        .where(*filters)
        .order_by(desc(Event.created_at), desc(Event.id))
        .limit(1)
    )
    return [_ensure_aware(created_at)] if created_at is not None else []


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
