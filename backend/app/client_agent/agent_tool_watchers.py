from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from app.client_agent.agent_work_presence import (
    PRESENCE_SEND_INTERVAL_SECONDS,
    detect_agent_work_presence,
)
from app.client_agent.ai_events import ManagedAiEvent, managed_event_from_payload
from app.client_agent.codex_watcher import iter_codex_session_files, read_new_codex_events
from app.client_agent.cursor_watcher import read_cursor_store_events
from app.client_agent.terminal import ClientTerminalMultiplexer
from app.client_agent.tmux_runtime import ClientTmuxRuntime
from app.services.runtime.protocol import AgentMessage

logger = logging.getLogger(__name__)

ManagedEventSender = Callable[[AgentMessage], Awaitable[None]]
PresenceEventSender = Callable[[AgentMessage], Awaitable[None]]

AGENT_WATCH_ACTIVE_INTERVAL_SECONDS = 0.5
AGENT_WATCH_IDLE_INTERVAL_SECONDS = 2.0
AGENT_WATCH_MAX_INTERVAL_SECONDS = 5.0
AGENT_WATCH_SLOW_SCAN_SECONDS = 1.0


def cursor_store_paths_for_window(window_id: UUID | str) -> list[Path]:
    root = Path.home() / ".web-terminal-acp" / "cursor-homes" / str(window_id)
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("store.db") if path.is_file())


def claude_code_projects_dir(window_id: UUID | str) -> Path:
    return Path.home() / ".web-terminal-acp" / "claude-code-homes" / str(window_id) / "projects"


def iter_claude_code_jsonl_files(window_id: UUID | str) -> list[Path]:
    root = claude_code_projects_dir(window_id)
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.jsonl") if path.is_file())


@dataclass
class AgentToolWatcherState:
    codex_offsets: dict[Path, int] = field(default_factory=dict)
    claude_code_offsets: dict[Path, int] = field(default_factory=dict)
    cursor_store_paths: list[Path] = field(default_factory=list)
    cursor_seen_blob_ids: dict[Path, set[str]] = field(default_factory=dict)
    cursor_last_rowids: dict[Path, int] = field(default_factory=dict)


AGENT_TOOL_COLLECTORS: tuple[tuple[str, str], ...] = (
    ("codex", "collect_codex_watch_events"),
    ("claude_code", "collect_claude_code_watch_events"),
    ("cursor_cli", "collect_cursor_watch_events"),
)


def collect_codex_watch_events(
    state: AgentToolWatcherState,
    *,
    client_id: UUID,
    window_id: UUID,
    project_path: str | None,
) -> list[ManagedAiEvent]:
    events: list[ManagedAiEvent] = []
    for path in iter_codex_session_files(window_id):
        offset = state.codex_offsets.get(path, 0)
        try:
            if offset > path.stat().st_size:
                offset = 0
            payloads, next_offset = read_new_codex_events(
                path,
                offset,
                client_id=client_id,
                window_id=window_id,
            )
        except FileNotFoundError:
            state.codex_offsets.pop(path, None)
            continue
        state.codex_offsets[path] = next_offset
        for payload, line_offset in payloads:
            payload["project_path"] = project_path
            events.append(
                ManagedAiEvent(
                    provider="codex",
                    client_id=client_id,
                    window_id=window_id,
                    source_path=str(path),
                    offset=line_offset,
                    cursor=line_offset,
                    project_path=project_path,
                    payload=payload,
                )
            )
    return events


def collect_claude_code_watch_events(
    state: AgentToolWatcherState,
    *,
    client_id: UUID,
    window_id: UUID,
    project_path: str | None,
) -> list[ManagedAiEvent]:
    events: list[ManagedAiEvent] = []
    for path in iter_claude_code_jsonl_files(window_id):
        offset = state.claude_code_offsets.get(path, 0)
        try:
            if offset > path.stat().st_size:
                offset = 0
            payloads, next_offset = read_new_jsonl_events(path, offset)
        except FileNotFoundError:
            state.claude_code_offsets.pop(path, None)
            continue
        state.claude_code_offsets[path] = next_offset
        for payload, line_offset in payloads:
            payload.setdefault("WEB_TERMINAL_CLIENT_ID", str(client_id))
            payload.setdefault("WEB_TERMINAL_WINDOW_ID", str(window_id))
            payload.setdefault("WEB_TERMINAL_PROJECT_PATH", project_path or "")
            events.append(
                ManagedAiEvent(
                    provider="claude_code",
                    client_id=client_id,
                    window_id=window_id,
                    source_path=str(path),
                    offset=line_offset,
                    cursor=line_offset,
                    project_path=project_path,
                    payload=payload,
                )
            )
    return events


def read_new_jsonl_events(
    path: Path,
    offset: int,
    *,
    max_events: int = 100,
) -> tuple[list[tuple[dict[str, Any], int]], int]:
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if max_events < 1:
        raise ValueError("max_events must be at least 1")

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
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("Skipping invalid Claude Code JSONL line", extra={"path": str(path), "offset": line_offset})
                continue
            if isinstance(payload, dict):
                events.append((payload, line_offset))
    return events, next_offset


