from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, desc, func, literal_column, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import AiSession, Event, EventSourceType, VirtualWindow
from app.schemas import WorkStatusOut
from app.services.agent_activity_projection import event_activity_time, event_is_agent_completion
from app.services.window_runtime_tags import agent_from_command

AGENT_ABORT_IDLE_SECONDS = 60 * 60
AGENT_EVENT_LATE_ARRIVAL_SECONDS = 30
RECENT_ACTIVE_WINDOW_SECONDS = 10 * 60

TERMINAL_ACTIVITY_KINDS = (
    "terminal_input_command",
    "terminal_command_finished",
)
TERMINAL_COMMAND_KIND = "terminal_input_command"
TERMINAL_COMMAND_FINISHED_KIND = "terminal_command_finished"
AGENT_EVENT_SCAN_LIMIT_PER_WINDOW = 200
AGENT_ACTIVITY_SOURCE_TYPES = (
    EventSourceType.agent_tool_record.value,
    EventSourceType.codex_trace.value,
    EventSourceType.claude_jsonl.value,
)

FINISHED_AGENT_TASK_STATUS = "FINISHED"
ABORTED_AGENT_TASK_STATUS = "ABORTED"


@dataclass(frozen=True)
class TerminalWorkStatus:
    state: str
    label: str
    color: str
    last_activity_at: datetime | None = None
    last_working_activity_at: datetime | None = None


@dataclass(frozen=True)
class AgentTaskStatus:
    state: str
    occurred_at: datetime


@dataclass(frozen=True)
class TreeWindowActivity:
    work_statuses: dict[UUID, TerminalWorkStatus]
    last_agent_task_completed_at: dict[UUID, datetime]
    last_agent_task_status: dict[UUID, AgentTaskStatus]
    latest_ai_sessions: dict[UUID, AiSession]
    latest_terminal_agents: dict[UUID, str]


@dataclass(frozen=True)
class _WindowActivityData:
    latest_activity: dict[UUID, datetime]
    latest_terminal_activity: dict[UUID, datetime]
    latest_working_activity: dict[UUID, datetime]
    latest_agent_active_at: dict[UUID, datetime]
    latest_agent_completed_at: dict[UUID, datetime]
    latest_commands: dict[UUID, Event]
    finished_sequences: dict[UUID, dict[str, datetime]]

    def work_statuses(self, window_ids: list[UUID], *, now: datetime | None) -> dict[UUID, TerminalWorkStatus]:
        return {
            window_id: work_status_from_activity(
                now=now,
                last_activity_at=self.latest_activity.get(window_id),
                last_terminal_activity_at=self.latest_terminal_activity.get(window_id),
                last_agent_active_at=self.latest_agent_active_at.get(window_id),
                last_agent_output_at=self.latest_working_activity.get(window_id),
                last_agent_completed_at=self.latest_agent_completed_at.get(window_id),
            )
            for window_id in window_ids
        }

    def latest_terminal_agents(self) -> dict[UUID, str]:
        latest: dict[UUID, str] = {}
        for window_id, event in self.latest_commands.items():
            agent = _event_agent(event)
            if agent is not None:
                latest[window_id] = agent
        return latest


@dataclass(frozen=True)
class _WindowAgentActivityState:
    latest_activity: dict[UUID, datetime]
    latest_completed_at: dict[UUID, datetime]


def to_work_status_out(status: TerminalWorkStatus) -> WorkStatusOut:
    return WorkStatusOut(
        state=status.state,
        label=status.label,
        color=status.color,
        last_activity_at=status.last_activity_at,
        last_working_activity_at=status.last_working_activity_at,
    )


def long_idle_work_status(
    *,
    last_activity_at: datetime | None = None,
    last_working_activity_at: datetime | None = None,
) -> TerminalWorkStatus:
    return TerminalWorkStatus(
        state="LONG_IDLE",
        label="长时间没有工作了",
        color="gray",
        last_activity_at=last_activity_at,
        last_working_activity_at=last_working_activity_at,
    )


