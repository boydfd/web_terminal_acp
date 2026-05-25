from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

_SESSION_ID_SUFFIX = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)

def codex_home_for_window(window_id: UUID | str) -> Path:
    return Path.home() / ".web-terminal-acp" / "codex-homes" / str(window_id)


def codex_sessions_dir(window_id: UUID | str) -> Path:
    return codex_home_for_window(window_id) / "sessions"


def iter_codex_session_files(window_id: UUID | str) -> list[Path]:
    sessions_dir = codex_sessions_dir(window_id)
    if not sessions_dir.exists():
        return []
    return sorted(path for path in sessions_dir.rglob("*.jsonl") if path.is_file())


def read_new_codex_events(
    path: Path,
    offset: int,
    *,
    client_id: UUID,
    window_id: UUID,
    max_events: int = 100,
) -> tuple[list[tuple[dict[str, Any], int]], int]:
    events: list[tuple[dict[str, Any], int]] = []
    next_offset = offset

    with path.open("rb") as handle:
        handle.seek(offset)
        while len(events) < max_events:
            line_offset = handle.tell()
            raw_line = handle.readline()
            if not raw_line:
                next_offset = handle.tell()
                break
            if not raw_line.endswith(b"\n"):
                next_offset = line_offset
                break

            next_offset = handle.tell()
            try:
                raw_event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(raw_event, dict) or not _should_send_codex_event(raw_event):
                continue
            events.append(
                (
                    _managed_codex_payload(
                        raw_event,
                        client_id=client_id,
                        window_id=window_id,
                        source_path=path,
                        offset=line_offset,
                    ),
                    line_offset,
                )
            )

    return events, next_offset


def _should_send_codex_event(raw_event: dict[str, Any]) -> bool:
    if raw_event.get("type") == "event_msg":
        payload = raw_event.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            return False
    return raw_event.get("type") in {"session_meta", "response_item", "event_msg"}


def _managed_codex_payload(
    raw_event: dict[str, Any],
    *,
    client_id: UUID,
    window_id: UUID,
    source_path: Path,
    offset: int,
) -> dict[str, Any]:
    session_id = _session_id(raw_event, source_path)
    event_type = raw_event.get("type") if isinstance(raw_event.get("type"), str) else "codex_event"
    return {
        "trace_id": session_id,
        "id": f"{session_id}:{offset}",
        "name": event_type,
        "timestamp": raw_event.get("timestamp"),
        "payload": raw_event.get("payload"),
        "raw_type": raw_event.get("type"),
        "client_id": str(client_id),
        "virtual_window_id": str(window_id),
        "source_path": str(source_path),
        "offset": offset,
    }


def _session_id(raw_event: dict[str, Any], source_path: Path) -> str:
    fallback = source_path.stem.removeprefix("rollout-")
    suffix_match = _SESSION_ID_SUFFIX.search(fallback)
    if suffix_match is not None:
        fallback = suffix_match.group(1)

    payload = raw_event.get("payload")
    if isinstance(payload, dict):
        raw_id = payload.get("id")
        if isinstance(raw_id, str) and raw_id:
            return raw_id
    return fallback