def collect_cursor_watch_events(
    state: AgentToolWatcherState,
    *,
    client_id: UUID,
    window_id: UUID,
    project_path: str | None,
) -> list[ManagedAiEvent]:
    known_paths = set(state.cursor_store_paths)
    for path in cursor_store_paths_for_window(window_id):
        if path not in known_paths:
            state.cursor_store_paths.append(path)
            known_paths.add(path)

    events: list[ManagedAiEvent] = []
    for path in state.cursor_store_paths:
        seen = state.cursor_seen_blob_ids.setdefault(path, set())
        after_rowid = state.cursor_last_rowids.get(path, 0)
        try:
            payloads, root_blob_id, max_rowid = read_cursor_store_events(
                path,
                seen_blob_ids=seen,
                after_rowid=after_rowid,
            )
            state.cursor_last_rowids[path] = max_rowid
        except sqlite3.Error:
            logger.exception(
                "failed to read cursor cli store",
                extra={"path": str(path), "window_id": str(window_id)},
            )
            continue
        for payload in payloads:
            blob_id = payload.get("blob_id")
            if blob_id is not None:
                seen.add(str(blob_id))
            payload["client_id"] = str(client_id)
            payload["virtual_window_id"] = str(window_id)
            payload["project_path"] = project_path
            events.append(
                ManagedAiEvent(
                    provider="cursor_cli",
                    client_id=client_id,
                    window_id=window_id,
                    source_path=str(path),
                    offset=None,
                    cursor=root_blob_id,
                    project_path=project_path,
                    payload=payload,
                )
            )
    return events


async def watch_agent_tool_events(
    send_event: ManagedEventSender,
    client_id: UUID,
    window_id: UUID,
    project_path: str | None,
    *,
    send_presence: PresenceEventSender | None = None,
    terminal: ClientTerminalMultiplexer | None = None,
    runtime: ClientTmuxRuntime | None = None,
) -> None:
    state = AgentToolWatcherState()
    sleep_seconds = AGENT_WATCH_IDLE_INTERVAL_SECONDS
    last_presence_sent_at = 0.0
    while True:
        started_at = time.perf_counter()
        managed_events = await asyncio.to_thread(
            _collect_all_events,
            state,
            client_id=client_id,
            window_id=window_id,
            project_path=project_path,
        )
        sent_count = 0
        for event in managed_events:
            if await enqueue_managed_ai_event(send_event, event):
                sent_count += 1

        if send_presence is not None:
            now = time.perf_counter()
            if now - last_presence_sent_at >= PRESENCE_SEND_INTERVAL_SECONDS:
                presence = await detect_agent_work_presence(
                    window_id,
                    terminal=terminal,
                    runtime=runtime,
                )
                if presence is not None:
                    await send_presence(
                        AgentMessage(
                            type="agent_work_presence",
                            client_id=client_id,
                            window_id=window_id,
                            payload={
                                "providers": list(presence.providers),
                                "reasons": list(presence.reasons),
                            },
                        )
                    )
                    last_presence_sent_at = now
                    sent_count += 1

        elapsed = time.perf_counter() - started_at
        if elapsed >= AGENT_WATCH_SLOW_SCAN_SECONDS:
            logger.warning(
                "client-agent agent watcher scan was slow",
                extra={
                    "client_id": str(client_id),
                    "window_id": str(window_id),
                    "event_count": sent_count,
                    "elapsed_seconds": round(elapsed, 3),
                },
            )

        if sent_count:
            sleep_seconds = AGENT_WATCH_ACTIVE_INTERVAL_SECONDS
        else:
            sleep_seconds = min(
                AGENT_WATCH_MAX_INTERVAL_SECONDS,
                max(AGENT_WATCH_IDLE_INTERVAL_SECONDS, sleep_seconds * 1.5),
            )
        await asyncio.sleep(sleep_seconds)




def _collect_all_events(
    state: AgentToolWatcherState,
    *,
    client_id: UUID,
    window_id: UUID,
    project_path: str | None,
) -> list[ManagedAiEvent]:
    events: list[ManagedAiEvent] = []
    for _, collector_name in AGENT_TOOL_COLLECTORS:
        collector = globals()[collector_name]
        events.extend(
            collector(
                state,
                client_id=client_id,
                window_id=window_id,
                project_path=project_path,
            )
        )
    return events


async def enqueue_managed_ai_event(send_event: ManagedEventSender, event: ManagedAiEvent) -> bool:
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
        return False

    await send_event(
        AgentMessage(
            type="ai_event",
            client_id=validated_event.client_id,
            window_id=validated_event.window_id,
            payload={
                "provider": validated_event.provider,
                "source_path": validated_event.source_path,
                "offset": validated_event.offset,
                "cursor": validated_event.cursor,
                "project_path": validated_event.project_path,
                "payload": validated_event.payload,
            },
        )
    )
    return True
