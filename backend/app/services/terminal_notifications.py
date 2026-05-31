from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TerminalNotificationState, VirtualWindow
from app.services.terminal_work_status import (
    ABORTED_AGENT_TASK_STATUS,
    FINISHED_AGENT_TASK_STATUS,
    AgentTaskStatus,
    load_tree_window_activity,
)


@dataclass(frozen=True)
class TerminalNotification:
    id: str
    client_id: UUID
    window_id: UUID
    window_title: str
    completed_at: datetime
    status: str
    read: bool


def notification_id(client_id: UUID, window_id: UUID, status: str, completed_at: datetime) -> str:
    return f"{client_id}:{window_id}:{status}:{_aware_utc(completed_at).isoformat()}"


async def list_terminal_notifications(
    session: AsyncSession,
    client_id: UUID,
) -> list[TerminalNotification]:
    windows = list(
        await session.scalars(
            select(VirtualWindow)
            .where(
                VirtualWindow.client_id == client_id,
                VirtualWindow.folder_id.is_not(None),
            )
            .order_by(VirtualWindow.created_at, VirtualWindow.title, VirtualWindow.id)
        )
    )
    if not windows:
        return []

    window_ids = [window.id for window in windows]
    activity = await load_tree_window_activity(
        session,
        client_id,
        window_ids,
        include_runtime_tags=False,
    )
    states = {
        state.window_id: state
        for state in await session.scalars(
            select(TerminalNotificationState).where(
                TerminalNotificationState.client_id == client_id,
                TerminalNotificationState.window_id.in_(window_ids),
            )
        )
    }

    notifications: list[TerminalNotification] = []
    for window in windows:
        task_status = activity.last_agent_task_status.get(window.id)
        if task_status is None:
            continue
        notification = _notification_from_window(
            client_id,
            window,
            task_status,
            states.get(window.id),
        )
        if notification is not None:
            notifications.append(notification)

    notifications.sort(key=lambda item: _aware_utc(item.completed_at), reverse=True)
    return notifications


async def mark_terminal_notification_read(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    completed_at: datetime,
) -> None:
    await _require_matching_notification(session, client_id, window_id, completed_at)
    state = await _get_or_create_state(session, client_id, window_id)
    completed = _aware_utc(completed_at)
    if not _at_or_after(state.read_at, completed):
        state.read_at = completed
    if state.dismissed_at is not None and not _at_or_after(state.dismissed_at, completed):
        state.dismissed_at = None
    await session.flush()


async def dismiss_terminal_notification(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    completed_at: datetime,
) -> None:
    await _require_matching_notification(session, client_id, window_id, completed_at)
    state = await _get_or_create_state(session, client_id, window_id)
    completed = _aware_utc(completed_at)
    if not _at_or_after(state.read_at, completed):
        state.read_at = completed
    if not _at_or_after(state.dismissed_at, completed):
        state.dismissed_at = completed
    await session.flush()


async def clear_terminal_notifications(session: AsyncSession, client_id: UUID) -> None:
    notifications = await list_terminal_notifications(session, client_id)
    for notification in notifications:
        await dismiss_terminal_notification(
            session,
            client_id,
            notification.window_id,
            notification.completed_at,
        )


def _notification_from_window(
    client_id: UUID,
    window: VirtualWindow,
    task_status: AgentTaskStatus,
    state: TerminalNotificationState | None,
) -> TerminalNotification | None:
    if task_status.state not in {FINISHED_AGENT_TASK_STATUS, ABORTED_AGENT_TASK_STATUS}:
        return None

    completed_at = _aware_utc(task_status.occurred_at)
    if state is not None and _at_or_after(state.dismissed_at, completed_at):
        return None

    return TerminalNotification(
        id=notification_id(client_id, window.id, task_status.state, completed_at),
        client_id=client_id,
        window_id=window.id,
        window_title=window.title,
        completed_at=completed_at,
        status=task_status.state,
        read=state is not None and _at_or_after(state.read_at, completed_at),
    )


async def _require_matching_notification(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    completed_at: datetime,
) -> None:
    notifications = await list_terminal_notifications(session, client_id)
    completed = _aware_utc(completed_at)
    if not any(
        notification.window_id == window_id and _aware_utc(notification.completed_at) == completed
        for notification in notifications
    ):
        raise TerminalNotificationNotFoundError


async def _get_or_create_state(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
) -> TerminalNotificationState:
    state = await session.scalar(
        select(TerminalNotificationState).where(
            TerminalNotificationState.client_id == client_id,
            TerminalNotificationState.window_id == window_id,
        )
    )
    if state is not None:
        return state

    state = TerminalNotificationState(client_id=client_id, window_id=window_id)
    session.add(state)
    await session.flush()
    return state


class TerminalNotificationNotFoundError(Exception):
    pass


def _at_or_after(value: datetime | None, reference: datetime) -> bool:
    return value is not None and _aware_utc(value) >= _aware_utc(reference)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
