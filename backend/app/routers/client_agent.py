from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import time
from typing import Any, Protocol
from uuid import UUID

from elasticsearch import AsyncElasticsearch
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.client_agent.ai_events import ManagedAiEvent, managed_event_from_payload
from app.models import Client, ClientStatus, VirtualWindow
from app.repositories.clients import authenticate_client, get_client
from app.repositories.windows import get_window_for_client
from app.routers.ui_events import ui_event_hub_from_state
from app.services.agent_event_ingest import (
    index_managed_agent_event_if_ready,
    persist_managed_agent_event,
)
from app.services.agent_work_presence import touch_agent_work_presence
from app.services.runtime.client_connections import (
    ClientConnection,
    ClientConnectionClosed,
    ClientConnectionRegistry,
)
from app.services.runtime.broker import TerminalBroker, terminal_status_message
from app.services.runtime.offline_monitor import reconcile_inventory
from app.services.runtime.offline_monitor import mark_remote_client_disconnected
from app.services.runtime.protocol import AgentMessage, TerminalPayload, decode_agent_message, encode_agent_message
from app.services.terminal_command_marker import ParsedCommandMarker
from app.services.terminal_stream_markers import TerminalStreamMarkerExtractor
from app.services.terminal_worktree_marker import ParsedWorktreeMarker
from app.services.git_worktree_coordinator import (
    process_terminal_commands_for_git,
    process_worktree_registration,
)
from app.services.terminal_output_recorder import (
    record_terminal_command_markers,
    record_terminal_output_chunk,
)
from app.services.terminal_selection import TerminalSelectionHub

router = APIRouter(tags=["client-agent"])
logger = logging.getLogger(__name__)
BACKGROUND_MESSAGE_QUEUE_MAX_SIZE = 5000
BACKGROUND_MESSAGE_QUEUE_WARN_SECONDS = 1.0


@dataclass(frozen=True)
class _TerminalOutputRecordingJob:
    client_id: UUID
    window_id: UUID
    clean_data: bytes
    commands: tuple[ParsedCommandMarker, ...]
    worktree_markers: tuple[ParsedWorktreeMarker, ...]


class _BackgroundMessageQueue(Protocol):
    def qsize(self) -> int: ...
    async def put(self, message: AgentMessage) -> None: ...
    async def get(self) -> AgentMessage: ...
    def task_done(self) -> None: ...
    async def join(self) -> None: ...


class _WindowFairMessageQueue:
    def __init__(self, maxsize: int = 0) -> None:
        self._maxsize = maxsize
        self._queued_count = 0
        self._unfinished_count = 0
        self._window_queues: dict[UUID, deque[AgentMessage]] = {}
        self._windows: deque[UUID] = deque()
        self._condition = asyncio.Condition()
        self._join_event = asyncio.Event()
        self._join_event.set()

    def qsize(self) -> int:
        return self._queued_count

    async def put(self, message: AgentMessage) -> None:
        if message.window_id is None:
            raise ValueError("window fair queue messages require window_id")
        async with self._condition:
            while self._maxsize > 0 and self._queued_count >= self._maxsize:
                await self._condition.wait()
            queue = self._window_queues.get(message.window_id)
            if queue is None:
                queue = deque()
                self._window_queues[message.window_id] = queue
                self._windows.append(message.window_id)
            queue.append(message)
            self._queued_count += 1
            self._unfinished_count += 1
            self._join_event.clear()
            self._condition.notify_all()

    async def get(self) -> AgentMessage:
        async with self._condition:
            while True:
                while self._windows:
                    window_id = self._windows.popleft()
                    queue = self._window_queues.get(window_id)
                    if not queue:
                        self._window_queues.pop(window_id, None)
                        continue
                    message = queue.popleft()
                    self._queued_count -= 1
                    if queue:
                        self._windows.append(window_id)
                    else:
                        self._window_queues.pop(window_id, None)
                    self._condition.notify_all()
                    return message
                await self._condition.wait()

    def task_done(self) -> None:
        self._unfinished_count -= 1
        if self._unfinished_count < 0:
            self._unfinished_count = 0
            raise ValueError("task_done() called too many times")
        if self._unfinished_count == 0:
            self._join_event.set()

    async def join(self) -> None:
        await self._join_event.wait()


