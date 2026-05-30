from __future__ import annotations

import json
from dataclasses import dataclass
from time import monotonic
from uuid import UUID

from fastapi import Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from app.services import cache_backend

_CACHE_TTL_SECONDS = 10.0
_CACHE_REDIS_TTL_SECONDS = 60


@dataclass(frozen=True)
class _CacheEntry:
    created_at: float
    content: str
    resources: frozenset[str]
    client_id: UUID | None


@dataclass(frozen=True)
class CachedJsonResponse:
    response: Response
    expired: bool


_response_cache: dict[tuple[object, ...], _CacheEntry] = {}
_refreshing_cache_keys: set[tuple[object, ...]] = set()


def cached_json_response(cache_key: tuple[object, ...]) -> Response | None:
    cached = _response_cache.get(cache_key)
    if cached is None:
        cached = _redis_cache_entry(cache_key)
        if cached is None:
            return None
    if monotonic() - cached.created_at > _CACHE_TTL_SECONDS:
        _response_cache.pop(cache_key, None)
        return None
    return Response(content=cached.content, media_type="application/json")


def cached_or_stale_json_response(cache_key: tuple[object, ...]) -> CachedJsonResponse | None:
    cached = _response_cache.get(cache_key)
    if cached is None:
        cached = _redis_cache_entry(cache_key)
        if cached is None:
            return None
    return CachedJsonResponse(
        response=Response(content=cached.content, media_type="application/json"),
        expired=monotonic() - cached.created_at > _CACHE_TTL_SECONDS,
    )


def store_json_response(
    cache_key: tuple[object, ...],
    payload: object,
    *,
    resources: set[str] | frozenset[str] | tuple[str, ...] | list[str],
    client_id: UUID | None = None,
) -> Response:
    content = json.dumps(_response_payload(payload), separators=(",", ":"))
    entry = _CacheEntry(
        created_at=monotonic(),
        content=content,
        resources=frozenset(resources),
        client_id=client_id,
    )
    if not _store_redis_cache_entry(cache_key, entry):
        _response_cache[cache_key] = entry
    return Response(content=content, media_type="application/json")


def begin_response_cache_refresh(cache_key: tuple[object, ...]) -> bool:
    if cache_key in _refreshing_cache_keys:
        return False
    _refreshing_cache_keys.add(cache_key)
    return True


def finish_response_cache_refresh(cache_key: tuple[object, ...]) -> None:
    _refreshing_cache_keys.discard(cache_key)


def invalidate_polling_response_cache(
    resources: set[str] | frozenset[str] | tuple[str, ...] | list[str],
    *,
    client_id: UUID | None = None,
) -> None:
    resource_set = set(resources)
    if not resource_set:
        return
    stale_keys = [
        key
        for key, entry in _response_cache.items()
        if entry.resources & resource_set
        and (client_id is None or entry.client_id is None or entry.client_id == client_id)
    ]
    for key in stale_keys:
        _response_cache.pop(key, None)
    cache_backend.delete_indexed(
        "polling-response",
        resource_set,
        client_id=str(client_id) if client_id is not None else None,
    )


def expire_polling_response_cache(
    resources: set[str] | frozenset[str] | tuple[str, ...] | list[str],
    *,
    client_id: UUID | None = None,
) -> None:
    resource_set = set(resources)
    if not resource_set:
        return
    expired_at = monotonic() - _CACHE_TTL_SECONDS - 1.0
    stale_keys = [
        key
        for key, entry in _response_cache.items()
        if entry.resources & resource_set
        and (client_id is None or entry.client_id is None or entry.client_id == client_id)
    ]
    for key in stale_keys:
        entry = _response_cache[key]
        _response_cache[key] = _CacheEntry(
            created_at=expired_at,
            content=entry.content,
            resources=entry.resources,
            client_id=entry.client_id,
        )
    cache_backend.expire_indexed_json(
        "polling-response",
        resource_set,
        client_id=str(client_id) if client_id is not None else None,
        created_at=expired_at,
        ttl_seconds=_CACHE_REDIS_TTL_SECONDS,
    )


def clear_polling_response_cache() -> None:
    _response_cache.clear()
    _refreshing_cache_keys.clear()
    cache_backend.clear_namespace("polling-response")


def response_cache_scope(session: object) -> tuple[int, str]:
    get_bind = getattr(session, "get_bind", None)
    if get_bind is None:
        return (id(type(session)), "")
    bind = get_bind()
    url = getattr(bind, "url", None)
    if url is not None:
        return (id(bind), url.render_as_string(hide_password=True))
    return (id(bind), "")


def _response_payload(payload: object) -> object:
    if isinstance(payload, BaseModel):
        return jsonable_encoder(payload.model_dump())
    return jsonable_encoder(payload)


def _redis_cache_entry(cache_key: tuple[object, ...]) -> _CacheEntry | None:
    cached = cache_backend.get_json("polling-response", cache_key)
    if cached is None:
        return None
    try:
        resources = cached["resources"]
        client_id = cached.get("client_id")
        return _CacheEntry(
            created_at=float(cached["created_at"]),
            content=str(cached["content"]),
            resources=frozenset(str(resource) for resource in resources),
            client_id=UUID(str(client_id)) if client_id is not None else None,
        )
    except (KeyError, TypeError, ValueError):
        cache_backend.delete_keys([cache_backend.cache_key("polling-response", cache_key)])
        return None


def _store_redis_cache_entry(cache_key: tuple[object, ...], entry: _CacheEntry) -> bool:
    return cache_backend.set_indexed_json(
        "polling-response",
        cache_key,
        {
            "created_at": entry.created_at,
            "content": entry.content,
            "resources": sorted(entry.resources),
            "client_id": str(entry.client_id) if entry.client_id is not None else None,
        },
        resources=entry.resources,
        client_id=str(entry.client_id) if entry.client_id is not None else None,
        ttl_seconds=_CACHE_REDIS_TTL_SECONDS,
    )