def work_status_from_activity(
    *,
    now: datetime | None = None,
    last_activity_at: datetime | None,
    last_working_activity_at: datetime | None = None,
    last_terminal_activity_at: datetime | None = None,
    last_agent_active_at: datetime | None = None,
    last_agent_started_at: datetime | None = None,
    last_agent_output_at: datetime | None = None,
    last_agent_completed_at: datetime | None = None,
) -> TerminalWorkStatus:
    current = _aware_utc(now or datetime.now(UTC))
    last_activity = _aware_utc(last_activity_at) if last_activity_at is not None else None
    last_terminal_activity = (
        _aware_utc(last_terminal_activity_at) if last_terminal_activity_at is not None else None
    )
    raw_agent_active_at = last_agent_active_at or last_agent_started_at
    last_agent_active = _aware_utc(raw_agent_active_at) if raw_agent_active_at is not None else None
    last_agent_output = _aware_utc(last_agent_output_at) if last_agent_output_at is not None else None
    last_agent_completed = (
        _aware_utc(last_agent_completed_at) if last_agent_completed_at is not None else None
    )
    if last_agent_output is not None:
        last_working_activity = last_agent_output
    elif last_working_activity_at is not None:
        last_working_activity = _aware_utc(last_working_activity_at)
    else:
        last_working_activity = None

    active_marker_running = (
        last_agent_active is not None
        and (last_agent_completed is None or last_agent_completed < last_agent_active)
    )
    recent_unmanaged_output = (
        last_agent_active is None
        and last_agent_output is not None
        and (last_agent_completed is None or last_agent_completed < last_agent_output)
        and (last_terminal_activity is None or last_terminal_activity < last_agent_output)
        and current - last_agent_output <= timedelta(seconds=RECENT_ACTIVE_WINDOW_SECONDS)
    )
    agent_active = active_marker_running or recent_unmanaged_output
    if agent_active:
        abort_reference = last_agent_output or last_agent_active
        if abort_reference is not None:
            abort_at = abort_reference + timedelta(seconds=AGENT_ABORT_IDLE_SECONDS)
            if current >= abort_at:
                if current - abort_at <= timedelta(seconds=RECENT_ACTIVE_WINDOW_SECONDS):
                    return TerminalWorkStatus(
                        state="ABORTED",
                        label="Agent 可能已中断",
                        color="red",
                        last_activity_at=last_activity,
                        last_working_activity_at=last_working_activity,
                    )
                agent_active = False
    if agent_active and last_agent_output is not None:
        return TerminalWorkStatus(
            state="WORKING",
            label="Agent 工作中",
            color="orange",
            last_activity_at=last_activity,
            last_working_activity_at=last_working_activity,
        )

    if (
        last_agent_completed is not None
        and (last_agent_active is None or last_agent_completed >= last_agent_active)
        and current - last_agent_completed <= timedelta(seconds=RECENT_ACTIVE_WINDOW_SECONDS)
    ):
        return TerminalWorkStatus(
            state="FINISHED",
            label="Agent 已完成",
            color="green",
            last_activity_at=last_activity,
            last_working_activity_at=last_working_activity,
        )

    if last_activity is not None and current - last_activity <= timedelta(seconds=RECENT_ACTIVE_WINDOW_SECONDS):
        return TerminalWorkStatus(
            state="RECENT_ACTIVE",
            label="Terminal 活跃",
            color="green",
            last_activity_at=last_activity,
            last_working_activity_at=last_working_activity,
        )

    return long_idle_work_status(
        last_activity_at=last_activity,
        last_working_activity_at=last_working_activity,
    )


async def load_tree_window_activity(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    now: datetime | None = None,
    include_runtime_tags: bool = True,
) -> TreeWindowActivity:
    if not window_ids:
        return TreeWindowActivity({}, {}, {}, {}, {})

    activity, latest_ai_sessions = await _load_tree_activity_bundle(
        session,
        client_id,
        window_ids,
        now=now,
        include_runtime_tags=include_runtime_tags,
    )
    work_statuses = activity.work_statuses(window_ids, now=now)
    return TreeWindowActivity(
        work_statuses=work_statuses,
        last_agent_task_completed_at=_last_agent_task_completed_at_from_activity(
            window_ids,
            activity=activity,
        ),
        last_agent_task_status=_last_agent_task_status_from_activity(
            window_ids,
            activity=activity,
            now=now,
        ),
        latest_ai_sessions=latest_ai_sessions,
        latest_terminal_agents=activity.latest_terminal_agents(),
    )


