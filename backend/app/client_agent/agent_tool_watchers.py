from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
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

if TYPE_CHECKING:
    from app.client_agent.agent_idle import AgentIdleSupervisor

ManagedEventSender = Callable[[AgentMessage], Awaitable[None]]
PresenceEventSender = Callable[[AgentMessage], Awaitable[None]]

AGENT_WATCH_ACTIVE_INTERVAL_SECONDS = 0.5
AGENT_WATCH_IDLE_INTERVAL_SECONDS = 2.0
AGENT_WATCH_MAX_INTERVAL_SECONDS = 5.0
AGENT_WATCH_SLOW_SCAN_SECONDS = 1.0
AGENT_WATCH_DISCOVERY_INTERVAL_SECONDS = 30.0
CODEX_ACTIVE_SESSION_BOOTSTRAP_SECONDS = 10 * 60
CLAUDE_HISTORY_PENDING_RETRY_SECONDS = 2.0
AGENT_WATCH_COLLECTION_CONCURRENCY = 2
AGENT_WATCH_PROCESS_SCAN_INTERVAL_SECONDS = 30.0
_WATCH_COLLECTION_SEMAPHORE: asyncio.Semaphore | None = None
_WATCH_COLLECTION_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None


def cursor_store_paths_for_window(window_id: UUID | str) -> list[Path]:
    root = Path.home() / ".web-terminal-acp" / "cursor-homes" / str(window_id)
    if not root.exists():
        return []
    return _find_files_without_directory_symlinks(root, "store.db")


def _find_files_without_directory_symlinks(root: Path, file_name: str) -> list[Path]:
    paths: list[Path] = []
    visited_dirs: set[tuple[int, int]] = set()
    for current_root, dir_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current_root)
        try:
            stat = current_path.stat()
        except OSError:
            dir_names[:] = []
            continue
        dir_key = (stat.st_dev, stat.st_ino)
        if dir_key in visited_dirs:
            dir_names[:] = []
            continue
        visited_dirs.add(dir_key)
        if file_name in file_names:
            path = current_path / file_name
            if path.is_file():
                paths.append(path)
    return sorted(paths)


def claude_code_home_for_window(window_id: UUID | str) -> Path:
    return Path.home() / ".web-terminal-acp" / "claude-code-homes" / str(window_id)


def claude_code_projects_dir(window_id: UUID | str) -> Path:
    return claude_code_home_for_window(window_id) / "projects"


def claude_code_history_file(window_id: UUID | str) -> Path:
    return claude_code_home_for_window(window_id) / "history.jsonl"


def iter_claude_code_jsonl_files(window_id: UUID | str) -> list[Path]:
    root = claude_code_projects_dir(window_id)
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*.jsonl") if path.is_file())


def iter_claude_code_transcript_files_for_session(window_id: UUID | str, session_id: str) -> list[Path]:
    root = claude_code_projects_dir(window_id)
    if not root.exists():
        return []
    return sorted(path for path in root.rglob(f"{session_id}.jsonl") if path.is_file())


def _initial_process_scan_delay(window_id: UUID, interval_seconds: float) -> float:
    if interval_seconds <= 0:
        return 0.0
    interval_ms = max(1, int(interval_seconds * 1000))
    stagger_seconds = (window_id.int % interval_ms) / 1000.0
    return min(AGENT_WATCH_IDLE_INTERVAL_SECONDS, interval_seconds) + stagger_seconds


@dataclass
class AgentToolWatcherState:
    codex_offsets: dict[Path, int] = field(default_factory=dict)
    codex_session_files: list[Path] = field(default_factory=list)
    codex_session_files_refreshed_at: float = 0.0
    claude_code_offsets: dict[Path, int] = field(default_factory=dict)
    claude_code_jsonl_files: list[Path] = field(default_factory=list)
    claude_code_jsonl_files_refreshed_at: float = 0.0
    claude_code_history_offset: int = 0
    claude_code_history_session_ids: set[str] = field(default_factory=set)
    claude_code_pending_history_session_ids: set[str] = field(default_factory=set)
    claude_code_pending_history_scanned_at: float = 0.0
    claude_code_history_jsonl_files: set[Path] = field(default_factory=set)
    cursor_store_paths: list[Path] = field(default_factory=list)
    cursor_seen_blob_ids: dict[Path, set[str]] = field(default_factory=dict)
    cursor_last_rowids: dict[Path, int] = field(default_factory=dict)
    cursor_discovery_started: bool = False