def _bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token:
        return None
    return token


def _connection_registry(websocket: WebSocket) -> ClientConnectionRegistry:
    registry = getattr(websocket.app.state, "client_connections", None)
    if registry is None:
        registry = ClientConnectionRegistry()
        websocket.app.state.client_connections = registry
    return registry


def _terminal_broker(websocket: WebSocket) -> TerminalBroker:
    broker = getattr(websocket.app.state, "terminal_broker", None)
    if broker is None:
        broker = TerminalBroker()
        websocket.app.state.terminal_broker = broker
    return broker


def _terminal_selection_hub(websocket: WebSocket) -> TerminalSelectionHub:
    hub = getattr(websocket.app.state, "terminal_selection_hub", None)
    if hub is None:
        hub = TerminalSelectionHub()
        websocket.app.state.terminal_selection_hub = hub
    return hub


def _ui_event_hub(websocket: WebSocket):
    return ui_event_hub_from_state(websocket.app.state)


async def _authenticate_websocket_client(
    websocket: WebSocket,
    session: AsyncSession,
) -> Client | None:
    client_id_header = websocket.headers.get("x-client-id")
    token = _bearer_token(websocket.headers.get("authorization"))
    if client_id_header is None or token is None:
        return None

    try:
        client_id = UUID(client_id_header)
    except ValueError:
        return None

    return await authenticate_client(session, client_id, token)


def _mark_client_seen(client: Client) -> datetime:
    now = datetime.now(UTC)
    client.status = ClientStatus.ONLINE
    client.last_seen_at = now
    if client.connected_at is None:
        client.connected_at = now
    return now


def _payload_text(payload: dict[str, Any], key: str, max_length: int) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return value[:max_length]


def _apply_client_reported_metadata(client: Client, payload: dict[str, Any]) -> None:
    hostname = _payload_text(payload, "hostname", 255)
    version = _payload_text(payload, "version", 64)
    if hostname is not None:
        client.hostname = hostname
    if version is not None:
        client.version = version


async def _authenticate_and_mark_seen(websocket: WebSocket) -> UUID | None:
    async with SessionLocal() as session:
        client = await _authenticate_websocket_client(websocket, session)
        if client is None:
            return None
        client_id = client.id
        _mark_client_seen(client)
        await session.commit()
        await _ui_event_hub(websocket).publish_debounced_invalidation(
            ("clients", client_id),
            ["clients"],
            client_id=client_id,
            reason="client_seen",
            delay_seconds=1.0,
        )
        return client_id


async def _mark_client_seen_with_metadata(client_id: UUID, payload: dict[str, Any]) -> bool:
    async with SessionLocal() as session:
        client = await get_client(session, client_id)
        if client is None:
            return False
        _mark_client_seen(client)
        _apply_client_reported_metadata(client, payload)
        await session.commit()
        return True


async def _mark_client_disconnected_by_id(client_id: UUID) -> bool:
    async with SessionLocal() as session:
        changed = await mark_remote_client_disconnected(session, client_id)
        await session.commit()
        return changed


async def _handle_inventory_message(websocket: WebSocket, client_id: UUID, message: AgentMessage) -> bool:
    inventory = message.payload.get("tmux_windows", message.payload.get("windows", []))
    if not isinstance(inventory, list):
        inventory = []

    async with SessionLocal() as session:
        client = await get_client(session, client_id)
        if client is None:
            return False
        _mark_client_seen(client)
        changed_count = await reconcile_inventory(session, client_id, inventory)
        await session.commit()
        resources = ["clients"]
        if changed_count:
            resources.extend(["tree", "window"])
        await _ui_event_hub(websocket).publish_invalidation(
            resources,
            client_id=client_id,
            reason="client_inventory",
        )
        return True


