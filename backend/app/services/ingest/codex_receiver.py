from __future__ import annotations

from typing import Any
from uuid import UUID

from elasticsearch import AsyncElasticsearch
from sqlalchemy.ext.asyncio import AsyncSession

from app.client_agent.ai_events import ManagedAiEvent
from app.models import Event, VirtualWindow
from app.repositories.ai_sessions import get_or_create_ai_session
from app.repositories.events import insert_normalized_event
from app.repositories.summary_jobs import enqueue_summary_job
from app.services.ingest.normalizers import normalize_codex_trace
from app.services.search_index import index_ai_event


async def receive_managed_codex_trace(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    client_id: UUID,
    window_id: UUID,
    es_client: AsyncElasticsearch | None = None,
) -> Event:
    """Normalize and persist a managed Codex trace for a known client/window."""
    from app.services.agent_event_ingest import persist_managed_agent_event

    source_path = payload.get("source_path")
    project_path = payload.get("project_path")
    cursor = payload.get("cursor") or payload.get("offset")
    return await persist_managed_agent_event(
        session,
        ManagedAiEvent(
            provider="codex",
            client_id=client_id,
            window_id=window_id,
            source_path=source_path if isinstance(source_path, str) else None,
            offset=None,
            cursor=cursor,
            project_path=project_path if isinstance(project_path, str) else None,
            payload=payload,
        ),
        es_client=es_client,
    )


async def receive_codex_trace(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    client_id: UUID,
    es_client: AsyncElasticsearch | None = None,
) -> Event:
    """Normalize and persist a Codex trace without indexing before durable commit."""
    _ = es_client
    normalized = normalize_codex_trace(payload)
    virtual_window_id = await _payload_virtual_window_id(session, payload, client_id)
    row = await insert_normalized_event(
        session,
        normalized,
        client_id=client_id,
        virtual_window_id=virtual_window_id,
    )

    if row.client_id != client_id:
        raise ValueError("event does not belong to client")
    if row.virtual_window_id is None:
        row.virtual_window_id = virtual_window_id
    elif row.virtual_window_id != virtual_window_id:
        raise ValueError("event virtual_window_id does not match payload")

    ai_session = await get_or_create_ai_session(
        session,
        client_id=client_id,
        provider="codex",
        source_id=normalized.source_id,
        virtual_window_id=row.virtual_window_id,
    )
    row.ai_session_id = ai_session.id
    await enqueue_summary_job(session, row.virtual_window_id)

    await session.flush()
    return row


async def _payload_virtual_window_id(
    session: AsyncSession,
    payload: dict[str, Any],
    client_id: UUID,
) -> UUID:
    raw_value = payload.get("virtual_window_id") or payload.get("virtualWindowId")
    if not isinstance(raw_value, str):
        raise ValueError("virtual_window_id is required")

    try:
        window_id = UUID(raw_value)
    except ValueError as exc:
        raise ValueError("virtual_window_id is invalid") from exc

    window = await session.get(VirtualWindow, window_id)
    if window is None:
        raise ValueError("virtual_window_id not found")
    if window.client_id != client_id:
        raise ValueError("virtual_window_id does not belong to client")
    return window_id


async def index_codex_trace_event(
    es_client: AsyncElasticsearch,
    row: Event,
    payload: dict[str, Any],
) -> None:
    normalized = normalize_codex_trace(payload)
    await index_ai_event(
        es_client,
        row.client_id,
        provider="codex",
        session_id=row.source_id,
        kind=row.kind,
        text=normalized.text,
        raw=payload,
        virtual_window_id=row.virtual_window_id,
        document_id=str(row.id),
    )