AGENT_TOOL_COLLECTORS: tuple[tuple[str, str], ...] = (
    ("codex", "collect_codex_watch_events"),
    ("claude_code", "collect_claude_code_watch_events"),
    ("cursor_cli", "collect_cursor_watch_events"),
)


@dataclass
class AgentToolWatchWindow:
    window_id: UUID
    project_path: str | None
    state: AgentToolWatcherState = field(default_factory=AgentToolWatcherState)
    initialized: bool = False
    sleep_seconds: float = AGENT_WATCH_IDLE_INTERVAL_SECONDS
    next_event_scan_at: float = 0.0
    next_process_scan_at: float = 0.0


class UnifiedAgentToolWatcher:
    def __init__(
        self,
        send_event: ManagedEventSender,
        client_id: UUID,
        *,
        send_presence: PresenceEventSender | None = None,
        terminal: ClientTerminalMultiplexer | None = None,
        runtime: ClientTmuxRuntime | None = None,
        idle_supervisor: AgentIdleSupervisor | None = None,
    ) -> None:
        self._send_event = send_event
        self._client_id = client_id
        self._send_presence = send_presence
        self._terminal = terminal
        self._runtime = runtime
        self._idle_supervisor = idle_supervisor
        self._windows: dict[UUID, AgentToolWatchWindow] = {}
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._process_scan_interval = max(
            PRESENCE_SEND_INTERVAL_SECONDS,
            AGENT_WATCH_PROCESS_SCAN_INTERVAL_SECONDS,
        )

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("unified agent tool watcher is closed")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def watch_window(self, window_id: UUID, project_path: str | None) -> None:
        existing = self._windows.get(window_id)
        if existing is not None:
            existing.project_path = project_path
            self._wakeup.set()
            return

        now = time.perf_counter()
        self._windows[window_id] = AgentToolWatchWindow(
            window_id=window_id,
            project_path=project_path,
            next_event_scan_at=now,
            next_process_scan_at=now
            + _initial_process_scan_delay(window_id, self._process_scan_interval),
        )
        self._wakeup.set()

    def remove_window(self, window_id: UUID) -> None:
        self._windows.pop(window_id, None)
        self._wakeup.set()

    async def close(self) -> None:
        self._closed = True
        self._wakeup.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            window = self._next_due_window()
            if window is None:
                await self._wait_for_due_window()
                continue
            try:
                await self._scan_window(window)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "client-agent unified agent watcher scan failed",
                    extra={
                        "client_id": str(self._client_id),
                        "window_id": str(window.window_id),
                    },
                )
                if self._windows.get(window.window_id) is window:
                    retry_at = time.perf_counter() + AGENT_WATCH_IDLE_INTERVAL_SECONDS
                    window.next_event_scan_at = retry_at
                    window.next_process_scan_at = max(window.next_process_scan_at, retry_at)

    def _next_due_window(self) -> AgentToolWatchWindow | None:
        if not self._windows:
            return None
        now = time.perf_counter()
        due_windows = [
            window
            for window in self._windows.values()
            if window.next_event_scan_at <= now or window.next_process_scan_at <= now
        ]
        if not due_windows:
            return None
        return min(due_windows, key=self._next_due_at)

    def _next_due_at(self, window: AgentToolWatchWindow) -> float:
        return min(window.next_event_scan_at, window.next_process_scan_at)

    async def _wait_for_due_window(self) -> None:
        self._wakeup.clear()
        if not self._windows:
            await self._wakeup.wait()
            return
        now = time.perf_counter()
        timeout = max(0.0, min(self._next_due_at(window) for window in self._windows.values()) - now)
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return

    async def _scan_window(self, window: AgentToolWatchWindow) -> None:
        window_id = window.window_id
        started_at = time.perf_counter()
        managed_sent_count = 0
        presence_sent_count = 0

        if not window.initialized:
            await _run_watcher_scan(
                initialize_agent_tool_watcher_state,
                window.state,
                window_id=window_id,
            )
            if self._windows.get(window_id) is not window:
                return
            window.initialized = True

        now = time.perf_counter()
        event_scan_due = now >= window.next_event_scan_at
        process_scan_due = now >= window.next_process_scan_at

        if event_scan_due:
            managed_events = await _run_watcher_scan(
                _collect_all_events,
                window.state,
                client_id=self._client_id,
                window_id=window_id,
                project_path=window.project_path,
            )
            if self._windows.get(window_id) is not window:
                return
            if self._idle_supervisor is not None and managed_events:
                await self._idle_supervisor.observe_events(managed_events)
        else:
            managed_events = []

        if self._windows.get(window_id) is not window:
            return

        now = time.perf_counter()
        process_scan_due = now >= window.next_process_scan_at
        if self._idle_supervisor is not None and process_scan_due:
            await self._idle_supervisor.maybe_suspend_window(window_id)

        if self._send_presence is not None and process_scan_due:
            presence = await detect_agent_work_presence(
                window_id,
                terminal=self._terminal,
                runtime=self._runtime,
            )
            if presence is not None:
                await self._send_presence(
                    AgentMessage(
                        type="agent_work_presence",
                        client_id=self._client_id,
                        window_id=window_id,
                        payload={
                            "providers": list(presence.providers),
                            "reasons": list(presence.reasons),
                        },
                    )
                )
                presence_sent_count += 1

        if self._windows.get(window_id) is not window:
            return

        for event in managed_events:
            if await enqueue_managed_ai_event(self._send_event, event):
                managed_sent_count += 1

        if self._windows.get(window_id) is not window:
            return

        if event_scan_due:
            if managed_sent_count:
                window.sleep_seconds = AGENT_WATCH_ACTIVE_INTERVAL_SECONDS
            else:
                window.sleep_seconds = min(
                    AGENT_WATCH_MAX_INTERVAL_SECONDS,
                    max(AGENT_WATCH_IDLE_INTERVAL_SECONDS, window.sleep_seconds * 1.5),
                )
            window.next_event_scan_at = time.perf_counter() + window.sleep_seconds

        if process_scan_due:
            window.next_process_scan_at = time.perf_counter() + self._process_scan_interval

        elapsed = time.perf_counter() - started_at
        if elapsed >= AGENT_WATCH_SLOW_SCAN_SECONDS:
            logger.warning(
                "client-agent unified agent watcher scan was slow",
                extra={
                    "client_id": str(self._client_id),
                    "window_id": str(window_id),
                    "managed_event_count": managed_sent_count,
                    "presence_event_count": presence_sent_count,
                    "elapsed_seconds": round(elapsed, 3),
                },
            )


