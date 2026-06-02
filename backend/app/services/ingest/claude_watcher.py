from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID

from elasticsearch import AsyncElasticsearch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import prefer_deferred_commit
from app.models import Event, EventSourceType, VirtualWindow
from app.repositories.ai_sessions import get_or_create_ai_session
from app.repositories.clients import ensure_local_client
from app.repositories.events import insert_normalized_event
from app.services.summary_scheduler import schedule_summary_after_agent_activity
from app.services.ingest.normalizers import normalize_claude_jsonl
from app.services.search_index import index_ai_event
from app.services.ui_events import UiEventHub

logger = logging.getLogger(__name__)


SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
DEFAULT_READ_MAX_EVENTS = 25
DEFAULT_READ_MAX_BYTES = 1024 * 1024
DEFAULT_INDEX_BATCH_SIZE = 25
DEFAULT_MAX_CHANGED_FILES_PER_PASS = 1
DEFAULT_KNOWN_FILE_SCAN_LIMIT = 128
DEFAULT_DISCOVERY_INTERVAL_SECONDS = 300.0


async def _run_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def iter_jsonl_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.jsonl") if path.is_file())


def initial_jsonl_offsets(root: Path) -> dict[Path, int]:
    offsets: dict[Path, int] = {}
    for path in iter_jsonl_files(root):
        try:
            offsets[path] = path.stat().st_size
        except FileNotFoundError:
            continue
    return offsets


def _changed_jsonl_offsets(
    root: Path,
    offsets: dict[Path, int],
    *,
    max_changed_files: int,
    paths: list[Path] | None = None,
    scan_cursor: int = 0,
    max_files: int | None = None,
) -> tuple[list[tuple[Path, int]], list[Path], int]:
    discovered_paths = iter_jsonl_files(root) if paths is None else list(paths)
    if not discovered_paths:
        return [], discovered_paths, 0

    total_paths = len(discovered_paths)
    scan_count = total_paths if max_files is None else min(max_files, total_paths)
    cursor = scan_cursor % total_paths
    changed: list[tuple[Path, int]] = []
    missing_paths: set[Path] = set()
    scanned = 0
    while scanned < scan_count:
        path = discovered_paths[cursor]
        offset = offsets.get(path, 0)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            offsets.pop(path, None)
            missing_paths.add(path)
            cursor = (cursor + 1) % total_paths
            scanned += 1
            continue
        if size < offset:
            offset = 0
        if size != offset:
            changed.append((path, offset))
            if len(changed) >= max_changed_files:
                cursor = (cursor + 1) % total_paths
                if missing_paths:
                    discovered_paths = [candidate for candidate in discovered_paths if candidate not in missing_paths]
                    cursor = cursor % len(discovered_paths) if discovered_paths else 0
                return changed, discovered_paths, cursor
        cursor = (cursor + 1) % total_paths
        scanned += 1
    if missing_paths:
        discovered_paths = [candidate for candidate in discovered_paths if candidate not in missing_paths]
        cursor = cursor % len(discovered_paths) if discovered_paths else 0
    return changed, discovered_paths, cursor


def _skip_overlong_jsonl_line(file, line_start: int, first_chunk: bytes, max_line_bytes: int) -> int | None:
    """Return the offset after an overlong complete line, or None for an incomplete line."""
    bytes_consumed = len(first_chunk)
    if first_chunk.endswith(b"\n"):
        return line_start + bytes_consumed

    while True:
        chunk = file.readline(max_line_bytes + 1)
        if chunk == b"":
            return None

        bytes_consumed += len(chunk)
        if chunk.endswith(b"\n"):
            return line_start + bytes_consumed


