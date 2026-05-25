from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_tools import agent_activity_source_types
from app.models import AiSession, Event
from app.schemas import WorkStatusOut
from app.services.event_kinds import AGENT_WORK_PRESENCE_KIND
from app.services.window_runtime_tags import agent_from_command

WORKING_WINDOW_SECONDS = 60
RECENT_ACTIVE_WINDOW_SECONDS = 5 * 60

TERMINAL_ACTIVITY_KINDS = (
    "terminal_input_command",
    "terminal_command_finished",
    "terminal_output",
    AGENT_WORK_PRESENCE_KIND,
)
TERMINAL_COMMAND_KIND = "terminal_input_command"
TERMINAL_COMMAND_FINISHED_KIND = "terminal_command_finished"
TERMINAL_OUTPUT_KIND = "terminal_output"


@dataclass(frozen=True)
class TerminalWorkStatus:
    state: str
    label: str
    color: str
    last_activity_at: datetime | None = None
    last_working_activity_at: datetime | None = None


@dataclass(frozen=True)
class TreeWindowActivity:
    work_statuses: dict[UUID, TerminalWorkStatus]
    last_agent_task_completed_at: dict[UUID, datetime]
    latest_ai_sessions: dict[UUID, AiSession]
    latest_terminal_agents: dict[UUID, str]