def _ready_es_client(websocket: WebSocket) -> AsyncElasticsearch | None:
    if getattr(websocket.app.state, "es_indexes_ready", False) is not True:
        return None
    return getattr(websocket.app.state, "es_client", None)


async def _commit_session(session) -> None:
    await session.commit()


async def _send_ai_event_ack_message(
    send_message,
    client_id: UUID,
    message: AgentMessage,
    *,
    ok: bool,
    error: str | None = None,
) -> None:
    if message.request_id is None:
        return
    payload: dict[str, Any] = {"ok": ok}
    if error is not None:
        payload["error"] = error
    await send_message(
        AgentMessage(
            type="ai_event_ack",
            client_id=client_id,
            window_id=message.window_id,
            request_id=message.request_id,
            payload=payload,
        )
    )


async def _send_ai_event_ack(
    connection: ClientConnection,
    client_id: UUID,
    message: AgentMessage,
    *,
    ok: bool,
    error: str | None = None,
) -> None:
    try:
        await _send_ai_event_ack_message(
            connection.send,
            client_id,
            message,
            ok=ok,
            error=error,
        )
    except (ClientConnectionClosed, RuntimeError):
        logger.debug(
            "skipped ai_event ack because client-agent connection is closed",
            extra={
                "client_id": str(client_id),
                "window_id": str(message.window_id) if message.window_id else None,
                "request_id": message.request_id,
            },
        )


def _managed_event_from_message(client_id: UUID, message: AgentMessage) -> ManagedAiEvent:
    if message.window_id is None:
        raise ValueError("window_id is required")

    provider = message.payload.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider is required")

    payload = message.payload.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload is required")

    source_path = message.payload.get("source_path")
    if source_path is None:
        source_path = payload.get("source_path")
    if source_path is not None and not isinstance(source_path, str):
        raise ValueError("source_path must be a string")

    cursor = message.payload.get("cursor")
    if cursor is None:
        cursor = payload.get("cursor")
    if cursor is not None and not isinstance(cursor, (str, int)):
        raise ValueError("cursor must be a string or integer")

    offset = message.payload.get("offset")
    if offset is None:
        offset = payload.get("offset")
    if offset is not None:
        try:
            offset = int(offset)
        except (TypeError, ValueError) as exc:
            raise ValueError("offset must be an integer") from exc

    project_path = message.payload.get("project_path")
    if project_path is None:
        project_path = message.payload.get("projectPath")
    if project_path is None:
        project_path = payload.get("project_path")
    if project_path is None:
        project_path = payload.get("projectPath")
    if project_path is not None and not isinstance(project_path, str):
        raise ValueError("project_path must be a string")

    event = managed_event_from_payload(
        client_id,
        message.window_id,
        provider.strip(),
        payload,
        source_path=source_path,
        offset=offset,
        cursor=cursor,
        project_path=project_path,
    )
    if event is None:
        raise ValueError("event attribution does not match client/window")
    return event



async def _handle_ai_event_message_with_ack_sender(
    websocket: WebSocket,
    send_ack_message,
    client_id: UUID,
    message: AgentMessage,
) -> None:
    try:
        if message.window_id is None:
            raise ValueError("window_id is required")
        event = _managed_event_from_message(client_id, message)
        async with SessionLocal() as session:
            row = await persist_managed_agent_event(session, event)
            await _commit_session(session)
            if await index_managed_agent_event_if_ready(session, _ready_es_client(websocket), row):
                await _commit_session(session)
        await _ui_event_hub(websocket).publish_invalidation(
            ["agent_record", "window", "tree", "search"],
            client_id=client_id,
            window_id=message.window_id,
            reason="ai_event",
        )
        await _send_ai_event_ack_message(send_ack_message, client_id, message, ok=True)
    except ValueError as exc:
        await _send_ai_event_ack_message(send_ack_message, client_id, message, ok=False, error=str(exc))


