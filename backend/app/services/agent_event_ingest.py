from __future__ import annotations

from datetime import UTC, datetime

from elastic_transport import TransportError
from elasticsearch import ApiError, AsyncElasticsearch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_tools import get_agent_tool_registry
from app.client_agent.ai_events import ManagedAiEvent, managed_event_from_payload
from app.models import AiSession, Event
from app.repositories.ai_sessions import get_or_create_ai_session
from app.repositories.events import insert_normalized_event
from app.repositories.summary_jobs import enqueue_summary_job
from app.repositories.windows import get_window_for_client
from app.services.search_index import index_ai_event

_PROVIDER_ALIASES = {"claude": "claude_code"}


def canonical_provider(provider: str) -> str:
    return _PROVIDER_ALIASES.get(provider, provider)


async def persist_managed_agent_event(
    session: AsyncSession,
    event: ManagedAiEvent,
    *,
    es_client: AsyncElasticsearch | None = None,
) -> Event:
    _ = es_client
    validated_event = managed_event_from_payload(
        event.client_id,
        event.window_id,
        event.provider,
        event.payload,
        source_path=event.source_path,
        offset=event.offset,
        cursor=event.cursor,
        project_path=event.project_path,
    )
    if validated_event is None:
        raise ValueError("event attribution does not match client/window")

    window = await get_window_for_client(session, event.client_id, event.window_id)
    if window is None:
        raise ValueError("window not found for client")

    provider = canonical_provider(event.provider)
    adapter = get_agent_tool_registry().by_provider(provider)
    cursor = event.cursor if event.cursor is not None else event.offset
    project_path = event.project_path or validated_event.project_path
    normalized = adapter.normalize(event.payload, source_path=event.source_path, cursor=cursor)
    row = await insert_normalized_event(
        session,
        normalized,
        client_id=event.client_id,
        virtual_window_id=event.window_id,
    )

    if row.client_id != event.client_id:
        raise ValueError("event does not belong to client")
    if row.virtual_window_id is None:
        row.virtual_window_id = event.window_id
    elif row.virtual_window_id != event.window_id:
        raise ValueError("event virtual_window_id does not match payload")

    ai_session = await get_or_create_ai_session(
        session,
        client_id=event.client_id,
        provider=provider,
        source_id=normalized.source_id,
        source_path=event.source_path,
        project_path=project_path or window.cwd,
        virtual_window_id=row.virtual_window_id,
    )
    row.ai_session_id = ai_session.id
    await enqueue_summary_job(session, row.virtual_window_id)
    await session.flush()

    return row


async def index_managed_agent_event_if_ready(
    session: AsyncSession,
    es_client: AsyncElasticsearch | None,
    row: Event,
) -> bool:
    if es_client is None or row.indexed_at is not None:
        return False

    provider = canonical_provider(await _event_provider(session, row))
    adapter = get_agent_tool_registry().by_provider(provider)
    try:
        await index_ai_event(
            es_client,
            row.client_id,
            provider=provider,
            session_id=row.source_id,
            kind=row.kind,
            text=adapter.index_text(row),
            raw=row.payload_json,
            virtual_window_id=row.virtual_window_id,
            document_id=str(row.id),
        )
    except (ApiError, TransportError):
        return False
    row.indexed_at = datetime.now(UTC)
    await session.flush()
    return True


async def _event_provider(session: AsyncSession, row: Event) -> str:
    if row.ai_session_id is not None:
        provider = await session.scalar(select(AiSession.provider).where(AiSession.id == row.ai_session_id))
        if provider:
            return provider

    raw_provider = row.payload_json.get("provider")
    if isinstance(raw_provider, str) and raw_provider.strip():
        return raw_provider.strip()

    raise ValueError("event provider is required for indexing")