async def load_work_statuses(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    now: datetime | None = None,
) -> dict[UUID, TerminalWorkStatus]:
    if not window_ids:
        return {}

    activity = await _load_window_activity_data(session, client_id, window_ids, now=now)
    return activity.work_statuses(window_ids, now=now)


async def load_work_status(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    *,
    now: datetime | None = None,
) -> TerminalWorkStatus:
    statuses = await load_work_statuses(session, client_id, [window_id], now=now)
    return statuses.get(window_id, long_idle_work_status())


async def load_last_agent_task_completed_at_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    now: datetime | None = None,
) -> dict[UUID, datetime]:
    if not window_ids:
        return {}

    activity = await _load_window_activity_data(session, client_id, window_ids, now=now)
    return _last_agent_task_completed_at_from_activity(
        window_ids,
        activity=activity,
    )


async def _load_tree_activity_bundle(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    now: datetime | None = None,
    include_runtime_tags: bool = True,
) -> tuple[_WindowActivityData, dict[UUID, AiSession]]:
    activity = await _load_window_activity_data(session, client_id, window_ids, now=now)
    if include_runtime_tags:
        latest_ai_sessions = await _latest_ai_sessions_by_window(session, client_id, window_ids)
    else:
        latest_ai_sessions = {}
    return activity, latest_ai_sessions


async def _load_window_activity_data(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    now: datetime | None = None,
) -> _WindowActivityData:
    current = _aware_utc(now or datetime.now(UTC))
    agent_activity = await _agent_activity_state_by_window(session, client_id, window_ids)
    latest_commands = await _latest_events_by_window(
        session,
        client_id,
        window_ids,
        kind=TERMINAL_COMMAND_KIND,
    )
    latest_terminal_events = await _latest_created_at_by_window_and_kinds(
        session,
        client_id,
        window_ids,
        kinds=TERMINAL_ACTIVITY_KINDS,
    )
    latest_terminal_output = await _latest_terminal_output_activity_by_window(
        session,
        client_id,
        window_ids,
    )
    agent_command_windows = [
        window_id
        for window_id, event in latest_commands.items()
        if _event_agent(event) is not None
    ]
    finished_sequences = await _finished_command_sequences_by_window(
        session,
        client_id,
        agent_command_windows,
        latest_commands=latest_commands,
    )
    latest_working_activity = _merge_latest_working_activity(
        window_ids,
        latest_commands=latest_commands,
        latest_ai=agent_activity.latest_activity,
        finished_sequences=finished_sequences,
        now=current,
    )
    latest_agent_active_at = _latest_agent_active_at(
        window_ids,
        latest_commands=latest_commands,
        latest_ai=agent_activity.latest_activity,
        finished_sequences=finished_sequences,
    )
    latest_agent_completed_at = agent_activity.latest_completed_at
    latest_terminal_activity = _merge_latest_created_at(latest_terminal_events, latest_terminal_output)
    latest_activity = _merge_latest_created_at(
        latest_terminal_activity,
        latest_working_activity,
        latest_agent_completed_at,
    )

    return _WindowActivityData(
        latest_activity=latest_activity,
        latest_terminal_activity=latest_terminal_activity,
        latest_working_activity=latest_working_activity,
        latest_agent_active_at=latest_agent_active_at,
        latest_agent_completed_at=latest_agent_completed_at,
        latest_commands=latest_commands,
        finished_sequences=finished_sequences,
    )


def _last_agent_task_completed_at_from_activity(
    window_ids: list[UUID],
    *,
    activity: _WindowActivityData,
) -> dict[UUID, datetime]:
    latest: dict[UUID, datetime] = {}
    for window_id in window_ids:
        completed_at = activity.latest_agent_completed_at.get(window_id)
        if completed_at is None:
            continue
        active_at = activity.latest_agent_active_at.get(window_id)
        if active_at is not None and _aware_utc(active_at) > _aware_utc(completed_at):
            continue
        latest[window_id] = completed_at
    return latest