async def _handle_ai_event_message(
    websocket: WebSocket,
    connection: ClientConnection,
    client_id: UUID,
    message: AgentMessage,
) -> None:
    try:
        await _handle_ai_event_message_with_ack_sender(
            websocket,
            connection.send,
            client_id,
            message,
        )
    except (ClientConnectionClosed, RuntimeError):
        logger.debug(
            "skipped ai_event ack because client-agent connection is closed",
            extra={
                "client_id": str(client_id),
                "window_id": str(message.window_id) if message.window_id else None,
                "request_id": message.request_id,
            },
        )


async def _handle_agent_work_presence_message(
    websocket: WebSocket,
    client_id: UUID,
    message: AgentMessage,
) -> None:
    if message.window_id is None:
        raise ValueError("window_id is required")
    providers = message.payload.get("providers")
    reasons = message.payload.get("reasons")
    if not isinstance(providers, list) or not isinstance(reasons, list):
        raise ValueError("providers and reasons must be lists")
    provider_values = [str(value) for value in providers]
    reason_values = [str(value) for value in reasons]

    async with SessionLocal() as session:
        await touch_agent_work_presence(
            session,
            client_id=client_id,
            window_id=message.window_id,
            providers=provider_values,
            reasons=reason_values,
        )
        await _commit_session(session)
    await _ui_event_hub(websocket).publish_invalidation(
        ["window", "tree"],
        client_id=client_id,
        window_id=message.window_id,
        reason="agent_work_presence",
    )


def _extract_terminal_output_bytes(
    message: AgentMessage,
    marker_extractors: dict[UUID, TerminalStreamMarkerExtractor] | None,
) -> tuple[bytes, tuple[ParsedCommandMarker, ...], tuple[ParsedWorktreeMarker, ...]] | None:
    if message.window_id is None:
        return None

    payload = TerminalPayload.model_validate(message.payload)
    if payload.window_id != message.window_id:
        return None

    data = payload.to_bytes()
    if marker_extractors is None:
        from app.services.terminal_command_marker import extract_command_markers
        from app.services.terminal_worktree_marker import extract_worktree_markers

        clean_data, commands = extract_command_markers(data)
        clean_data, worktrees = extract_worktree_markers(clean_data)
    else:
        clean_data, commands, worktrees = marker_extractors.setdefault(
            message.window_id,
            TerminalStreamMarkerExtractor(),
        ).feed(data)
    return clean_data, tuple(commands), tuple(worktrees)