def initialize_agent_tool_watcher_state(state: AgentToolWatcherState, *, window_id: UUID) -> None:
    state.codex_session_files = iter_codex_session_files(window_id)
    state.codex_session_files_refreshed_at = time.monotonic()
    bootstrap_codex_path = _recent_latest_codex_session_file(state.codex_session_files)
    for path in state.codex_session_files:
        try:
            state.codex_offsets[path] = 0 if path == bootstrap_codex_path else _jsonl_tail_resume_offset(path)
        except FileNotFoundError:
            state.codex_offsets.pop(path, None)

    state.claude_code_jsonl_files = iter_claude_code_jsonl_files(window_id)
    state.claude_code_jsonl_files_refreshed_at = time.monotonic()
    for path in state.claude_code_jsonl_files:
        try:
            state.claude_code_offsets[path] = _jsonl_tail_resume_offset(path)
        except FileNotFoundError:
            state.claude_code_offsets.pop(path, None)
    history_file = claude_code_history_file(window_id)
    try:
        state.claude_code_history_offset = _jsonl_tail_resume_offset(history_file)
    except FileNotFoundError:
        state.claude_code_history_offset = 0

    state.cursor_store_paths = cursor_store_paths_for_window(window_id)
    for path in state.cursor_store_paths:
        state.cursor_last_rowids[path] = _cursor_store_max_rowid(path)
        state.cursor_seen_blob_ids.setdefault(path, set())
    state.cursor_discovery_started = True


