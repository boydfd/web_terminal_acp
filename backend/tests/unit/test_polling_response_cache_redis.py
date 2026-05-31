from __future__ import annotations

from uuid import uuid4

import pytest

from app.services import cache_backend
from app.services import polling_response_cache
from app.services.polling_response_cache import (
    cached_json_response,
    clear_polling_response_cache,
    expire_polling_response_cache,
    invalidate_polling_response_cache,
    store_json_response,
)


def test_polling_response_cache_uses_redis_when_available(monkeypatch) -> None:
    stored: dict[str, dict[str, object]] = {}

    monkeypatch.setattr(
        cache_backend,
        "set_indexed_json",
        lambda namespace, key, value, *, resources, client_id, ttl_seconds: stored.setdefault(repr(key), value) is value,
    )
    monkeypatch.setattr(cache_backend, "get_json", lambda namespace, key: stored.get(repr(key)))
    monkeypatch.setattr(cache_backend, "delete_keys", lambda keys: None)
    monkeypatch.setattr(cache_backend, "clear_namespace", lambda namespace: stored.clear())
    monkeypatch.setattr(cache_backend, "delete_indexed", lambda namespace, resources, *, client_id: None)
    monkeypatch.setattr(cache_backend, "expire_indexed_json", lambda namespace, resources, *, client_id, created_at, ttl_seconds: None)

    cache_key = ("tree", uuid4())
    response = store_json_response(cache_key, {"ok": True}, resources={"tree"})

    assert response.status_code == 200
    assert cached_json_response(cache_key) is not None

    clear_polling_response_cache()


def test_polling_response_cache_invalidates_redis_entries(monkeypatch) -> None:
    client_id = uuid4()
    delete_calls: list[tuple[str, set[str], str | None]] = []

    monkeypatch.setattr(cache_backend, "delete_indexed", lambda namespace, resources, *, client_id: delete_calls.append((namespace, resources, client_id)))
    monkeypatch.setattr(cache_backend, "clear_namespace", lambda namespace: None)

    invalidate_polling_response_cache({"tree"}, client_id=client_id)

    assert delete_calls == [("polling-response", {"tree"}, str(client_id))]


def test_polling_response_cache_expires_redis_entries(monkeypatch) -> None:
    client_id = uuid4()
    expire_calls: list[tuple[str, set[str], str | None, float, int]] = []

    monkeypatch.setattr(
        cache_backend,
        "expire_indexed_json",
        lambda namespace, resources, *, client_id, created_at, ttl_seconds: expire_calls.append(
            (namespace, resources, client_id, created_at, ttl_seconds)
        ),
    )
    monkeypatch.setattr(cache_backend, "clear_namespace", lambda namespace: None)
    monkeypatch.setattr(polling_response_cache, "monotonic", lambda: 100.0)

    expire_polling_response_cache({"window"}, client_id=client_id)

    assert expire_calls == [("polling-response", {"window"}, str(client_id), 89.0, 60)]


@pytest.mark.asyncio
async def test_cache_backend_skips_sync_redis_inside_event_loop(monkeypatch) -> None:
    def fail_get_client():
        raise AssertionError("sync Redis client should not be opened inside the event loop")

    monkeypatch.setattr(cache_backend, "_get_client", fail_get_client)

    assert cache_backend.get_json("polling-response", ("tree", uuid4())) is None
