from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import signal
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from app.client_agent.agent_commands import format_agent_command
from app.client_agent.agent_work_presence import (
    AgentProcess,
    _build_parent_map,
    _descendant_pids,
    detect_agent_processes,
)
from app.client_agent.ai_events import ManagedAiEvent
from app.client_agent.agent_tool_watchers import (
    cursor_store_paths_for_window,
    iter_claude_code_jsonl_files,
)
from app.client_agent.codex_watcher import iter_codex_session_files
from app.client_agent.terminal import ClientTerminalMultiplexer
from app.client_agent.tmux_runtime import ClientTmuxRuntime

logger = logging.getLogger(__name__)

AGENT_IDLE_SUSPEND_SECONDS = 60 * 60
AGENT_TERMINATE_GRACE_SECONDS = 5.0

_SESSION_ID_SUFFIX = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)

_DEFAULT_COMMANDS = {
    "claude_code": "claude",
    "codex": "codex",
    "cursor_cli": "agent",
}

_CURSOR_COMMANDS = {"agent", "cursor", "cursor-agent"}
_PROVIDER_COMMANDS = {
    "claude_code": {"claude"},
    "codex": {"codex"},
    "cursor_cli": _CURSOR_COMMANDS,
}
_CLAUDE_LOCAL_COMMAND_PREFIXES = (
    "<local-command-caveat>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)


@dataclass(frozen=True)
class AgentSessionRef:
    provider: str
    session_id: str
    source_path: str | None
    last_output_at: float
    claude_worktree_name: str | None = None
    claude_worktree_original_cwd: str | None = None


@dataclass
class AgentProviderState:
    last_output_at: float | None = None
    session: AgentSessionRef | None = None
    project_path: str | None = None


@dataclass(frozen=True)
class SuspendedAgent:
    provider: str
    session_id: str
    command_name: str
    cwd: str | None
    source_path: str | None
    last_output_at: float
    suspended_at: float
    claude_worktree_name: str | None = None
    claude_worktree_original_cwd: str | None = None


ProcessDetector = Callable[
    [UUID, ClientTerminalMultiplexer | None, ClientTmuxRuntime | None],
    Awaitable[dict[str, tuple[AgentProcess, ...]]],
]
ProcessTerminator = Callable[[tuple[AgentProcess, ...]], Awaitable[None]]


async def default_process_detector(
    window_id: UUID,
    terminal: ClientTerminalMultiplexer | None,
    runtime: ClientTmuxRuntime | None,
) -> dict[str, tuple[AgentProcess, ...]]:
    return await detect_agent_processes(window_id, terminal=terminal, runtime=runtime)


class AgentIdleSupervisor:
    def __init__(
        self,
        *,
        terminal: ClientTerminalMultiplexer,
        runtime: ClientTmuxRuntime,
        idle_seconds: float = AGENT_IDLE_SUSPEND_SECONDS,
        suspension_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
        process_detector: ProcessDetector = default_process_detector,
        process_terminator: ProcessTerminator | None = None,
    ) -> None:
        self._terminal = terminal
        self._runtime = runtime
        self._idle_seconds = idle_seconds
        self._suspension_dir = suspension_dir or (
            Path.home() / ".web-terminal-acp" / "agent-suspensions"
        )
        self._clock = clock
        self._process_detector = process_detector
        self._process_terminator = process_terminator or terminate_agent_processes
        self._states: dict[tuple[UUID, str], AgentProviderState] = {}
        self._window_project_paths: dict[UUID, str | None] = {}
        self._attached_views: dict[UUID, UUID] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}

    def register_window(self, window_id: UUID, project_path: str | None) -> None:
        self._window_project_paths[window_id] = project_path

    def remove_window(self, window_id: UUID) -> None:
        self._window_project_paths.pop(window_id, None)
        for key in [key for key in self._states if key[0] == window_id]:
            self._states.pop(key, None)
        for view_id, attached_window_id in tuple(self._attached_views.items()):
            if attached_window_id == window_id:
                self._attached_views.pop(view_id, None)
        self._record_path(window_id).unlink(missing_ok=True)

    def attach_view(self, view_id: UUID, window_id: UUID) -> None:
        self._attached_views[view_id] = window_id

    def detach_view(self, view_id: UUID) -> None:
        self._attached_views.pop(view_id, None)

    async def observe_events(self, events: list[ManagedAiEvent]) -> None:
        for event in events:
            session = session_ref_from_event(event)
            if session is None:
                continue
            state = self._state_for(event.window_id, event.provider)
            state.project_path = event.project_path or state.project_path
            session = _merge_session_metadata(state.session, session)
            if state.last_output_at is None or session.last_output_at >= state.last_output_at:
                state.last_output_at = session.last_output_at
                state.session = session

    async def maybe_suspend_window(self, window_id: UUID) -> None:
        if self._is_attached(window_id):
            return

        lock = self._lock_for(window_id)
        async with lock:
            if self._is_attached(window_id):
                return
            processes_by_provider = await self._process_detector(
                window_id,
                self._terminal,
                self._runtime,
            )
            if not processes_by_provider:
                return
            for provider, processes in processes_by_provider.items():
                await self._maybe_suspend_provider(window_id, provider, processes)

    async def resume_window(self, window_id: UUID, *, allow_latest_session: bool = False) -> None:
        lock = self._lock_for(window_id)
        async with lock:
            records = self._load_suspended_agents(window_id)
            if not records:
                if allow_latest_session:
                    await self._resume_latest_session(window_id)
                return

            tmux_target = self._terminal.tmux_target_for(window_id)
            if tmux_target is None:
                return

            remaining: list[SuspendedAgent] = []
            for record in records:
                command = resume_command(record)
                if command is None:
                    remaining.append(record)
                    continue
                await self._runtime._run(
                    [
                        "tmux",
                        "send-keys",
                        "-t",
                        tmux_target,
                        "--",
                        command,
                        "C-m",
                    ]
                )
                self._state_for(window_id, record.provider).last_output_at = self._clock()

            if remaining:
                self._save_suspended_agents(window_id, remaining)
            else:
                self._record_path(window_id).unlink(missing_ok=True)

    async def _resume_latest_session(self, window_id: UUID) -> None:
        command = latest_resume_command(
            window_id,
            project_path=self._window_project_paths.get(window_id),
        )
        if command is None:
            return
        tmux_target = self._terminal.tmux_target_for(window_id)
        if tmux_target is None:
            return
        await self._runtime._run(
            [
                "tmux",
                "send-keys",
                "-t",
                tmux_target,
                "--",
                command,
                "C-m",
            ]
        )

    async def _maybe_suspend_provider(
        self,
        window_id: UUID,
        provider: str,
        processes: tuple[AgentProcess, ...],
    ) -> None:
        state = self._state_for(window_id, provider)
        if state.session is None or state.last_output_at is None:
            session = latest_session_ref(window_id, provider)
            if session is None:
                return
            state.session = session
            state.last_output_at = session.last_output_at

        now = self._clock()
        if now - state.last_output_at < self._idle_seconds:
            return

        record = suspended_agent_from_process(
            state.session,
            processes,
            project_path=state.project_path or self._window_project_paths.get(window_id),
            suspended_at=now,
        )
        if record is None:
            return

        records = [item for item in self._load_suspended_agents(window_id) if item.provider != provider]
        records.append(record)
        self._save_suspended_agents(window_id, records)
        await self._process_terminator(processes)
        logger.info(
            "suspended idle agent",
            extra={
                "window_id": str(window_id),
                "provider": provider,
                "session_id": record.session_id,
                "idle_seconds": round(now - state.last_output_at, 3),
            },
        )

    def _is_attached(self, window_id: UUID) -> bool:
        return window_id in set(self._attached_views.values())

    def _lock_for(self, window_id: UUID) -> asyncio.Lock:
        lock = self._locks.get(window_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[window_id] = lock
        return lock

    def _state_for(self, window_id: UUID, provider: str) -> AgentProviderState:
        key = (window_id, provider)
        state = self._states.get(key)
        if state is None:
            state = AgentProviderState(project_path=self._window_project_paths.get(window_id))
            self._states[key] = state
        return state

    def _record_path(self, window_id: UUID) -> Path:
        return self._suspension_dir / f"{window_id}.json"

    def _load_suspended_agents(self, window_id: UUID) -> list[SuspendedAgent]:
        path = self._record_path(window_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        items = raw.get("agents") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return []
        records: list[SuspendedAgent] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            record = _suspended_agent_from_dict(item)
            if record is not None:
                records.append(record)
        return records

    def _save_suspended_agents(self, window_id: UUID, records: list[SuspendedAgent]) -> None:
        self._suspension_dir.mkdir(parents=True, exist_ok=True)
        path = self._record_path(window_id)
        tmp_path = path.with_suffix(".tmp")
        payload = {
            "window_id": str(window_id),
            "agents": [asdict(record) for record in records],
        }
        tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(path)


def session_ref_from_event(event: ManagedAiEvent) -> AgentSessionRef | None:
    if event.provider == "claude_code" and _claude_local_command_payload(event.payload):
        return None

    session_id = session_id_from_payload(event.provider, event.payload, event.source_path)
    if session_id is None:
        return None

    claude_worktree_name, claude_worktree_original_cwd = claude_worktree_metadata_from_payload(
        event.provider,
        event.payload,
    )
    last_output_at = _event_output_time(event.source_path)
    return AgentSessionRef(
        provider=event.provider,
        session_id=session_id,
        source_path=event.source_path,
        last_output_at=last_output_at,
        claude_worktree_name=claude_worktree_name,
        claude_worktree_original_cwd=claude_worktree_original_cwd,
    )


def latest_session_ref(window_id: UUID, provider: str) -> AgentSessionRef | None:
    if provider == "claude_code":
        return latest_claude_session_ref(window_id)
    if provider == "codex":
        return latest_codex_session_ref(window_id)
    if provider == "cursor_cli":
        return latest_cursor_session_ref(window_id)
    return None


def latest_resume_command(window_id: UUID, *, project_path: str | None = None) -> str | None:
    candidates = [
        session
        for provider in _DEFAULT_COMMANDS
        if (session := latest_session_ref(window_id, provider)) is not None
    ]
    if not candidates:
        return None
    session = max(candidates, key=lambda candidate: candidate.last_output_at)
    command_name = _DEFAULT_COMMANDS.get(session.provider)
    if command_name is None:
        return None
    return resume_command(
        SuspendedAgent(
            provider=session.provider,
            session_id=session.session_id,
            command_name=command_name,
            cwd=project_path,
            source_path=session.source_path,
            last_output_at=session.last_output_at,
            suspended_at=time.time(),
            claude_worktree_name=session.claude_worktree_name,
            claude_worktree_original_cwd=session.claude_worktree_original_cwd,
        )
    )


def latest_claude_session_ref(window_id: UUID) -> AgentSessionRef | None:
    best: AgentSessionRef | None = None
    for path in iter_claude_code_jsonl_files(window_id):
        session_id = _latest_claude_session_id(path)
        if session_id is None:
            continue
        claude_worktree_name, claude_worktree_original_cwd = _latest_claude_worktree_metadata(
            path,
            session_id=session_id,
        )
        candidate = AgentSessionRef(
            provider="claude_code",
            session_id=session_id,
            source_path=str(path),
            last_output_at=_path_mtime(path),
            claude_worktree_name=claude_worktree_name,
            claude_worktree_original_cwd=claude_worktree_original_cwd,
        )
        best = _newer_session(best, candidate)
    return best


def latest_codex_session_ref(window_id: UUID) -> AgentSessionRef | None:
    best: AgentSessionRef | None = None
    for path in iter_codex_session_files(window_id):
        session_id = _latest_codex_session_id(path)
        if session_id is None:
            continue
        candidate = AgentSessionRef(
            provider="codex",
            session_id=session_id,
            source_path=str(path),
            last_output_at=_path_mtime(path),
        )
        best = _newer_session(best, candidate)
    return best


def latest_cursor_session_ref(window_id: UUID) -> AgentSessionRef | None:
    best: AgentSessionRef | None = None
    for path in cursor_store_paths_for_window(window_id):
        session_id = _cursor_session_id(path)
        if session_id is None:
            continue
        candidate = AgentSessionRef(
            provider="cursor_cli",
            session_id=session_id,
            source_path=str(path),
            last_output_at=_path_mtime(path),
        )
        best = _newer_session(best, candidate)
    return best


def session_id_from_payload(
    provider: str,
    payload: dict[str, Any],
    source_path: str | None,
) -> str | None:
    if provider == "claude_code":
        return _string_value(payload.get("sessionId")) or _string_value(payload.get("session_id"))
    if provider == "codex":
        return _codex_session_id_from_payload(payload) or _session_id_from_path(source_path)
    if provider == "cursor_cli":
        return (
            _string_value(payload.get("agentId"))
            or _string_value(payload.get("chat_id"))
            or (_cursor_session_id(Path(source_path)) if source_path else None)
        )
    return None


def claude_worktree_metadata_from_payload(
    provider: str,
    payload: dict[str, Any],
) -> tuple[str | None, str | None]:
    if provider != "claude_code":
        return None, None
    return _claude_worktree_metadata_from_payload(payload)


def resume_command(record: SuspendedAgent) -> str | None:
    command_name = _resume_command_name(record.provider, record.command_name)
    if command_name is None:
        return None

    if record.provider == "claude_code":
        args = _claude_resume_args(record)
        provider_command = format_agent_command(command_name, *args)
    elif record.provider == "codex":
        provider_command = format_agent_command(command_name, "resume", record.session_id)
    elif record.provider == "cursor_cli":
        provider_command = f"{shlex.quote(command_name)} --resume {shlex.quote(record.session_id)}"
    else:
        return None

    cwd = _resume_cwd(record)
    if cwd:
        return f"cd {shlex.quote(cwd)} && WEB_TERMINAL_AUTO_RESUME=1 {provider_command}"
    return f"WEB_TERMINAL_AUTO_RESUME=1 {provider_command}"


def suspended_agent_from_process(
    session: AgentSessionRef,
    processes: tuple[AgentProcess, ...],
    *,
    project_path: str | None,
    suspended_at: float,
) -> SuspendedAgent | None:
    process = _root_agent_process(processes)
    command_name = _resume_command_name(
        session.provider,
        process.command_name if process is not None else None,
    )
    cwd = (process.cwd if process is not None else None) or project_path
    if command_name is None:
        return None
    return SuspendedAgent(
        provider=session.provider,
        session_id=session.session_id,
        command_name=command_name,
        cwd=cwd,
        source_path=session.source_path,
        last_output_at=session.last_output_at,
        suspended_at=suspended_at,
        claude_worktree_name=session.claude_worktree_name,
        claude_worktree_original_cwd=session.claude_worktree_original_cwd,
    )


async def terminate_agent_processes(processes: tuple[AgentProcess, ...]) -> None:
    pids = tuple(process.pid for process in processes)
    if not pids:
        return

    parent_map = await asyncio.to_thread(_build_parent_map)
    target_pids = await asyncio.to_thread(_descendant_pids, list(pids), parent_map)
    if not target_pids:
        target_pids = set(pids)

    await asyncio.to_thread(_signal_processes, target_pids, signal.SIGTERM)
    deadline = time.monotonic() + AGENT_TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not any(_pid_exists(pid) for pid in target_pids):
            return
        await asyncio.sleep(0.1)
    await asyncio.to_thread(_signal_processes, target_pids, signal.SIGKILL)


def _latest_claude_session_id(path: Path) -> str | None:
    latest: str | None = None
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    latest = _string_value(payload.get("sessionId")) or latest
                    latest = _string_value(payload.get("session_id")) or latest
    except OSError:
        return None
    return latest


def _latest_claude_worktree_metadata(
    path: Path,
    *,
    session_id: str | None = None,
) -> tuple[str | None, str | None]:
    latest_name: str | None = None
    latest_original_cwd: str | None = None
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                payload_session_id = session_id_from_payload("claude_code", payload, str(path))
                if session_id is not None and payload_session_id not in {None, session_id}:
                    continue
                worktree_name, original_cwd = _claude_worktree_metadata_from_payload(payload)
                if worktree_name is not None:
                    latest_name = worktree_name
                    latest_original_cwd = original_cwd
    except OSError:
        return None, None
    return latest_name, latest_original_cwd


def _latest_codex_session_id(path: Path) -> str | None:
    latest: str | None = None
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    latest = _codex_session_id_from_payload(payload) or latest
    except OSError:
        return None
    return latest or _session_id_from_path(str(path))


def _codex_session_id_from_payload(payload: dict[str, Any]) -> str | None:
    raw_id = _string_value(payload.get("trace_id")) or _string_value(payload.get("id"))
    if raw_id:
        return raw_id
    nested = payload.get("payload")
    if isinstance(nested, dict):
        raw_id = _string_value(nested.get("id"))
        if raw_id:
            return raw_id
    return None


def _claude_resume_args(record: SuspendedAgent) -> tuple[str, ...]:
    return ("--resume", record.session_id)


def _resume_cwd(record: SuspendedAgent) -> str | None:
    if record.provider == "claude_code" and record.claude_worktree_name is not None:
        return record.claude_worktree_original_cwd or record.cwd
    return record.cwd


def _claude_worktree_metadata_from_payload(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    worktree_session = payload.get("worktreeSession")
    if not isinstance(worktree_session, dict):
        return None, None
    worktree_name = _valid_claude_worktree_name(
        _string_value(worktree_session.get("worktreeName"))
    )
    if worktree_name is None:
        return None, None
    return worktree_name, _string_value(worktree_session.get("originalCwd"))


def _valid_claude_worktree_name(value: str | None) -> str | None:
    if value is None or "/" in value or "\\" in value:
        return None
    return value


def _cursor_session_id(path: Path) -> str | None:
    meta = _cursor_store_meta(path)
    raw_id = _string_value(meta.get("agentId")) or _string_value(meta.get("chatId"))
    if raw_id:
        return raw_id
    parent_name = path.parent.name
    return parent_name if parent_name else None


def _cursor_store_meta(path: Path) -> dict[str, Any]:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return {}
    try:
        row = conn.execute("select value from meta order by key limit 1").fetchone()
    except sqlite3.DatabaseError:
        return {}
    finally:
        conn.close()
    if row is None:
        return {}

    value = row[0]
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if not isinstance(value, str):
            return {}
        decoded = json.loads(bytes.fromhex(value).decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _event_output_time(source_path: str | None) -> float:
    if source_path:
        return _path_mtime(Path(source_path))
    return time.time()


def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


def _session_id_from_path(source_path: str | None) -> str | None:
    if not source_path:
        return None
    stem = Path(source_path).stem.removeprefix("rollout-")
    suffix_match = _SESSION_ID_SUFFIX.search(stem)
    return suffix_match.group(1) if suffix_match is not None else stem or None


def _newer_session(
    current: AgentSessionRef | None,
    candidate: AgentSessionRef,
) -> AgentSessionRef:
    if current is None or candidate.last_output_at >= current.last_output_at:
        return candidate
    return current


def _merge_session_metadata(
    current: AgentSessionRef | None,
    candidate: AgentSessionRef,
) -> AgentSessionRef:
    if (
        current is None
        or current.provider != candidate.provider
        or current.session_id != candidate.session_id
    ):
        return candidate
    claude_worktree_name = candidate.claude_worktree_name or current.claude_worktree_name
    claude_worktree_original_cwd = (
        candidate.claude_worktree_original_cwd or current.claude_worktree_original_cwd
    )
    if (
        claude_worktree_name == candidate.claude_worktree_name
        and claude_worktree_original_cwd == candidate.claude_worktree_original_cwd
    ):
        return candidate
    return AgentSessionRef(
        provider=candidate.provider,
        session_id=candidate.session_id,
        source_path=candidate.source_path,
        last_output_at=candidate.last_output_at,
        claude_worktree_name=claude_worktree_name,
        claude_worktree_original_cwd=claude_worktree_original_cwd,
    )


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def _claude_local_command_payload(payload: dict[str, Any]) -> bool:
    if _string_value(payload.get("type")) != "user":
        return False

    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    role = _string_value(message.get("role"))
    if role not in {None, "user"}:
        return False

    content = _string_value(message.get("content"))
    if content is None:
        return payload.get("isMeta") is True

    stripped = content.lstrip()
    return payload.get("isMeta") is True or any(
        stripped.startswith(prefix) for prefix in _CLAUDE_LOCAL_COMMAND_PREFIXES
    )


def _root_agent_process(processes: tuple[AgentProcess, ...]) -> AgentProcess | None:
    return min(processes, key=lambda process: process.pid) if processes else None


def _resume_command_name(provider: str, detected_command: str | None) -> str | None:
    detected_command = detected_command or ""
    if detected_command in _PROVIDER_COMMANDS.get(provider, set()):
        return detected_command
    return _DEFAULT_COMMANDS.get(provider)


def _suspended_agent_from_dict(value: dict[str, Any]) -> SuspendedAgent | None:
    provider = _string_value(value.get("provider"))
    session_id = _string_value(value.get("session_id"))
    command_name = _string_value(value.get("command_name"))
    if provider is None or session_id is None or command_name is None:
        return None
    return SuspendedAgent(
        provider=provider,
        session_id=session_id,
        command_name=command_name,
        cwd=_string_value(value.get("cwd")),
        source_path=_string_value(value.get("source_path")),
        last_output_at=float(value.get("last_output_at") or 0),
        suspended_at=float(value.get("suspended_at") or 0),
        claude_worktree_name=_valid_claude_worktree_name(
            _string_value(value.get("claude_worktree_name"))
        ),
        claude_worktree_original_cwd=_string_value(value.get("claude_worktree_original_cwd")),
    )


def _signal_processes(pids: set[int], sig: int) -> None:
    current_pid = os.getpid()
    for pid in sorted(pids, reverse=True):
        if pid <= 1 or pid == current_pid:
            continue
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError:
            logger.debug("cannot signal agent process", extra={"pid": pid, "signal": sig})


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
