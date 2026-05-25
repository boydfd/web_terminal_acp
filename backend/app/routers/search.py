from typing import Annotated
from uuid import UUID

from elastic_transport import TransportError
from elasticsearch import ApiError, AsyncElasticsearch
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Client, LOCAL_CLIENT_ID
from app.repositories.clients import get_client
from app.schemas import SearchOut
from app.services.search_index import ensure_indexes, search_all

router = APIRouter(prefix="/api", tags=["search"])

SearchQuery = Annotated[str, Query(min_length=1, max_length=512)]


async def validated_search_query(q: SearchQuery) -> str:
    query = q.strip()
    if not query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="search query is required")
    return query


async def require_client(
    client_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Client:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    return client


def get_search_client(
    request: Request,
    _query: Annotated[str, Depends(validated_search_query)],
) -> AsyncElasticsearch:
    client = getattr(request.app.state, "es_client", None)
    if client is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="search service unavailable")
    return client


async def ensure_search_ready(request: Request, client: AsyncElasticsearch) -> None:
    if getattr(request.app.state, "es_indexes_ready", True):
        return

    try:
        await ensure_indexes(client)
    except (ApiError, TransportError) as exc:
        request.app.state.es_startup_error = exc
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="search service unavailable",
        ) from exc

    request.app.state.es_indexes_ready = True
    request.app.state.es_startup_error = None


async def _search_for_client(
    request: Request,
    query: str,
    client: AsyncElasticsearch,
    client_id: UUID,
) -> SearchOut:
    await ensure_search_ready(request, client)
    try:
        results = await search_all(
            client,
            query,
            client_id,
            include_legacy_local_documents=client_id == LOCAL_CLIENT_ID,
        )
    except (ApiError, TransportError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="search service unavailable",
        ) from exc
    return SearchOut(query=query, results=results)


@router.get("/clients/{client_id}/search", response_model=SearchOut)
async def search_client_scope(
    request: Request,
    query: Annotated[str, Depends(validated_search_query)],
    scoped_client: Annotated[Client, Depends(require_client)],
    client: Annotated[AsyncElasticsearch, Depends(get_search_client)],
) -> SearchOut:
    return await _search_for_client(request, query, client, scoped_client.id)


@router.get("/search", response_model=SearchOut)
async def search(
    request: Request,
    query: Annotated[str, Depends(validated_search_query)],
    client: Annotated[AsyncElasticsearch, Depends(get_search_client)],
) -> SearchOut:
    return await _search_for_client(request, query, client, LOCAL_CLIENT_ID)