def _last_agent_task_status_from_activity(
    window_ids: list[UUID],
    *,
    activity: _WindowActivityData,
    now: datetime | None,
) -> dict[UUID, AgentTaskStatus]:
    current = _aware_utc(now or datetime.now(UTC))
    latest: dict[UUID, AgentTaskStatus] = {}
    for window_id in window_ids:
        completed_at = activity.latest_agent_completed_at.get(window_id)
        active_at = activity.latest_agent_active_at.get(window_id)
        if active_at is not None and (completed_at is None or _aware_utc(active_at) > _aware_utc(completed_at)):
            abort_reference = activity.latest_working_activity.get(window_id) or active_at
            abort_at = _aware_utc(abort_reference) + timedelta(seconds=AGENT_ABORT_IDLE_SECONDS)
            if current >= abort_at:
                latest[window_id] = AgentTaskStatus(
                    state=ABORTED_AGENT_TASK_STATUS,
                    occurred_at=abort_at,
                )
            continue

        if completed_at is not None:
            latest[window_id] = AgentTaskStatus(
                state=FINISHED_AGENT_TASK_STATUS,
                occurred_at=_aware_utc(completed_at),
            )
    return latest


async def _recent_agent_events_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
) -> dict[UUID, list[Event]]:
    if not window_ids:
        return {}

    if _dialect_name(session) == "postgresql":
        return await _recent_agent_events_by_window_postgresql(session, client_id, window_ids)
    return await _recent_agent_events_by_window_ranked(session, client_id, window_ids)


async def _recent_agent_events_by_window_postgresql(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
) -> dict[UUID, list[Event]]:
    outer_window = aliased(VirtualWindow)
    recent_events_lateral = (
        select(Event)
        .where(
            Event.client_id == client_id,
            Event.virtual_window_id == outer_window.id,
            _agent_activity_source_type_filter(Event),
        )
        .order_by(desc(Event.created_at), desc(Event.id))
        .limit(AGENT_EVENT_SCAN_LIMIT_PER_WINDOW)
        .lateral("recent_agent_events")
    )
    recent_event = aliased(Event, recent_events_lateral)
    rows = list(
        await session.scalars(
            select(recent_event)
            .select_from(outer_window)
            .join(recent_event, true())
            .where(
                outer_window.client_id == client_id,
                outer_window.id.in_(window_ids),
            )
            .order_by(
                outer_window.id,
                desc(recent_event.created_at),
                desc(recent_event.id),
            )
        )
    )
    return _group_events_by_window(rows)


async def _recent_agent_events_by_window_ranked(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
) -> dict[UUID, list[Event]]:
    rank = (
        func.row_number()
        .over(
            partition_by=Event.virtual_window_id,
            order_by=(desc(Event.created_at), desc(Event.id)),
        )
        .label("event_rank")
    )
    ranked_events = (
        select(Event.id.label("event_id"), rank)
        .where(
            Event.client_id == client_id,
            Event.virtual_window_id.in_(window_ids),
            _agent_activity_source_type_filter(Event),
        )
        .subquery()
    )
    rows = list(
        await session.scalars(
            select(Event)
            .join(ranked_events, Event.id == ranked_events.c.event_id)
            .where(ranked_events.c.event_rank <= AGENT_EVENT_SCAN_LIMIT_PER_WINDOW)
            .order_by(Event.virtual_window_id, ranked_events.c.event_rank)
        )
    )
    return _group_events_by_window(rows)


