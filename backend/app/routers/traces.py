from datetime import UTC, datetime
import json
from typing import Any

from elastic_transport import TransportError
from elasticsearch import ApiError, AsyncElasticsearch
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session, prefer_deferred_commit
from app.models import Event
from app.repositories.clients import ensure_local_client
from app.routers.ui_events import ui_event_hub_from_state
from app.schemas import IngestEventOut
from app.services.ingest.codex_receiver import index_codex_trace_event, receive_codex_trace

router = APIRouter(prefix="/api/traces", tags=["traces"])
MAX_CODEX_TRACE_PAYLOAD_BYTES = 256 * 1024


def get_ready_es_client(request: Request) -> AsyncElasticsearch | None:
    if getattr(request.app.state, "es_indexes_ready", False) is not True:
        return None
    return getattr(request.app.state, "es_client", None)


def _serialized_payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def reject_oversized_payload(payload: dict[str, Any]) -> None:
    if _serialized_payload_size(payload) > MAX_CODEX_TRACE_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="codex trace payload too large")


def to_ingest_event_out(event: Event) -> IngestEventOut:
    return IngestEventOut(
        id=event.id,
        source_type=event.source_type.value,
        source_id=event.source_id,
        kind=event.kind,
        fingerprint=event.fingerprint,
    )


@router.post("/codex", response_model=IngestEventOut)
async def ingest_codex_trace(
    request: Request,
    payload: dict[str, Any] = Body(...),
    session: AsyncSession = Depends(get_session),
    es_client: AsyncElasticsearch | None = Depends(get_ready_es_client),
) -> IngestEventOut:
    reject_oversized_payload(payload)
    local_client = await ensure_local_client(session)
    await session.commit()
    await prefer_deferred_commit(session)
    try:
        event = await receive_codex_trace(
            session, payload, client_id=local_client.id, es_client=es_client
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(event)

    if es_client is not None and event.indexed_at is None:
        try:
            await index_codex_trace_event(es_client, event, payload)
        except (ApiError, TransportError):
            return to_ingest_event_out(event)
        event.indexed_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(event)

    await ui_event_hub_from_state(request.app.state).publish_invalidation(
        ["agent_record", "window", "tree", "search"],
        client_id=local_client.id,
        window_id=event.virtual_window_id,
        reason="trace_ingested",
    )
    return to_ingest_event_out(event)