@dataclass(frozen=True)
class _WindowActivityData:
    latest_activity: dict[UUID, datetime]
    latest_working_activity: dict[UUID, datetime]
    latest_ai_activity: dict[UUID, datetime]
    latest_presence: dict[UUID, datetime]
    latest_commands: dict[UUID, Event]
    finished_sequences: dict[UUID, set[str]]
    latest_agent_finished_at: dict[UUID, datetime]

    @property
    def agent_commands_in_progress(self) -> set[UUID]:
        in_progress: set[UUID] = set()
        for window_id, event in self.latest_commands.items():
            if _event_agent(event) is None:
                continue
            sequence = event.payload_json.get("sequence")
            if sequence is None:
                continue
            if str(sequence) not in self.finished_sequences.get(window_id, set()):
                in_progress.add(window_id)
        return in_progress

    def work_statuses(self, window_ids: list[UUID], *, now: datetime | None) -> dict[UUID, TerminalWorkStatus]:
        in_progress = self.agent_commands_in_progress
        return {
            window_id: work_status_from_activity(
                now=now,
                last_activity_at=self.latest_activity.get(window_id),
                last_working_activity_at=self._working_activity_at_for_status(
                    window_id,
                    in_progress=in_progress,
                ),
                agent_command_in_progress=window_id in in_progress,
            )
            for window_id in window_ids
        }

    def _working_activity_at_for_status(
        self,
        window_id: UUID,
        *,
        in_progress: set[UUID],
    ) -> datetime | None:
        command = self.latest_commands.get(window_id)
        candidates: list[datetime] = []
        if command is not None and _event_agent(command) is not None:
            sequence = command.payload_json.get("sequence")
            if sequence is None or str(sequence) not in self.finished_sequences.get(window_id, set()):
                candidates.append(command.created_at)
        if window_id in self.latest_ai_activity:
            candidates.append(self.latest_ai_activity[window_id])
        if window_id in in_progress and window_id in self.latest_presence:
            candidates.append(self.latest_presence[window_id])
        if not candidates:
            return None
        return max(_aware_utc(value) for value in candidates)

    def latest_terminal_agents(self) -> dict[UUID, str]:
        latest: dict[UUID, str] = {}
        for window_id, event in self.latest_commands.items():
            agent = _event_agent(event)
            if agent is not None:
                latest[window_id] = agent
        return latest


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
    last_working_activity_at: datetime | None,
    agent_command_in_progress: bool = False,
) -> TerminalWorkStatus:
    current = _aware_utc(now or datetime.now(UTC))
    last_activity = _aware_utc(last_activity_at) if last_activity_at is not None else None
    last_working_activity = (
        _aware_utc(last_working_activity_at) if last_working_activity_at is not None else None
    )

    if agent_command_in_progress or (
        last_working_activity is not None
        and current - last_working_activity <= timedelta(seconds=WORKING_WINDOW_SECONDS)
    ):
        return TerminalWorkStatus(
            state="WORKING",
            label="正在工作中",
            color="orange",
            last_activity_at=last_activity,
            last_working_activity_at=last_working_activity,
        )

    if (
        last_activity is not None
        and current - last_activity <= timedelta(seconds=RECENT_ACTIVE_WINDOW_SECONDS)
    ):
        return TerminalWorkStatus(
            state="RECENT_ACTIVE",
            label="最近刚活跃过",
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
        return TreeWindowActivity({}, {}, {}, {})

    activity, latest_ai_sessions = await _load_tree_activity_bundle(
        session,
        client_id,
        window_ids,
        include_runtime_tags=include_runtime_tags,
    )
    work_statuses = activity.work_statuses(window_ids, now=now)
    return TreeWindowActivity(
        work_statuses=work_statuses,
        last_agent_task_completed_at=_last_agent_task_completed_at_from_activity(
            window_ids,
            activity=activity,
            work_statuses=work_statuses,
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

    activity = await _load_window_activity_data(session, client_id, window_ids)
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

    activity = await _load_window_activity_data(session, client_id, window_ids)
    work_statuses = activity.work_statuses(window_ids, now=now)
    return _last_agent_task_completed_at_from_activity(
        window_ids,
        activity=activity,
        work_statuses=work_statuses,
        now=now,
    )


async def _load_tree_activity_bundle(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    include_runtime_tags: bool = True,
) -> tuple[_WindowActivityData, dict[UUID, AiSession]]:
    activity = await _load_window_activity_data(session, client_id, window_ids)
    if include_runtime_tags:
        latest_ai_sessions = await _latest_ai_sessions_by_window(session, client_id, window_ids)
    else:
        latest_ai_sessions = {}
    return activity, latest_ai_sessions


async def _load_window_activity_data(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
) -> _WindowActivityData:
    latest_commands = await _latest_events_by_window(
        session,
        client_id,
        window_ids,
        kind=TERMINAL_COMMAND_KIND,
    )
    latest_activity = await _latest_activity_by_window(session, client_id, window_ids)
    latest_ai = await _latest_ai_activity_by_window(session, client_id, window_ids)
    latest_presence = await _latest_agent_work_presence_by_window(session, client_id, window_ids)
    agent_command_windows = [
        window_id
        for window_id, event in latest_commands.items()
        if _event_agent(event) is not None
    ]
    finished_sequences = await _finished_command_sequences_by_window(
        session,
        client_id,
        agent_command_windows,
    )
    latest_working_activity = _merge_latest_working_activity(
        window_ids,
        latest_commands=latest_commands,
        latest_ai=latest_ai,
        latest_presence=latest_presence,
        finished_sequences=finished_sequences,
    )

    latest_agent_finished_at = await _latest_agent_command_finished_at_by_window(
        session,
        client_id,
        window_ids,
        latest_commands,
    )

    return _WindowActivityData(
        latest_activity=latest_activity,
        latest_working_activity=latest_working_activity,
        latest_ai_activity=latest_ai,
        latest_presence=latest_presence,
        latest_commands=latest_commands,
        finished_sequences=finished_sequences,
        latest_agent_finished_at=latest_agent_finished_at,
    )


def _last_agent_task_completed_at_from_activity(
    window_ids: list[UUID],
    *,
    activity: _WindowActivityData,
    work_statuses: dict[UUID, TerminalWorkStatus],
    now: datetime | None,
) -> dict[UUID, datetime]:
    latest_from_finished = _latest_agent_command_finished_at(activity)
    latest_from_idle = _latest_agent_task_completed_from_idle_work(
        window_ids,
        activity=activity,
        work_statuses=work_statuses,
        now=now,
    )

    latest: dict[UUID, datetime] = {}
    for window_id in window_ids:
        finished_at = latest_from_finished.get(window_id)
        if finished_at is not None:
            latest[window_id] = _aware_utc(finished_at)
            continue
        idle_at = latest_from_idle.get(window_id)
        if idle_at is not None:
            latest[window_id] = _aware_utc(idle_at)
    return latest


def _latest_agent_command_finished_at(activity: _WindowActivityData) -> dict[UUID, datetime]:
    return activity.latest_agent_finished_at


def _latest_agent_task_completed_from_idle_work(
    window_ids: list[UUID],
    *,
    activity: _WindowActivityData,
    work_statuses: dict[UUID, TerminalWorkStatus],
    now: datetime | None,
) -> dict[UUID, datetime]:
    del now
    in_progress = activity.agent_commands_in_progress
    latest: dict[UUID, datetime] = {}
    for window_id in window_ids:
        if window_id in in_progress:
            continue
        working_at = activity.latest_working_activity.get(window_id)
        if working_at is None:
            continue
        status = work_statuses.get(window_id, long_idle_work_status())
        if status.state == "WORKING":
            continue
        latest[window_id] = _aware_utc(working_at) + timedelta(seconds=WORKING_WINDOW_SECONDS)
    return latest


async def _latest_agent_command_finished_at_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    latest_commands: dict[UUID, Event],
) -> dict[UUID, datetime]:
    if not window_ids:
        return {}

    rows = list(
        await session.scalars(
            select(Event)
            .where(
                Event.client_id == client_id,
                Event.virtual_window_id.in_(window_ids),
                Event.kind == TERMINAL_COMMAND_FINISHED_KIND,
            )
            .order_by(Event.virtual_window_id, desc(Event.created_at), desc(Event.id))
        )
    )

    latest: dict[UUID, datetime] = {}
    deferred: list[Event] = []
    for event in rows:
        window_id = event.virtual_window_id
        if window_id is None or window_id in latest:
            continue
        if _event_agent(event) is not None:
            latest[window_id] = event.created_at
            continue
        sequence = event.payload_json.get("sequence")
        if sequence is None:
            continue
        deferred.append(event)

    if not deferred:
        return latest

    sequence_pairs = [
        (event.virtual_window_id, str(event.payload_json["sequence"]))
        for event in deferred
        if event.virtual_window_id is not None and event.payload_json.get("sequence") is not None
    ]
    input_by_window_sequence = await _input_commands_for_window_sequences(
        session,
        client_id,
        sequence_pairs,
        latest_commands,
    )
    for event in deferred:
        window_id = event.virtual_window_id
        if window_id is None or window_id in latest:
            continue
        if _finished_event_is_agent(event, input_by_window_sequence):
            latest[window_id] = event.created_at
    return latest


async def _input_commands_for_window_sequences(
    session: AsyncSession,
    client_id: UUID,
    sequence_pairs: list[tuple[UUID, str]],
    latest_commands: dict[UUID, Event],
) -> dict[tuple[UUID, str], Event]:
    if not sequence_pairs:
        return {}

    pair_set = set(sequence_pairs)
    indexed: dict[tuple[UUID, str], Event] = {}
    for window_id, event in latest_commands.items():
        sequence = event.payload_json.get("sequence")
        if sequence is None:
            continue
        key = (window_id, str(sequence))
        if key in pair_set:
            indexed[key] = event

    missing_pairs = [pair for pair in sequence_pairs if pair not in indexed]
    if not missing_pairs:
        return indexed

    window_ids = {window_id for window_id, _sequence in missing_pairs}
    missing_set = set(missing_pairs)
    rows = list(
        await session.scalars(
            select(Event).where(
                Event.client_id == client_id,
                Event.virtual_window_id.in_(window_ids),
                Event.kind == TERMINAL_COMMAND_KIND,
            )
        )
    )
    for event in rows:
        if event.virtual_window_id is None:
            continue
        sequence = event.payload_json.get("sequence")
        if sequence is None:
            continue
        key = (event.virtual_window_id, str(sequence))
        if key in missing_set and key not in indexed:
            indexed[key] = event
    return indexed


def _finished_event_is_agent(
    event: Event,
    input_by_window_sequence: dict[tuple[UUID, str], Event],
) -> bool:
    if _event_agent(event) is not None:
        return True
    if event.virtual_window_id is None:
        return False
    sequence = event.payload_json.get("sequence")
    if sequence is None:
        return False
    input_event = input_by_window_sequence.get((event.virtual_window_id, str(sequence)))
    return _event_agent(input_event) is not None


async def _latest_events_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
    *,
    kind: str,
) -> dict[UUID, Event]:
    if not window_ids:
        return {}

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


def _merge_latest_working_activity(
    window_ids: list[UUID],
    *,
    latest_commands: dict[UUID, Event],
    latest_ai: dict[UUID, datetime],
    latest_presence: dict[UUID, datetime],
    finished_sequences: dict[UUID, set[str]],
) -> dict[UUID, datetime]:
    latest_work: dict[UUID, datetime] = {}
    for window_id in window_ids:
        command = latest_commands.get(window_id)
        candidates: list[datetime] = []
        if command is not None and _event_agent(command) is not None:
            sequence = command.payload_json.get("sequence")
            if sequence is None or str(sequence) not in finished_sequences.get(window_id, set()):
                candidates.append(command.created_at)
        if window_id in latest_ai:
            candidates.append(latest_ai[window_id])
        if window_id in latest_presence:
            candidates.append(latest_presence[window_id])
        if candidates:
            latest_work[window_id] = max(_aware_utc(value) for value in candidates)
    return latest_work


async def _finished_command_sequences_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
) -> dict[UUID, set[str]]:
    if not window_ids:
        return {}

    rows = await session.execute(
        select(Event.virtual_window_id, Event.payload_json).where(
            Event.client_id == client_id,
            Event.virtual_window_id.in_(window_ids),
            Event.kind == TERMINAL_COMMAND_FINISHED_KIND,
        )
    )
    sequences: dict[UUID, set[str]] = {}
    for window_id, payload in rows:
        if window_id is None:
            continue
        sequence = payload.get("sequence")
        if sequence is None:
            continue
        sequences.setdefault(window_id, set()).add(str(sequence))
    return sequences