def _recent_latest_codex_session_file(paths: list[Path]) -> Path | None:
    latest_path: Path | None = None
    latest_mtime = 0.0
    now = time.time()
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if now - stat.st_mtime > CODEX_ACTIVE_SESSION_BOOTSTRAP_SECONDS:
            continue
        if latest_path is None or stat.st_mtime > latest_mtime:
            latest_path = path
            latest_mtime = stat.st_mtime
    return latest_path


def _jsonl_tail_resume_offset(path: Path) -> int:
    size = path.stat().st_size
    if size == 0:
        return 0

    with path.open("rb") as handle:
        handle.seek(size - 1)
        if handle.read(1) == b"\n":
            return size

        cursor = size
        while cursor > 0:
            chunk_size = min(8192, cursor)
            cursor -= chunk_size
            handle.seek(cursor)
            chunk = handle.read(chunk_size)
            newline_index = chunk.rfind(b"\n")
            if newline_index >= 0:
                return cursor + newline_index + 1
    return 0


def _cursor_store_max_rowid(path: Path) -> int:
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.DatabaseError:
        return 0
    try:
        try:
            row = conn.execute("select max(rowid) from blobs").fetchone()
        except sqlite3.DatabaseError:
            return 0
        value = row[0] if row is not None else None
        return int(value) if value is not None else 0
    finally:
        conn.close()


def _cached_codex_session_files(state: AgentToolWatcherState, window_id: UUID) -> list[Path]:
    now = time.monotonic()
    if state.codex_session_files_refreshed_at == 0.0 or (
        now - state.codex_session_files_refreshed_at >= AGENT_WATCH_DISCOVERY_INTERVAL_SECONDS
    ):
        state.codex_session_files = iter_codex_session_files(window_id)
        state.codex_session_files_refreshed_at = now
    return state.codex_session_files


def _cached_claude_code_jsonl_files(state: AgentToolWatcherState, window_id: UUID) -> list[Path]:
    now = time.monotonic()
    if state.claude_code_jsonl_files_refreshed_at == 0.0 or (
        now - state.claude_code_jsonl_files_refreshed_at >= AGENT_WATCH_DISCOVERY_INTERVAL_SECONDS
    ):
        state.claude_code_jsonl_files = iter_claude_code_jsonl_files(window_id)
        state.claude_code_jsonl_files_refreshed_at = now
    return sorted({*state.claude_code_jsonl_files, *state.claude_code_history_jsonl_files})


def _refresh_claude_code_history_sessions(state: AgentToolWatcherState, window_id: UUID) -> None:
    history_file = claude_code_history_file(window_id)
    offset = state.claude_code_history_offset
    try:
        session_ids, next_offset = read_claude_history_session_ids(history_file, offset)
    except FileNotFoundError:
        state.claude_code_history_offset = 0
        return

    state.claude_code_history_offset = next_offset
    new_pending_session_ids = session_ids - state.claude_code_history_session_ids
    state.claude_code_pending_history_session_ids.update(new_pending_session_ids)
    if not state.claude_code_pending_history_session_ids:
        return

    now = time.monotonic()
    pending_retry_due = (
        state.claude_code_pending_history_scanned_at == 0.0
        or now - state.claude_code_pending_history_scanned_at >= CLAUDE_HISTORY_PENDING_RETRY_SECONDS
    )
    if not new_pending_session_ids and not pending_retry_due:
        return
    state.claude_code_pending_history_scanned_at = now

    found_session_ids = _add_claude_code_history_transcripts(
        state,
        window_id=window_id,
        session_ids=state.claude_code_pending_history_session_ids,
        start_at_eof=False,
    )
    if found_session_ids:
        state.claude_code_pending_history_session_ids.difference_update(found_session_ids)
        state.claude_code_history_session_ids.update(found_session_ids)


