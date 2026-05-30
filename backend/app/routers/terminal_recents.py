from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Client
from app.routers.ui_events import ui_event_hub_from_state
from app.repositories.clients import get_client
from app.repositories.terminal_recents import (
    DEFAULT_TERMINAL_RECENTS_PAGE_SIZE,
    list_terminal_recents,
    total_pages,
    touch_terminal_recent,
)
from app.schemas import TerminalRecentPageOut, TerminalRecentOut, TerminalRecentTouchIn

router = APIRouter(prefix="/api/clients", tags=["terminal-recents"])


async def _require_client(session: AsyncSession, client_id: UUID) -> Client:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    return client


@router.get("/{client_id}/terminal-recents", response_model=TerminalRecentPageOut)
async def get_terminal_recents(
    client_id: UUID,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_TERMINAL_RECENTS_PAGE_SIZE, ge=1, le=100),
    q: str | None = Query(default=None, max_length=255),
    session: AsyncSession = Depends(get_session),
) -> TerminalRecentPageOut:
    await _require_client(session, client_id)
    items, total = await list_terminal_recents(
        session,
        client_id=client_id,
        page=page,
        page_size=page_size,
        query=q,
    )
    return TerminalRecentPageOut(
        items=[
            TerminalRecentOut(
                window_id=item.window_id,
                title=title,
                last_used_at=item.last_used_at,
            )
            for item, title in items
        ],
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages(total, page_size),
    )


@router.post("/{client_id}/terminal-recents", response_model=TerminalRecentOut)
async def record_terminal_recent(
    request: Request,
    client_id: UUID,
    payload: TerminalRecentTouchIn,
    session: AsyncSession = Depends(get_session),
) -> TerminalRecentOut:
    await _require_client(session, client_id)
    usage = await touch_terminal_recent(
        session,
        client_id=client_id,
        window_id=payload.window_id,
        title=payload.title,
    )
    if usage is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")
    await session.commit()
    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["window"],
        client_id=client_id,
        window_id=payload.window_id,
        reason="terminal_recent",
    )
    return TerminalRecentOut(
        window_id=usage.window_id,
        title=usage.title,
        last_used_at=usage.last_used_at,
    )
