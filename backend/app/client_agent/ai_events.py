from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class ManagedAiEvent:
    provider: str
    client_id: UUID
    window_id: UUID
    source_path: str | None
    offset: int | None
    cursor: str | int | None
    project_path: str | None
    payload: dict[str, Any]


def managed_event_from_payload(
    client_id: UUID,
    window_id: UUID,
    provider: str,
    payload: dict[str, Any],
    source_path: str | None = None,
    offset: int | None = None,
    cursor: str | int | None = None,
    project_path: str | None = None,
) -> ManagedAiEvent | None:
    payload_client_id = _payload_uuid(payload, "WEB_TERMINAL_CLIENT_ID", "client_id")
    payload_window_id = _payload_uuid(
        payload,
        "WEB_TERMINAL_WINDOW_ID",
        "virtual_window_id",
        "virtualWindowId",
    )
    if payload_client_id != client_id or payload_window_id != window_id:
        return None

    return ManagedAiEvent(
        provider=provider,
        client_id=client_id,
        window_id=window_id,
        source_path=source_path,
        offset=offset,
        cursor=cursor,
        project_path=project_path or _payload_text(payload, "WEB_TERMINAL_PROJECT_PATH", "project_path", "projectPath"),
        payload=payload,
    )


def _payload_uuid(payload: dict[str, Any], *keys: str) -> UUID | None:
    for key in keys:
        raw_value = payload.get(key)
        if raw_value is None:
            continue
        try:
            return UUID(str(raw_value))
        except (TypeError, ValueError):
            return None
    return None


def _payload_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value:
            return value
    return None