def _add_claude_code_history_transcripts(
    state: AgentToolWatcherState,
    *,
    window_id: UUID,
    session_ids: set[str],
    start_at_eof: bool,
) -> set[str]:
    found_session_ids: set[str] = set()
    for session_id in sorted(session_ids):
        for path in iter_claude_code_transcript_files_for_session(window_id, session_id):
            if path not in state.claude_code_history_jsonl_files:
                state.claude_code_history_jsonl_files.add(path)
                if start_at_eof:
                    try:
                        state.claude_code_offsets.setdefault(path, path.stat().st_size)
                    except FileNotFoundError:
                        state.claude_code_offsets.pop(path, None)
                        continue
                else:
                    state.claude_code_offsets.setdefault(path, 0)
            found_session_ids.add(session_id)
    return found_session_ids


def read_claude_history_session_ids(path: Path, offset: int) -> tuple[set[str], int]:
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if offset > path.stat().st_size:
        offset = 0
    entries, next_offset = read_new_jsonl_events(path, offset)
    return {
        session_id
        for entry, _line_offset in entries
        if (session_id := _claude_history_session_id(entry)) is not None
    }, next_offset


def read_all_claude_history_session_ids(path: Path) -> set[str]:
    session_ids: set[str] = set()
    offset = 0
    while True:
        batch_session_ids, next_offset = read_claude_history_session_ids(path, offset)
        session_ids.update(batch_session_ids)
        if next_offset == offset or next_offset >= path.stat().st_size:
            return session_ids
        offset = next_offset


def _claude_history_session_id(entry: dict[str, Any]) -> str | None:
    value = entry.get("sessionId")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def collect_codex_watch_events(
    state: AgentToolWatcherState,
    *,
    client_id: UUID,
    window_id: UUID,
    project_path: str | None,
) -> list[ManagedAiEvent]:
    events: list[ManagedAiEvent] = []
    for path in _cached_codex_session_files(state, window_id):
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
    _refresh_claude_code_history_sessions(state, window_id)
    events: list[ManagedAiEvent] = []
    for path in _cached_claude_code_jsonl_files(state, window_id):
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
            if state.cursor_discovery_started:
                state.cursor_last_rowids[path] = _cursor_store_max_rowid(path)
            state.cursor_seen_blob_ids.setdefault(path, set())
    state.cursor_discovery_started = True

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
    idle_supervisor: AgentIdleSupervisor | None = None,
) -> None:
    state = AgentToolWatcherState()
    sleep_seconds = AGENT_WATCH_IDLE_INTERVAL_SECONDS
    await _run_watcher_scan(initialize_agent_tool_watcher_state, state, window_id=window_id)
    process_scan_interval = max(PRESENCE_SEND_INTERVAL_SECONDS, AGENT_WATCH_PROCESS_SCAN_INTERVAL_SECONDS)
    next_process_scan_at = time.perf_counter() + _initial_process_scan_delay(window_id, process_scan_interval)
    while True:
        started_at = time.perf_counter()
        managed_events = await _run_watcher_scan(
            _collect_all_events,
            state,
            client_id=client_id,
            window_id=window_id,
            project_path=project_path,
        )
        sent_count = 0
        if idle_supervisor is not None and managed_events:
            await idle_supervisor.observe_events(managed_events)
        for event in managed_events:
            if await enqueue_managed_ai_event(send_event, event):
                sent_count += 1
        now = time.perf_counter()
        process_scan_due = now >= next_process_scan_at
        if idle_supervisor is not None and process_scan_due:
            await idle_supervisor.maybe_suspend_window(window_id)

        if send_presence is not None and process_scan_due:
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
                sent_count += 1
        if process_scan_due:
            next_process_scan_at = now + process_scan_interval

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


def _watch_collection_semaphore() -> asyncio.Semaphore:
    global _WATCH_COLLECTION_SEMAPHORE, _WATCH_COLLECTION_SEMAPHORE_LOOP

    loop = asyncio.get_running_loop()
    if _WATCH_COLLECTION_SEMAPHORE is None or _WATCH_COLLECTION_SEMAPHORE_LOOP is not loop:
        _WATCH_COLLECTION_SEMAPHORE = asyncio.Semaphore(AGENT_WATCH_COLLECTION_CONCURRENCY)
        _WATCH_COLLECTION_SEMAPHORE_LOOP = loop
    return _WATCH_COLLECTION_SEMAPHORE


async def _run_watcher_scan(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    async with _watch_collection_semaphore():
        return await asyncio.to_thread(func, *args, **kwargs)


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