async def _agent_activity_state_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
) -> _WindowAgentActivityState:
    if not window_ids:
        return _WindowAgentActivityState({}, {})

    rows = await session.execute(
        select(
            VirtualWindow.id,
            VirtualWindow.agent_activity_latest_at,
            VirtualWindow.agent_activity_latest_completed_at,
        ).where(
            VirtualWindow.client_id == client_id,
            VirtualWindow.id.in_(window_ids),
        )
    )
    latest_activity: dict[UUID, datetime] = {}
    latest_completed_at: dict[UUID, datetime] = {}
    missing_window_ids: list[UUID] = []
    for window_id, activity_at, completed_at in rows:
        if activity_at is None:
            missing_window_ids.append(window_id)
        else:
            latest_activity[window_id] = _aware_utc(activity_at)
        if completed_at is not None:
            latest_completed_at[window_id] = _aware_utc(completed_at)

    if _dialect_name(session) == "postgresql":
        return _WindowAgentActivityState(latest_activity, latest_completed_at)

    fallback_events = await _recent_agent_events_by_window(session, client_id, missing_window_ids)
    latest_activity.update(_latest_ai_activity_by_window(fallback_events))
    latest_completed_at.update(_latest_agent_completed_at_by_window(fallback_events))
    return _WindowAgentActivityState(latest_activity, latest_completed_at)


def _group_events_by_window(rows: list[Event]) -> dict[UUID, list[Event]]:
    latest: dict[UUID, list[Event]] = {}
    for event in rows:
        if event.virtual_window_id is not None:
            latest.setdefault(event.virtual_window_id, []).append(event)
    return latest


def _agent_activity_source_type_filter(event_model=Event):
    return event_model.source_type.in_(
        tuple(literal_column(f"'{value}'") for value in AGENT_ACTIVITY_SOURCE_TYPES)
    )


def _latest_agent_completed_at_by_window(
    recent_agent_events: dict[UUID, list[Event]],
) -> dict[UUID, datetime]:
    latest: dict[UUID, datetime] = {}
    for window_id, events in recent_agent_events.items():
        for event in events:
            if event_is_agent_completion(event):
                completed_at = event_activity_time(event)
                if window_id not in latest or completed_at > latest[window_id]:
                    latest[window_id] = completed_at
    return latest


def _latest_ai_activity_by_window(
    recent_agent_events: dict[UUID, list[Event]],
) -> dict[UUID, datetime]:
    latest: dict[UUID, datetime] = {}
    for window_id, events in recent_agent_events.items():
        for event in events:
            activity_at = event_activity_time(event)
            if window_id not in latest or activity_at > latest[window_id]:
                latest[window_id] = activity_at
    return latest


async def _latest_events_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    kind: str,
) -> dict[UUID, Event]:
    if not window_ids:
        return {}

    if _dialect_name(session) == "postgresql":
        return await _latest_events_by_window_postgresql(
            session,
            client_id,
            window_ids,
            kind=kind,
        )

    latest_created_at = (
        select(
            Event.virtual_window_id.label("window_id"),
            func.max(Event.created_at).label("max_created_at"),
        )
        .where(
            Event.client_id == client_id,
            Event.virtual_window_id.in_(window_ids),
            Event.kind == kind,
        )
        .group_by(Event.virtual_window_id)
        .subquery()
    )

    rows = list(
        await session.scalars(
            select(Event)
            .join(
                latest_created_at,
                and_(
                    Event.virtual_window_id == latest_created_at.c.window_id,
                    Event.created_at == latest_created_at.c.max_created_at,
                ),
            )
            .where(
                Event.client_id == client_id,
                Event.virtual_window_id.in_(window_ids),
                Event.kind == kind,
            )
            .order_by(Event.virtual_window_id, desc(Event.id))
        )
    )
    latest: dict[UUID, Event] = {}
    for event in rows:
        if event.virtual_window_id is not None and event.virtual_window_id not in latest:
            latest[event.virtual_window_id] = event
    return latest


async def _latest_events_by_window_postgresql(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    kind: str,
) -> dict[UUID, Event]:
    outer_window = aliased(VirtualWindow)
    latest_events_lateral = (
        select(Event)
        .where(
            Event.client_id == client_id,
            Event.virtual_window_id == outer_window.id,
            Event.kind == kind,
        )
        .order_by(desc(Event.created_at), desc(Event.id))
        .limit(1)
        .lateral("latest_window_event")
    )
    latest_event = aliased(Event, latest_events_lateral)
    rows = list(
        await session.scalars(
            select(latest_event)
            .select_from(outer_window)
            .join(latest_event, true())
            .where(
                outer_window.client_id == client_id,
                outer_window.id.in_(window_ids),
            )
        )
    )
    return {
        event.virtual_window_id: event
        for event in rows
        if event.virtual_window_id is not None
    }


