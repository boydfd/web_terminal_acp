from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event
from app.services.ingest.normalizers import NormalizedEvent


async def _select_event_by_fingerprint(
    session: AsyncSession, client_id: UUID, fingerprint: str
) -> Event | None:
    return await session.scalar(
        select(Event).where(Event.client_id == client_id, Event.fingerprint == fingerprint)
    )


async def insert_normalized_event(
    session: AsyncSession,
    event: NormalizedEvent,
    *,
    client_id: UUID,
    virtual_window_id: UUID | None = None,
) -> Event:
    existing_event = await _select_event_by_fingerprint(session, client_id, event.fingerprint)
    if existing_event is not None:
        return existing_event

    if event.payload_json is None:
        raise ValueError("event payload_json is required")

    db_event = Event(
        client_id=client_id,
        source_type=event.source_type,
        source_id=event.source_id,
        kind=event.kind,
        virtual_window_id=virtual_window_id,
        payload_json=event.payload_json,
        fingerprint=event.fingerprint,
    )

    try:
        async with session.begin_nested():
            session.add(db_event)
            await session.flush()
    except IntegrityError as exc:
        existing_event = await _select_event_by_fingerprint(session, client_id, event.fingerprint)
        if existing_event is not None:
            return existing_event
        raise exc

    return db_event
