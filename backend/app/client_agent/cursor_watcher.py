from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def _content_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content if content else None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                if block:
                    parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            text = _string_value(block.get("text"))
            if text:
                parts.append(text)
        return "\n".join(parts) or None
    return None


def _message_text(payload: dict[str, Any]) -> str | None:
    text = _string_value(payload.get("text"))
    if text:
        return text
    if "content" in payload:
        content = _content_text(payload.get("content"))
        if content:
            return content
    message = payload.get("message")
    if isinstance(message, dict):
        return _message_text(message)
    return None


def _message_kind(role: str | None) -> str:
    if role == "user":
        return "user_message"
    if role == "assistant":
        return "assistant_message"
    if role == "system":
        return "system_message"
    return "message"


def _decode_meta(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if not isinstance(value, str):
            return {}
        decoded = json.loads(bytes.fromhex(value).decode("utf-8"))
    except (TypeError, AttributeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _parse_json_blob(data: bytes) -> dict[str, Any] | None:
    stripped = data.strip()
    if not stripped.startswith(b"{"):
        return None
    try:
        parsed = json.loads(stripped.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None

    role = _string_value(parsed.get("role"))
    if role not in {"user", "assistant", "system"}:
        return None
    if not _message_text(parsed):
        return None
    return parsed


def read_cursor_store_events(
    path: Path,
    *,
    seen_blob_ids: set[str],
    after_rowid: int = 0,
) -> tuple[list[dict[str, Any]], str | None, int]:
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.DatabaseError as exc:
        logger.info("Unable to open Cursor store %s: %s", path, exc)
        return [], None, after_rowid
    conn.row_factory = sqlite3.Row
    try:
        try:
            meta_row = conn.execute("select value from meta order by key limit 1").fetchone()
            if after_rowid > 0:
                blob_rows = list(
                    conn.execute(
                        "select id, data, rowid from blobs where rowid > ? order by rowid",
                        (after_rowid,),
                    )
                )
            else:
                blob_rows = list(conn.execute("select id, data, rowid from blobs order by rowid"))
        except sqlite3.DatabaseError as exc:
            logger.info("Unable to read Cursor store %s: %s", path, exc)
            return [], None, after_rowid

        meta = _decode_meta(meta_row["value"]) if meta_row is not None else {}
        agent_id = _string_value(meta.get("agentId")) or path.parent.name
        root_blob_id = _string_value(meta.get("latestRootBlobId"))
        events: list[dict[str, Any]] = []
        max_rowid = after_rowid
        for row in blob_rows:
            max_rowid = max(max_rowid, int(row["rowid"]))
            blob_id = str(row["id"])
            if blob_id in seen_blob_ids:
                continue
            data = row["data"]
            if isinstance(data, memoryview):
                data = data.tobytes()
            if isinstance(data, str):
                data = data.encode("utf-8")
            if not isinstance(data, bytes):
                continue
            parsed = _parse_json_blob(data)
            if parsed is None:
                continue
            role = _string_value(parsed.get("role")) or "event"
            text = _message_text(parsed)
            if not text:
                continue
            events.append(
                {
                    "provider": "cursor_cli",
                    "agentId": agent_id,
                    "chat_name": _string_value(meta.get("name")),
                    "createdAt": meta.get("createdAt"),
                    "lastUsedModel": meta.get("lastUsedModel"),
                    "root_blob_id": root_blob_id,
                    "blob_id": blob_id,
                    "role": role,
                    "type": _message_kind(role),
                    "text": text,
                    "raw_message": parsed,
                }
            )
        return events, root_blob_id, max_rowid
    finally:
        conn.close()