def _merge_latest_working_activity(
    window_ids: list[UUID],
    *,
    latest_commands: dict[UUID, Event],
    latest_ai: dict[UUID, datetime],
    finished_sequences: dict[UUID, dict[str, datetime]],
    now: datetime,
) -> dict[UUID, datetime]:
    latest_work: dict[UUID, datetime] = {}
    for window_id in window_ids:
        candidates: list[datetime] = []
        if window_id in latest_ai:
            latest_ai_at = _aware_utc(latest_ai[window_id])
            if _agent_command_allows_ai_activity(
                window_id,
                latest_ai_at=latest_ai_at,
                latest_commands=latest_commands,
                finished_sequences=finished_sequences,
                now=now,
            ):
                candidates.append(latest_ai_at)
        if candidates:
            latest_work[window_id] = max(_aware_utc(value) for value in candidates)
    return latest_work


def _latest_agent_active_at(
    window_ids: list[UUID],
    *,
    latest_commands: dict[UUID, Event],
    latest_ai: dict[UUID, datetime],
    finished_sequences: dict[UUID, dict[str, datetime]],
) -> dict[UUID, datetime]:
    latest: dict[UUID, datetime] = {}
    for window_id in window_ids:
        command = latest_commands.get(window_id)
        if command is not None:
            if _event_agent(command) is None:
                continue
            sequence = command.payload_json.get("sequence")
            if sequence is None or str(sequence) not in finished_sequences.get(window_id, {}):
                candidates = [_aware_utc(command.created_at)]
                if window_id in latest_ai:
                    candidates.append(_aware_utc(latest_ai[window_id]))
                latest[window_id] = max(candidates)
            continue
        if window_id in latest_ai:
            latest[window_id] = _aware_utc(latest_ai[window_id])
    return latest


def _agent_command_allows_ai_activity(
    window_id: UUID,
    *,
    latest_ai_at: datetime,
    latest_commands: dict[UUID, Event],
    finished_sequences: dict[UUID, dict[str, datetime]],
    now: datetime,
) -> bool:
    command = latest_commands.get(window_id)
    if command is None:
        return True
    if _event_agent(command) is None:
        return False
    sequence = command.payload_json.get("sequence")
    if sequence is None:
        return True
    finished_at = finished_sequences.get(window_id, {}).get(str(sequence))
    if finished_at is None:
        return True
    aware_finished_at = _aware_utc(finished_at)
    if latest_ai_at < aware_finished_at:
        return False
    return now - aware_finished_at <= timedelta(seconds=AGENT_EVENT_LATE_ARRIVAL_SECONDS)


async def _finished_command_sequences_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    latest_commands: dict[UUID, Event],
) -> dict[UUID, dict[str, datetime]]:
    if not window_ids:
        return {}

    command_refs: dict[UUID, tuple[str, datetime]] = {}
    for window_id in window_ids:
        command = latest_commands.get(window_id)
        if command is None:
            continue
        sequence = command.payload_json.get("sequence")
        if sequence is None:
            continue
        command_refs[window_id] = (str(sequence), _aware_utc(command.created_at))
    if not command_refs:
        return {}

    earliest_command_at = min(created_at for _sequence, created_at in command_refs.values())
    rows = await session.execute(
        select(Event.virtual_window_id, Event.payload_json, Event.created_at).where(
            Event.client_id == client_id,
            Event.virtual_window_id.in_(tuple(command_refs)),
            Event.kind == TERMINAL_COMMAND_FINISHED_KIND,
            Event.created_at >= earliest_command_at,
        )
    )
    sequences: dict[UUID, dict[str, datetime]] = {}
    for window_id, payload, created_at in rows:
        if window_id is None:
            continue
        sequence = payload.get("sequence")
        if sequence is None:
            continue
        key = str(sequence)
        expected = command_refs.get(window_id)
        if expected is None:
            continue
        expected_sequence, command_created_at = expected
        if key != expected_sequence or _aware_utc(created_at) < command_created_at:
            continue
        current = sequences.setdefault(window_id, {}).get(key)
        if current is None or created_at > current:
            sequences.setdefault(window_id, {})[key] = created_at
    return sequences


