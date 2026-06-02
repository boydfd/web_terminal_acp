from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_tools.common import stable_hash
from app.db import prefer_deferred_commit
from app.models import Event, EventSourceType
from app.repositories.events import _select_event_by_fingerprint
from app.repositories.windows import get_window_for_client

from app.services.event_kinds import AGENT_WORK_PRESENCE_KIND

PRESENCE_FINGERPRINT_BUCKET_SECONDS = 30


async def touch_agent_work_presence(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
    providers: list[str],
    reasons: list[str],
    observed_at: datetime | None = None,
) -> Event:
    await prefer_deferred_commit(session)

    window = await get_window_for_client(session, client_id, window_id)
    if window is None:
        raise ValueError("window not found for client")

    current = observed_at or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)

    bucket = int(current.timestamp()) // PRESENCE_FINGERPRINT_BUCKET_SECONDS
    fingerprint = stable_hash(
        {
            "kind": AGENT_WORK_PRESENCE_KIND,
            "client_id": str(client_id),
            "window_id": str(window_id),
            "bucket": bucket,
        }
    )
    fingerprint = f"agent_work_presence:{fingerprint}"

    existing = await _select_event_by_fingerprint(session, client_id, fingerprint)
    payload_json = {
        "providers": providers,
        "reasons": reasons,
        "observed_at": current.isoformat(),
    }
    if existing is not None:
        existing.created_at = current
        existing.payload_json = payload_json
        await session.flush()
        return existing

    row = Event(
        client_id=client_id,
        source_type=EventSourceType.terminal,
        source_id=str(window_id),
        kind=AGENT_WORK_PRESENCE_KIND,
        virtual_window_id=window_id,
        payload_json=payload_json,
        fingerprint=fingerprint,
        created_at=current,
    )
    session.add(row)
    await session.flush()
    return row
