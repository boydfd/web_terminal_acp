from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from hashlib import sha256
from time import monotonic
from typing import Any

from fastapi.encoders import jsonable_encoder
from redis import Redis
from redis.exceptions import RedisError

from app.config import get_settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "web-terminal-acp:cache"
_REDIS_RETRY_AFTER_SECONDS = 30.0
_REDIS_TIMEOUT_SECONDS = 0.05

_redis_client: Redis | None = None
_redis_url: str | None = None
_redis_disabled_until = 0.0


def enabled() -> bool:
    return bool(get_settings().redis_url)


def cache_key(namespace: str, key_parts: object) -> str:
    encoded = json.dumps(
        jsonable_encoder(key_parts),
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = sha256(encoded.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{namespace}:{digest}"


def get_json(namespace: str, key_parts: object) -> dict[str, Any] | None:
    key = cache_key(namespace, key_parts)

    def operation(client: Redis) -> dict[str, Any] | None:
        raw = client.get(key)
        if raw is None:
            return None
        try:
            value = json.loads(str(raw))
        except json.JSONDecodeError:
            client.delete(key)
            return None
        return value if isinstance(value, dict) else None

    return _run(operation)


def set_json(namespace: str, key_parts: object, value: dict[str, Any], *, ttl_seconds: int) -> bool:
    key = cache_key(namespace, key_parts)
    return set_json_key(key, value, ttl_seconds=ttl_seconds)


def set_indexed_json(
    namespace: str,
    key_parts: object,
    value: dict[str, Any],
    *,
    resources: frozenset[str],
    client_id: str | None,
    ttl_seconds: int,
) -> bool:
    key = cache_key(namespace, key_parts)
    payload = json.dumps(jsonable_encoder(value), separators=(",", ":"))

    def operation(client: Redis) -> bool:
        pipeline = client.pipeline()
        pipeline.set(key, payload, ex=ttl_seconds)
        for index_key in _index_keys(namespace, resources, client_id):
            pipeline.sadd(index_key, key)
            pipeline.expire(index_key, ttl_seconds)
        pipeline.execute()
        return True

    return _run(operation) is True


def set_json_key(key: str, value: dict[str, Any], *, ttl_seconds: int) -> bool:
    payload = json.dumps(jsonable_encoder(value), separators=(",", ":"))

    def operation(client: Redis) -> bool:
        client.set(key, payload, ex=ttl_seconds)
        return True

    return _run(operation) is True


def delete_indexed(namespace: str, resources: set[str], *, client_id: str | None) -> None:
    keys = _indexed_data_keys(namespace, resources, client_id=client_id)
    delete_keys(list(keys))


def expire_indexed_json(
    namespace: str,
    resources: set[str],
    *,
    client_id: str | None,
    created_at: float,
    ttl_seconds: int,
) -> None:
    keys = _indexed_data_keys(namespace, resources, client_id=client_id)
    if not keys:
        return

    def operation(client: Redis) -> None:
        pipeline = client.pipeline()
        for key in keys:
            raw = client.get(key)
            if raw is None:
                continue
            try:
                value = json.loads(str(raw))
            except json.JSONDecodeError:
                pipeline.delete(key)
                continue
            if not isinstance(value, dict):
                pipeline.delete(key)
                continue
            value["created_at"] = created_at
            pipeline.set(
                key,
                json.dumps(jsonable_encoder(value), separators=(",", ":")),
                ex=ttl_seconds,
            )
        pipeline.execute()

    _run(operation)


def delete_matching(namespace: str, predicate: Callable[[dict[str, Any]], bool]) -> None:
    keys_to_delete: list[str] = []
    for key, value in iter_namespace(namespace):
        if predicate(value):
            keys_to_delete.append(key)
    delete_keys(keys_to_delete)


def iter_namespace(namespace: str) -> list[tuple[str, dict[str, Any]]]:
    pattern = f"{_KEY_PREFIX}:{namespace}:*"

    def operation(client: Redis) -> list[tuple[str, dict[str, Any]]]:
        entries: list[tuple[str, dict[str, Any]]] = []
        for key in client.scan_iter(match=pattern, count=100):
            raw = client.get(key)
            if raw is None:
                continue
            try:
                value = json.loads(str(raw))
            except json.JSONDecodeError:
                client.delete(key)
                continue
            if isinstance(value, dict):
                entries.append((str(key), value))
        return entries

    return _run(operation) or []


def delete_keys(keys: list[str]) -> None:
    if not keys:
        return

    def operation(client: Redis) -> None:
        client.delete(*keys)

    _run(operation)


def clear_namespace(namespace: str) -> None:
    pattern = f"{_KEY_PREFIX}:{namespace}:*"
    index_pattern = f"{_KEY_PREFIX}:idx:{namespace}:*"

    def operation(client: Redis) -> None:
        keys = list(client.scan_iter(match=pattern, count=100)) + list(
            client.scan_iter(match=index_pattern, count=100)
        )
        if keys:
            client.delete(*keys)

    _run(operation)


def _run(operation: Callable[[Redis], Any]) -> Any:
    if _running_event_loop():
        return None

    client = _get_client()
    if client is None:
        return None
    try:
        return operation(client)
    except (OSError, RedisError):
        _disable_temporarily()
        logger.warning("redis cache operation failed; falling back to local cache", exc_info=True)
        return None


def _get_client() -> Redis | None:
    global _redis_client, _redis_disabled_until, _redis_url

    url = get_settings().redis_url
    if not url:
        return None
    now = monotonic()
    if now < _redis_disabled_until:
        return None
    if _redis_client is not None and _redis_url == url:
        return _redis_client

    _redis_url = url
    _redis_client = Redis.from_url(
        url,
        decode_responses=True,
        socket_timeout=_REDIS_TIMEOUT_SECONDS,
        socket_connect_timeout=_REDIS_TIMEOUT_SECONDS,
    )
    return _redis_client


def _disable_temporarily() -> None:
    global _redis_client, _redis_disabled_until
    _redis_client = None
    _redis_disabled_until = monotonic() + _REDIS_RETRY_AFTER_SECONDS


def _running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _index_keys(namespace: str, resources: frozenset[str], client_id: str | None) -> list[str]:
    keys: list[str] = []
    client_index_value = client_id or "_global"
    for resource in resources:
        keys.append(f"{_KEY_PREFIX}:idx:{namespace}:resource:{resource}")
        keys.append(f"{_KEY_PREFIX}:idx:{namespace}:resource-client:{resource}:{client_index_value}")
    return keys


def _indexed_data_keys(
    namespace: str,
    resources: set[str],
    *,
    client_id: str | None,
) -> set[str]:
    def operation(client: Redis) -> set[str]:
        keys: set[str] = set()
        for resource in resources:
            if client_id is None:
                keys.update(str(key) for key in client.smembers(f"{_KEY_PREFIX}:idx:{namespace}:resource:{resource}"))
                continue
            keys.update(
                str(key)
                for key in client.smembers(
                    f"{_KEY_PREFIX}:idx:{namespace}:resource-client:{resource}:_global"
                )
            )
            keys.update(
                str(key)
                for key in client.smembers(
                    f"{_KEY_PREFIX}:idx:{namespace}:resource-client:{resource}:{client_id}"
                )
            )
        return keys

    return _run(operation) or set()