def read_new_jsonl_events(
    path: Path,
    offset: int,
    *,
    max_events: int | None = None,
    max_bytes: int | None = None,
) -> tuple[list[tuple[dict[str, Any], int]], int]:
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if max_events is not None and max_events <= 0:
        raise ValueError("max_events must be positive")
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    events: list[tuple[dict[str, Any], int]] = []
    next_offset = offset
    bytes_read = 0
    max_line_bytes = max_bytes if max_bytes is not None else DEFAULT_READ_MAX_BYTES

    with path.open("rb") as file:
        file.seek(offset)
        while True:
            if max_events is not None and len(events) >= max_events:
                return events, next_offset

            line_start = next_offset
            line = file.readline(max_line_bytes + 1)
            if line == b"":
                return events, next_offset

            if len(line) > max_line_bytes:
                overlong_line_end = _skip_overlong_jsonl_line(file, line_start, line, max_line_bytes)
                if overlong_line_end is None:
                    return events, line_start

                logger.warning("Skipping overlong Claude JSONL line", extra={"path": str(path), "offset": line_start})
                next_offset = overlong_line_end
                bytes_read += overlong_line_end - line_start
                continue

            line_end = line_start + len(line)
            if not line.endswith(b"\n"):
                return events, line_start

            if max_bytes is not None and bytes_read > 0 and bytes_read + len(line) > max_bytes:
                return events, next_offset

            next_offset = line_end
            bytes_read += len(line)
            stripped = line.strip()
            if not stripped:
                continue

            try:
                parsed = json.loads(stripped.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("Skipping invalid Claude JSONL line", extra={"path": str(path), "offset": line_start})
                continue

            if not isinstance(parsed, dict):
                logger.warning(
                    "Skipping non-object Claude JSONL line",
                    extra={"path": str(path), "offset": line_start},
                )
                continue

            events.append((parsed, line_start))


async def _payload_virtual_window_id(
    session: AsyncSession,
    payload: dict[str, Any],
    client_id: UUID,
) -> UUID | None:
    raw_value = payload.get("virtual_window_id") or payload.get("virtualWindowId")
    if not isinstance(raw_value, str):
        return None

    try:
        window_id = UUID(raw_value)
    except ValueError:
        return None

    window = await session.get(VirtualWindow, window_id)
    if window is None or window.client_id != client_id:
        return None
    return window_id


async def ingest_claude_jsonl_file(
    session: AsyncSession,
    path: Path,
    offset: int,
    es_client: AsyncElasticsearch | None = None,
    *,
    max_events: int | None = None,
    max_bytes: int | None = None,
) -> int:
    """Persist new Claude JSONL events and return the next byte offset.

    The optional es_client is accepted for backward-compatible call sites, but
    indexing is deliberately performed after the caller commits persisted rows.
    """
    _ = es_client
    events, next_offset = await _run_blocking(
        read_new_jsonl_events,
        path,
        offset,
        max_events=max_events,
        max_bytes=max_bytes,
    )
    local_client = await ensure_local_client(session)

    for payload, line_offset in events:
        normalized = normalize_claude_jsonl(payload, source_path=str(path), offset=line_offset)
        virtual_window_id = await _payload_virtual_window_id(session, payload, local_client.id)
        row = await insert_normalized_event(
            session,
            normalized,
            client_id=local_client.id,
            virtual_window_id=virtual_window_id,
        )

        if row.virtual_window_id is None:
            row.virtual_window_id = virtual_window_id

        ai_session = await get_or_create_ai_session(
            session,
            client_id=local_client.id,
            provider="claude",
            source_id=normalized.source_id,
            source_path=str(path),
            virtual_window_id=row.virtual_window_id,
        )
        row.ai_session_id = ai_session.id
        if row.virtual_window_id is not None:
            window = await session.get(VirtualWindow, row.virtual_window_id)
            if window is not None:
                await schedule_summary_after_agent_activity(session, window, event=row)

    await session.flush()
    return next_offset


async def _index_claude_event(es_client: AsyncElasticsearch, row: Event) -> None:
    normalized = normalize_claude_jsonl(row.payload_json, source_path=row.source_id, offset=0)
    await index_ai_event(
        es_client,
        row.client_id,
        provider="claude",
        session_id=row.source_id,
        kind=row.kind,
        text=normalized.text,
        raw=row.payload_json,
        virtual_window_id=row.virtual_window_id,
        document_id=str(row.id),
    )


async def index_claude_events(
    session: AsyncSession,
    es_client: AsyncElasticsearch,
    *,
    limit: int = DEFAULT_INDEX_BATCH_SIZE,
) -> int:
    """Index committed Claude rows missing indexed_at, leaving failures retryable."""
    rows = (
        await session.execute(
            select(Event)
            .where(Event.source_type == EventSourceType.claude_jsonl, Event.indexed_at.is_(None))
            .order_by(Event.created_at, Event.id)
            .limit(limit)
        )
    ).scalars()

    indexed_count = 0
    for row in rows:
        try:
            await _index_claude_event(es_client, row)
        except Exception:
            logger.exception("Failed to index Claude JSONL event", extra={"fingerprint": row.fingerprint})
            continue

        row.indexed_at = datetime.now(UTC)
        indexed_count += 1

    await session.flush()
    return indexed_count


async def poll_claude_jsonl_directory_once(
    session_factory: SessionFactory,
    root: Path,
    offsets: dict[Path, int],
    *,
    es_client: AsyncElasticsearch | None = None,
    ui_event_hub: UiEventHub | None = None,
    max_events: int = DEFAULT_READ_MAX_EVENTS,
    max_bytes: int = DEFAULT_READ_MAX_BYTES,
    max_changed_files: int = DEFAULT_MAX_CHANGED_FILES_PER_PASS,
    known_paths: list[Path] | None = None,
    scan_cursor: int = 0,
    max_files: int | None = None,
    discover_new_files: bool = True,
) -> int:
    """Run one bounded Claude JSONL poll pass for tests and scheduled loops."""
    changed_files, discovered_paths, next_scan_cursor = await _run_blocking(
        _changed_jsonl_offsets,
        root,
        offsets,
        max_changed_files=max_changed_files,
        paths=None if discover_new_files or known_paths is None else known_paths,
        scan_cursor=scan_cursor,
        max_files=max_files,
    )
    if known_paths is not None:
        known_paths[:] = discovered_paths
    for path, offset in changed_files:
        try:
            async with session_factory() as session:
                local_client = await ensure_local_client(session)
                await prefer_deferred_commit(session)
                offsets[path] = await ingest_claude_jsonl_file(
                    session,
                    path,
                    offset,
                    max_events=max_events,
                    max_bytes=max_bytes,
                )
                await session.commit()
                if ui_event_hub is not None and offsets[path] != offset:
                    await ui_event_hub.publish_invalidation(
                        ["agent_record", "window", "search"],
                        client_id=local_client.id,
                        reason="claude_jsonl_ingested",
                    )
        except Exception:
            logger.exception("Failed to ingest Claude JSONL file", extra={"path": str(path)})
        await asyncio.sleep(0)

    if es_client is None:
        return next_scan_cursor

    try:
        async with session_factory() as session:
            indexed_count = await index_claude_events(session, es_client)
            await session.commit()
            if ui_event_hub is not None and indexed_count:
                await ui_event_hub.publish_invalidation(
                    ["search"],
                    reason="claude_jsonl_indexed",
                )
    except Exception:
        logger.exception("Failed to reconcile Claude JSONL search index")
    return next_scan_cursor


async def poll_claude_jsonl_directory(
    session_factory: SessionFactory,
    root: Path,
    interval_seconds: float = 2.0,
    es_client: AsyncElasticsearch | None = None,
    ui_event_hub: UiEventHub | None = None,
) -> None:
    offsets = await _run_blocking(initial_jsonl_offsets, root)
    known_paths = sorted(offsets)
    scan_cursor = 0
    next_discovery_at = monotonic() + DEFAULT_DISCOVERY_INTERVAL_SECONDS

    while True:
        try:
            now = monotonic()
            discover_new_files = now >= next_discovery_at or not known_paths
            scan_cursor = await poll_claude_jsonl_directory_once(
                session_factory,
                root,
                offsets,
                es_client=es_client,
                ui_event_hub=ui_event_hub,
                known_paths=known_paths,
                scan_cursor=scan_cursor,
                max_files=DEFAULT_KNOWN_FILE_SCAN_LIMIT,
                discover_new_files=discover_new_files,
            )
            if discover_new_files:
                next_discovery_at = now + DEFAULT_DISCOVERY_INTERVAL_SECONDS
        except Exception:
            logger.exception("Failed to poll Claude JSONL directory", extra={"root": str(root)})
        await asyncio.sleep(interval_seconds)