async def _window_belongs_to_client(client_id: UUID, window_id: UUID) -> bool:
    async with SessionLocal() as session:
        result = await session.execute(
            select(VirtualWindow.id).where(
                VirtualWindow.id == window_id,
                VirtualWindow.client_id == client_id,
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def _display_terminal_output_message(
    websocket: WebSocket,
    client_id: UUID,
    message: AgentMessage,
    marker_extractors: dict[UUID, TerminalStreamMarkerExtractor] | None = None,
    *,
    known_windows: set[UUID] | None = None,
) -> _TerminalOutputRecordingJob | None:
    if message.window_id is None:
        return None
    if known_windows is not None:
        if message.window_id not in known_windows:
            if not await _window_belongs_to_client(client_id, message.window_id):
                return None
            known_windows.add(message.window_id)
    elif not await _window_belongs_to_client(client_id, message.window_id):
        return None

    extracted = _extract_terminal_output_bytes(message, marker_extractors)
    if extracted is None:
        return None

    clean_data, commands, worktree_markers = extracted
    if clean_data:
        await _terminal_broker(websocket).publish_output(client_id, message.window_id, clean_data)

    if message.payload.get("is_snapshot") is True:
        return None
    if not clean_data and not commands and not worktree_markers:
        return None
    return _TerminalOutputRecordingJob(
        client_id=client_id,
        window_id=message.window_id,
        clean_data=clean_data,
        commands=commands,
        worktree_markers=worktree_markers,
    )


async def _record_terminal_output_job(websocket: WebSocket, job: _TerminalOutputRecordingJob) -> None:
    registry = _connection_registry(websocket)
    async with SessionLocal() as session:
        window = await get_window_for_client(session, job.client_id, job.window_id)
        if window is None:
            return
        command_events = await record_terminal_command_markers(
            session,
            job.client_id,
            job.window_id,
            list(job.commands),
        )
        git_invalidated = False
        for marker in job.worktree_markers:
            if str(marker.get("window_id")) != str(job.window_id):
                continue
            await process_worktree_registration(
                session,
                client_id=job.client_id,
                window_id=job.window_id,
                marker=marker,
                registry=registry,
            )
            git_invalidated = True
        if job.commands:
            await process_terminal_commands_for_git(
                session,
                client_id=job.client_id,
                window_id=job.window_id,
                commands=list(job.commands),
                registry=registry,
            )
            git_invalidated = True
        if git_invalidated:
            await session.commit()
        if command_events:
            with contextlib.suppress(Exception):
                await _ui_event_hub(websocket).publish_invalidation(
                    ["agent_record", "window", "tree", "search"],
                    client_id=job.client_id,
                    window_id=job.window_id,
                    reason="terminal_command",
                )
        if git_invalidated:
            with contextlib.suppress(Exception):
                await _ui_event_hub(websocket).publish_invalidation(
                    ["window", "tree"],
                    client_id=job.client_id,
                    window_id=job.window_id,
                    reason="git_worktree",
                )
        output_event = None
        if job.clean_data:
            output_event = await record_terminal_output_chunk(
                session,
                job.client_id,
                job.window_id,
                job.clean_data,
                _ready_es_client(websocket),
            )
        if output_event is not None:
            with contextlib.suppress(Exception):
                await _ui_event_hub(websocket).publish_debounced_invalidation(
                    ("terminal_output", job.client_id, job.window_id),
                    ["window", "tree", "search"],
                    client_id=job.client_id,
                    window_id=job.window_id,
                    reason="terminal_output",
                    delay_seconds=1.0,
                )


async def _handle_terminal_output_message(
    websocket: WebSocket,
    client_id: UUID,
    message: AgentMessage,
    marker_extractors: dict[UUID, TerminalStreamMarkerExtractor] | None = None,
    *,
    known_windows: set[UUID] | None = None,
) -> _TerminalOutputRecordingJob | None:
    return await _display_terminal_output_message(
        websocket,
        client_id,
        message,
        marker_extractors,
        known_windows=known_windows,
    )


async def _handle_terminal_selection_message(
    websocket: WebSocket,
    client_id: UUID,
    message: AgentMessage,
) -> None:
    if message.window_id is None:
        return

    async with SessionLocal() as session:
        window = await get_window_for_client(session, client_id, message.window_id)
        if window is None:
            return

    await _terminal_selection_hub(websocket).publish(client_id, message.window_id)
    await _ui_event_hub(websocket).publish_terminal_selection(client_id, message.window_id)


async def _handle_terminal_error_message(
    websocket: WebSocket,
    client_id: UUID,
    message: AgentMessage,
) -> None:
    if message.window_id is None:
        return
    await _terminal_broker(websocket).publish_status(
        client_id,
        message.window_id,
        terminal_status_message("error", reason="runtime_error"),
    )


async def _enqueue_terminal_output_recording_job(
    queue: asyncio.Queue[_TerminalOutputRecordingJob],
    *,
    client_id: UUID,
    job: _TerminalOutputRecordingJob,
) -> None:
    started_at = time.perf_counter()
    await queue.put(job)
    elapsed = time.perf_counter() - started_at
    if elapsed >= BACKGROUND_MESSAGE_QUEUE_WARN_SECONDS:
        logger.warning(
            "client-agent terminal output recording queue applied backpressure",
            extra={
                "client_id": str(client_id),
                "window_id": str(job.window_id),
                "queue_size": queue.qsize(),
                "elapsed_seconds": round(elapsed, 3),
            },
        )


async def _enqueue_background_message(
    queue: _BackgroundMessageQueue,
    *,
    client_id: UUID,
    message: AgentMessage,
    queue_name: str,
) -> None:
    started_at = time.perf_counter()
    await queue.put(message)
    elapsed = time.perf_counter() - started_at
    if elapsed >= BACKGROUND_MESSAGE_QUEUE_WARN_SECONDS:
        logger.warning(
            "client-agent background queue applied backpressure",
            extra={
                "client_id": str(client_id),
                "queue_name": queue_name,
                "message_type": message.type,
                "window_id": str(message.window_id) if message.window_id else None,
                "queue_size": queue.qsize(),
                "elapsed_seconds": round(elapsed, 3),
            },
        )


async def _client_agent_message_worker(
    *,
    client_id: UUID,
    queue_name: str,
    queue: _BackgroundMessageQueue,
    handler,
) -> None:
    while True:
        message = await queue.get()
        try:
            await handler(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "client-agent background message handler failed",
                extra={
                    "client_id": str(client_id),
                    "queue_name": queue_name,
                    "message_type": message.type,
                    "window_id": str(message.window_id) if message.window_id else None,
                },
            )
        finally:
            queue.task_done()


async def _terminal_output_recording_worker(
    *,
    client_id: UUID,
    queue: asyncio.Queue[_TerminalOutputRecordingJob],
    handler,
) -> None:
    while True:
        job = await queue.get()
        try:
            await handler(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "client-agent terminal output recording handler failed",
                extra={
                    "client_id": str(client_id),
                    "window_id": str(job.window_id),
                },
            )
        finally:
            queue.task_done()


async def _wait_for_background_queues(
    *,
    client_id: UUID,
    queues: list[tuple[str, _BackgroundMessageQueue]],
) -> None:
    await asyncio.gather(*(queue.join() for _name, queue in queues))


@router.websocket("/api/client-agent/bulk-ws")
async def client_agent_bulk_websocket(websocket: WebSocket) -> None:
    client_id = await _authenticate_and_mark_seen(websocket)
    if client_id is None:
        logger.warning("client-agent bulk websocket authentication failed")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    marker_extractors: dict[UUID, TerminalStreamMarkerExtractor] = {}
    known_windows: set[UUID] = set()
    send_lock = asyncio.Lock()
    ai_event_queue: asyncio.Queue[AgentMessage] = asyncio.Queue(
        maxsize=BACKGROUND_MESSAGE_QUEUE_MAX_SIZE
    )
    terminal_output_queue = _WindowFairMessageQueue(maxsize=BACKGROUND_MESSAGE_QUEUE_MAX_SIZE)
    terminal_output_recording_queue: asyncio.Queue[_TerminalOutputRecordingJob] = asyncio.Queue(
        maxsize=BACKGROUND_MESSAGE_QUEUE_MAX_SIZE
    )

    async def send_message(message: AgentMessage) -> None:
        async with send_lock:
            await websocket.send_text(encode_agent_message(message))

    async def handle_ai_event(message: AgentMessage) -> None:
        if message.type == "agent_work_presence":
            await _handle_agent_work_presence_message(websocket, client_id, message)
            return
        await _handle_ai_event_message_with_ack_sender(websocket, send_message, client_id, message)

    async def handle_terminal_output(message: AgentMessage) -> None:
        job = await _handle_terminal_output_message(
            websocket,
            client_id,
            message,
            marker_extractors,
            known_windows=known_windows,
        )
        if job is not None:
            await _enqueue_terminal_output_recording_job(
                terminal_output_recording_queue,
                client_id=client_id,
                job=job,
            )

    async def handle_terminal_output_recording(job: _TerminalOutputRecordingJob) -> None:
        await _record_terminal_output_job(websocket, job)

    background_workers = []

    try:
        try:
            raw_message = await websocket.receive_text()
            hello = decode_agent_message(raw_message)
        except (ValidationError, RuntimeError):
            await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
            return
        if hello.client_id != client_id:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        if hello.type != "bulk_hello":
            await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
            return
        await send_message(AgentMessage(type="bulk_hello_ack", client_id=client_id))
        background_workers = [
            asyncio.create_task(
                _client_agent_message_worker(
                    client_id=client_id,
                    queue_name="bulk_ai_event",
                    queue=ai_event_queue,
                    handler=handle_ai_event,
                )
            ),
            asyncio.create_task(
                _client_agent_message_worker(
                    client_id=client_id,
                    queue_name="bulk_terminal_output",
                    queue=terminal_output_queue,
                    handler=handle_terminal_output,
                )
            ),
            asyncio.create_task(
                _terminal_output_recording_worker(
                    client_id=client_id,
                    queue=terminal_output_recording_queue,
                    handler=handle_terminal_output_recording,
                )
            ),
        ]

        while True:
            try:
                try:
                    raw_message = await websocket.receive_text()
                except RuntimeError as exc:
                    if "WebSocket is not connected" in str(exc):
                        logger.info(
                            "client-agent bulk websocket receive stopped after close",
                            extra={"client_id": str(client_id)},
                        )
                        return
                    raise
                message = decode_agent_message(raw_message)
            except ValidationError:
                logger.warning(
                    "client-agent bulk websocket received invalid message",
                    extra={"client_id": str(client_id)},
                )
                await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                return

            if message.client_id != client_id:
                logger.warning(
                    "client-agent bulk websocket client_id mismatch",
                    extra={
                        "authenticated_client_id": str(client_id),
                        "message_client_id": str(message.client_id),
                        "message_type": message.type,
                    },
                )
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

            if message.type in {"ai_event", "agent_work_presence"}:
                await _enqueue_background_message(
                    ai_event_queue,
                    client_id=client_id,
                    message=message,
                    queue_name="bulk_ai_event",
                )
                continue

            if message.type == "terminal_output":
                await _enqueue_background_message(
                    terminal_output_queue,
                    client_id=client_id,
                    message=message,
                    queue_name="bulk_terminal_output",
                )
                continue

            logger.warning(
                "client-agent bulk websocket received unsupported message",
                extra={
                    "client_id": str(client_id),
                    "message_type": message.type,
                    "window_id": str(message.window_id) if message.window_id else None,
                },
            )
            await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
            return
    except WebSocketDisconnect as exc:
        logger.info(
            "client-agent bulk websocket disconnected",
            extra={"client_id": str(client_id), "code": getattr(exc, "code", None)},
        )
    except Exception:
        logger.exception(
            "client-agent bulk websocket failed",
            extra={"client_id": str(client_id)},
        )
        raise
    finally:
        await _wait_for_background_queues(
            client_id=client_id,
            queues=[
                ("bulk_ai_event", ai_event_queue),
                ("bulk_terminal_output", terminal_output_queue),
                ("bulk_terminal_output_recording", terminal_output_recording_queue),
            ],
        )
        for worker in background_workers:
            worker.cancel()
        for worker in background_workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker


@router.websocket("/api/client-agent/ws")
async def client_agent_websocket(websocket: WebSocket) -> None:
    client_id = await _authenticate_and_mark_seen(websocket)
    if client_id is None:
        logger.warning("client-agent websocket authentication failed")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    connection = ClientConnection(websocket=websocket, client_id=client_id)
    registry = _connection_registry(websocket)
    terminal_selection_queue: asyncio.Queue[AgentMessage] = asyncio.Queue(
        maxsize=BACKGROUND_MESSAGE_QUEUE_MAX_SIZE
    )

    async def handle_terminal_selection(message: AgentMessage) -> None:
        await _handle_terminal_selection_message(websocket, client_id, message)

    background_workers = [
        asyncio.create_task(
            _client_agent_message_worker(
                client_id=client_id,
                queue_name="terminal_selection",
                queue=terminal_selection_queue,
                handler=handle_terminal_selection,
            )
        ),
    ]

    await registry.register(connection)
    logger.info("client-agent websocket connected", extra={"client_id": str(client_id)})

    try:
        while True:
            try:
                try:
                    raw_message = await websocket.receive_text()
                except RuntimeError as exc:
                    if "WebSocket is not connected" in str(exc):
                        logger.info(
                            "client-agent websocket receive stopped after close",
                            extra={"client_id": str(client_id)},
                        )
                        return
                    raise
                message = decode_agent_message(raw_message)
            except ValidationError:
                logger.warning(
                    "client-agent websocket received invalid message",
                    extra={"client_id": str(client_id)},
                )
                await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                return

            if message.client_id != client_id:
                logger.warning(
                    "client-agent websocket client_id mismatch",
                    extra={
                        "authenticated_client_id": str(client_id),
                        "message_client_id": str(message.client_id),
                        "message_type": message.type,
                    },
                )
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

            if connection.resolve(message):
                connection.mark_seen()
                continue

            if message.type == "hello":
                if not await _mark_client_seen_with_metadata(client_id, message.payload):
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    return
                await _ui_event_hub(websocket).publish_debounced_invalidation(
                    ("clients", client_id),
                    ["clients"],
                    client_id=client_id,
                    reason="client_hello",
                    delay_seconds=1.0,
                )
                connection.mark_seen()
                await connection.send(AgentMessage(type="hello_ack", client_id=client_id))
                continue

            if message.type == "heartbeat":
                if not await _mark_client_seen_with_metadata(client_id, message.payload):
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    return
                await _ui_event_hub(websocket).publish_debounced_invalidation(
                    ("clients", client_id),
                    ["clients"],
                    client_id=client_id,
                    reason="client_heartbeat",
                    delay_seconds=1.0,
                )
                connection.mark_seen()
                await connection.send(AgentMessage(type="heartbeat_ack", client_id=client_id))
                continue

            if message.type == "inventory":
                if not await _handle_inventory_message(websocket, client_id, message):
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    return
                connection.mark_seen()
                continue

            if message.type in {"ai_event", "terminal_output"}:
                logger.warning(
                    "client-agent bulk message received on control websocket",
                    extra={
                        "client_id": str(client_id),
                        "message_type": message.type,
                        "window_id": str(message.window_id) if message.window_id else None,
                    },
                )
                await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                return

            if message.type == "terminal_selection":
                connection.mark_seen()
                await _enqueue_background_message(
                    terminal_selection_queue,
                    client_id=client_id,
                    message=message,
                    queue_name="terminal_selection",
                )
                continue

            if message.type == "terminal_error":
                connection.mark_seen()
                logger.warning(
                    "client-agent terminal error",
                    extra={
                        "client_id": str(client_id),
                        "window_id": str(message.window_id) if message.window_id else None,
                        "error_message": message.payload.get("message"),
                    },
                )
                await _handle_terminal_error_message(websocket, client_id, message)
                continue
    except WebSocketDisconnect as exc:
        logger.info(
            "client-agent websocket disconnected",
            extra={"client_id": str(client_id), "code": getattr(exc, "code", None)},
        )
    except ClientConnectionClosed:
        logger.info(
            "client-agent websocket send stopped after close",
            extra={"client_id": str(client_id)},
        )
    except Exception:
        logger.exception(
            "client-agent websocket failed",
            extra={"client_id": str(client_id)},
        )
        raise
    finally:
        await _wait_for_background_queues(
            client_id=client_id,
            queues=[("terminal_selection", terminal_selection_queue)],
        )
        for worker in background_workers:
            worker.cancel()
        for worker in background_workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        await registry.unregister(connection)
        connection.abort()
        if registry.get(client_id) is None:
            changed = await _mark_client_disconnected_by_id(client_id)
            if changed:
                await _ui_event_hub(websocket).publish_invalidation(
                    ["clients", "tree", "window"],
                    client_id=client_id,
                    reason="client_disconnected",
                )
            logger.warning(
                "client-agent websocket removed last connection",
                extra={"client_id": str(client_id), "marked_offline": changed},
            )
            await _terminal_broker(websocket).clear_client(
                client_id,
                status_message=terminal_status_message(
                    "unavailable",
                    reason="client_offline",
                    retry_after_ms=5000,
                ),
            )