async def _latest_terminal_output_activity_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
) -> dict[UUID, datetime]:
    if not window_ids:
        return {}

    rows = await session.execute(
        select(VirtualWindow.id, VirtualWindow.terminal_last_output_at).where(
            VirtualWindow.client_id == client_id,
            VirtualWindow.id.in_(window_ids),
            VirtualWindow.terminal_last_output_at.is_not(None),
        )
    )
    return {window_id: activity_at for window_id, activity_at in rows if activity_at is not None}


async def _latest_created_at_by_window_and_kinds(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    kinds: tuple[str, ...],
) -> dict[UUID, datetime]:
    return await _latest_created_at_by_window_for_event_values(
        session,
        client_id,
        window_ids,
        value_column=Event.kind,
        values=kinds,
    )


async def _latest_created_at_by_window_for_event_values(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    value_column,
    values: tuple,
) -> dict[UUID, datetime]:
    if not window_ids or not values:
        return {}

    columns = [VirtualWindow.id]
    for index, value in enumerate(values):
        latest_created_at = (
            select(Event.created_at)
            .where(
                Event.client_id == client_id,
                Event.virtual_window_id == VirtualWindow.id,
                value_column == value,
            )
            .order_by(desc(Event.created_at))
            .limit(1)
            .scalar_subquery()
            .label(f"latest_created_at_{index}")
        )
        columns.append(latest_created_at)

    rows = await session.execute(
        select(*columns).where(
            VirtualWindow.client_id == client_id,
            VirtualWindow.id.in_(window_ids),
        )
    )
    latest: dict[UUID, datetime] = {}
    for row in rows:
        window_id = row[0]
        candidates = [created_at for created_at in row[1:] if created_at is not None]
        if candidates:
            latest[window_id] = max(candidates)
    return latest


def _merge_latest_created_at(*items: dict[UUID, datetime]) -> dict[UUID, datetime]:
    latest: dict[UUID, datetime] = {}
    for item in items:
        for window_id, created_at in item.items():
            aware_created_at = _aware_utc(created_at)
            if window_id not in latest or aware_created_at > latest[window_id]:
                latest[window_id] = aware_created_at
    return latest


async def _latest_ai_sessions_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
) -> dict[UUID, AiSession]:
    if not window_ids:
        return {}

    latest_updated_at = (
        select(
            AiSession.virtual_window_id.label("window_id"),
            func.max(AiSession.updated_at).label("max_updated_at"),
        )
        .where(
            AiSession.client_id == client_id,
            AiSession.virtual_window_id.in_(window_ids),
        )
        .group_by(AiSession.virtual_window_id)
        .subquery()
    )

    rows = list(
        await session.scalars(
            select(AiSession)
            .join(
                latest_updated_at,
                and_(
                    AiSession.virtual_window_id == latest_updated_at.c.window_id,
                    AiSession.updated_at == latest_updated_at.c.max_updated_at,
                ),
            )
            .where(
                AiSession.client_id == client_id,
                AiSession.virtual_window_id.in_(window_ids),
            )
            .order_by(AiSession.virtual_window_id, desc(AiSession.created_at))
        )
    )
    latest_by_window: dict[UUID, AiSession] = {}
    for ai_session in rows:
        if (
            ai_session.virtual_window_id is not None
            and ai_session.virtual_window_id not in latest_by_window
        ):
            latest_by_window[ai_session.virtual_window_id] = ai_session
    return latest_by_window


def _event_agent(event: Event | None) -> str | None:
    if event is None:
        return None
    command = event.payload_json.get("command")
    return agent_from_command(command if isinstance(command, str) else None)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _dialect_name(session: AsyncSession) -> str:
    return session.get_bind().dialect.name