async def _latest_activity_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
) -> dict[UUID, datetime]:
    rows = await session.execute(
        select(Event.virtual_window_id, func.max(Event.created_at))
        .where(
            Event.client_id == client_id,
            Event.virtual_window_id.in_(window_ids),
            or_(
                Event.kind.in_(TERMINAL_ACTIVITY_KINDS),
                Event.source_type.in_(agent_activity_source_types()),
            ),
        )
        .group_by(Event.virtual_window_id)
    )
    return {window_id: created_at for window_id, created_at in rows if window_id is not None}


async def _latest_agent_work_presence_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
) -> dict[UUID, datetime]:
    rows = await session.execute(
        select(Event.virtual_window_id, func.max(Event.created_at))
        .where(
            Event.client_id == client_id,
            Event.virtual_window_id.in_(window_ids),
            Event.kind == AGENT_WORK_PRESENCE_KIND,
        )
        .group_by(Event.virtual_window_id)
    )
    return {window_id: created_at for window_id, created_at in rows if window_id is not None}


async def _latest_ai_activity_by_window(
    session: AsyncSession,
    client_id: UUID,
    window_ids: list[UUID],
) -> dict[UUID, datetime]:
    rows = await session.execute(
        select(Event.virtual_window_id, func.max(Event.created_at))
        .where(
            Event.client_id == client_id,
            Event.virtual_window_id.in_(window_ids),
            Event.source_type.in_(agent_activity_source_types()),
        )
        .group_by(Event.virtual_window_id)
    )
    return {window_id: created_at for window_id, created_at in rows if window_id is not None}


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
