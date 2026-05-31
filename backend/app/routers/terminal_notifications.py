from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.repositories.clients import get_client
from app.repositories.windows import get_window_for_client
from app.routers.ui_events import ui_event_hub_from_state
from app.schemas import (
    TerminalNotificationAckIn,
    TerminalNotificationListOut,
    TerminalNotificationOut,
)
from app.services.terminal_notifications import (
    TerminalNotification,
    TerminalNotificationNotFoundError,
    clear_terminal_notifications,
    dismiss_terminal_notification,
    list_terminal_notifications,
    mark_terminal_notification_read,
)

router = APIRouter(prefix="/api", tags=["terminal-notifications"])


async def _require_client(session: AsyncSession, client_id: UUID) -> None:
    if await get_client(session, client_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")


async def _require_window(session: AsyncSession, client_id: UUID, window_id: UUID) -> None:
    if await get_window_for_client(session, client_id, window_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")


def _notification_out(notification: TerminalNotification) -> TerminalNotificationOut:
    return TerminalNotificationOut(
        id=notification.id,
        client_id=notification.client_id,
        window_id=notification.window_id,
        window_title=notification.window_title,
        completed_at=notification.completed_at,
        status=notification.status,
        read=notification.read,
    )


@router.get(
    "/clients/{client_id}/terminal-notifications",
    response_model=TerminalNotificationListOut,
)
async def read_terminal_notifications(
    client_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> TerminalNotificationListOut:
    await _require_client(session, client_id)
    notifications = await list_terminal_notifications(session, client_id)
    return TerminalNotificationListOut(
        notifications=[_notification_out(notification) for notification in notifications]
    )


@router.post(
    "/clients/{client_id}/terminal-notifications/read",
    response_model=TerminalNotificationListOut,
)
async def mark_terminal_notification_read_endpoint(
    request: Request,
    client_id: UUID,
    payload: TerminalNotificationAckIn,
    session: AsyncSession = Depends(get_session),
) -> TerminalNotificationListOut:
    await _require_client(session, client_id)
    await _require_window(session, client_id, payload.window_id)
    try:
        await mark_terminal_notification_read(
            session,
            client_id,
            payload.window_id,
            payload.completed_at,
        )
    except TerminalNotificationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="notification not found",
        ) from exc
    await session.commit()
    notifications = await list_terminal_notifications(session, client_id)
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["terminal_notifications"],
        client_id=client_id,
        window_id=payload.window_id,
        reason="terminal_notification_read",
    )
    return TerminalNotificationListOut(
        notifications=[_notification_out(notification) for notification in notifications]
    )


@router.post(
    "/clients/{client_id}/terminal-notifications/dismiss",
    response_model=TerminalNotificationListOut,
)
async def dismiss_terminal_notification_endpoint(
    request: Request,
    client_id: UUID,
    payload: TerminalNotificationAckIn,
    session: AsyncSession = Depends(get_session),
) -> TerminalNotificationListOut:
    await _require_client(session, client_id)
    await _require_window(session, client_id, payload.window_id)
    try:
        await dismiss_terminal_notification(
            session,
            client_id,
            payload.window_id,
            payload.completed_at,
        )
    except TerminalNotificationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="notification not found",
        ) from exc
    await session.commit()
    notifications = await list_terminal_notifications(session, client_id)
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["terminal_notifications"],
        client_id=client_id,
        window_id=payload.window_id,
        reason="terminal_notification_dismissed",
    )
    return TerminalNotificationListOut(
        notifications=[_notification_out(notification) for notification in notifications]
    )


@router.delete(
    "/clients/{client_id}/terminal-notifications",
    response_model=TerminalNotificationListOut,
)
async def clear_terminal_notifications_endpoint(
    request: Request,
    client_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> TerminalNotificationListOut:
    await _require_client(session, client_id)
    await clear_terminal_notifications(session, client_id)
    await session.commit()
    notifications = await list_terminal_notifications(session, client_id)
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["terminal_notifications"],
        client_id=client_id,
        reason="terminal_notifications_cleared",
    )
    return TerminalNotificationListOut(
        notifications=[_notification_out(notification) for notification in notifications]
    )
