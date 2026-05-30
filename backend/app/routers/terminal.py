from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from uuid import UUID

from elasticsearch import AsyncElasticsearch
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status

from app.auth import require_websocket_auth
from app.db import SessionLocal
from app.models import LOCAL_CLIENT_ID, VirtualWindow, WindowStatus
from app.repositories.clients import get_client
from app.repositories.windows import get_window_for_client
from app.repositories.windows import get_window_for_local_tmux_target
from app.repositories.windows import patch_runtime_window
from app.routers.ui_events import ui_event_hub_from_state
from app.services.runtime.broker import TerminalBroker, TerminalRuntimeUnavailable, terminal_status_message
from app.services.runtime.client_connections import ClientConnectionRegistry
from app.services.runtime.local import LocalTerminalRuntime
from app.services.runtime.remote import RemoteClientUnavailable, RemoteRuntime, RemoteTerminalError
from app.services.runtime.types import RuntimeWindow
from app.services.terminal_bridge import OutputAckControl, ResizeControl, SelectWindowControl, parse_text_input
from app.services.terminal_stream_markers import TerminalStreamMarkerExtractor
from app.services.git_worktree_coordinator import (
    commands_need_git_worktree_tracking,
    git_worktree_agent_run_sequences,
    process_git_worktree_snapshot_refresh,
    process_terminal_commands_for_git,
    process_worktree_registration,
)
from app.services.terminal_output_recorder import (
    record_terminal_command_markers,
    record_terminal_output_chunk,
)
from app.services.terminal_selection import TerminalSelectionHub
from app.services.tmux_manager import TmuxCommandError, TmuxManager, get_tmux_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["terminal"])
REMOTE_RECONNECT_RETRY_AFTER_MS = 5000
ATTACH_SNAPSHOT_GRACE_SECONDS = 0.5
LOCAL_OUTPUT_RECORD_BATCH_BYTES = 32 * 1024
LOCAL_OUTPUT_RECORD_BATCH_DELAY_SECONDS = 0.02


@dataclass(frozen=True)
class _LocalTerminalOutputRecordJob:
    target_window_id: UUID
    clean_data: bytes
    commands: list
    worktree_markers: list
    is_attach_snapshot: bool


def mark_window_error(window: VirtualWindow) -> None:
    window.status = WindowStatus.error


def mark_window_active(window: VirtualWindow) -> None:
    window.status = WindowStatus.active


def mark_window_disconnected(window: VirtualWindow) -> None:
    window.status = WindowStatus.disconnected


def _ready_es_client(websocket: WebSocket) -> AsyncElasticsearch | None:
    if getattr(websocket.app.state, "es_indexes_ready", False) is not True:
        return None
    return getattr(websocket.app.state, "es_client", None)


def _client_connection_registry(websocket: WebSocket) -> ClientConnectionRegistry:
    registry = getattr(websocket.app.state, "client_connections", None)
    if registry is None:
        registry = ClientConnectionRegistry()
        websocket.app.state.client_connections = registry
    return registry


def _terminal_broker(websocket: WebSocket, tmux_manager: TmuxManager) -> TerminalBroker:
    broker = getattr(websocket.app.state, "terminal_broker", None)
    if broker is None:
        broker = TerminalBroker()
        websocket.app.state.terminal_broker = broker

    if broker.runtime_for(LOCAL_CLIENT_ID) is None:
        local_runtime = getattr(websocket.app.state, "local_terminal_runtime", None)
        if local_runtime is None:
            local_runtime = LocalTerminalRuntime(tmux_manager)
            websocket.app.state.local_terminal_runtime = local_runtime
        broker.register_runtime(LOCAL_CLIENT_ID, local_runtime)
    return broker


def _terminal_selection_hub(websocket: WebSocket) -> TerminalSelectionHub:
    hub = getattr(websocket.app.state, "terminal_selection_hub", None)
    if hub is None:
        hub = TerminalSelectionHub()
        websocket.app.state.terminal_selection_hub = hub
    return hub


def _ui_event_hub(websocket: WebSocket):
    return ui_event_hub_from_state(websocket.app.state)


def _runtime_window_from_virtual_window(window: VirtualWindow) -> tuple[RuntimeWindow, bool, bool] | None:
    is_local_window = window.tmux_session is not None and window.tmux_window_id is not None
    is_remote_window = window.remote_session_id is not None and window.remote_window_id is not None
    if is_local_window:
        return (
            RuntimeWindow(
                session_id=window.tmux_session,
                window_id=window.tmux_window_id,
                cwd=window.cwd,
                shell_command=window.shell_command,
            ),
            True,
            False,
        )
    if is_remote_window:
        return (
            RuntimeWindow(
                session_id=window.remote_session_id,
                window_id=window.remote_window_id,
                cwd=window.cwd,
                shell_command=window.shell_command,
            ),
            False,
            True,
        )
    return None


async def _persist_runtime_window(
    client_id: UUID,
    window_id: UUID,
    runtime_window: RuntimeWindow,
    *,
    is_local_window: bool,
    is_remote_window: bool,
) -> None:
    async with SessionLocal() as session:
        await patch_runtime_window(
            session,
            client_id,
            window_id,
            tmux_session=runtime_window.session_id if is_local_window else None,
            tmux_window_id=runtime_window.window_id if is_local_window else None,
            remote_session_id=runtime_window.session_id if is_remote_window else None,
            remote_window_id=runtime_window.window_id if is_remote_window else None,
            cwd=runtime_window.cwd,
            shell_command=runtime_window.shell_command,
        )
        with contextlib.suppress(Exception):
            await session.commit()


async def _mark_window_error(client_id: UUID, window_id: UUID) -> None:
    async with SessionLocal() as session:
        window = await get_window_for_client(session, client_id, window_id)
        if window is not None:
            mark_window_error(window)
            with contextlib.suppress(Exception):
                await session.commit()


async def _mark_window_active(client_id: UUID, window_id: UUID) -> None:
    async with SessionLocal() as session:
        window = await get_window_for_client(session, client_id, window_id)
        if window is not None and window.status is not WindowStatus.active:
            mark_window_active(window)
            with contextlib.suppress(Exception):
                await session.commit()


async def _mark_window_disconnected(client_id: UUID, window_id: UUID) -> None:
    async with SessionLocal() as session:
        window = await get_window_for_client(session, client_id, window_id)
        if window is not None and window.status is not WindowStatus.disconnected:
            mark_window_disconnected(window)
            with contextlib.suppress(Exception):
                await session.commit()


async def _local_runtime_window_to_virtual_window_id(
    client_id: UUID,
    runtime_window: RuntimeWindow,
) -> UUID | None:
    async with SessionLocal() as session:
        window = await get_window_for_local_tmux_target(
            session,
            client_id,
            tmux_session=runtime_window.session_id,
            tmux_window_id=runtime_window.window_id,
        )
        return window.id if window is not None else None


@router.websocket("/api/clients/{client_id}/terminal/{window_id}")
async def terminal_websocket(
    websocket: WebSocket,
    client_id: UUID,
    window_id: UUID,
    tmux_manager: TmuxManager = Depends(get_tmux_manager),
) -> None:
    if not await require_websocket_auth(websocket):
        return

    query_params = getattr(websocket, "query_params", {})
    view_id_text = query_params.get("view_id")
    try:
        view_id = UUID(view_id_text) if view_id_text else window_id
    except ValueError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    async with SessionLocal() as session:
        window = await get_window_for_client(session, client_id, window_id)
        if window is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        runtime_result = _runtime_window_from_virtual_window(window)
        if runtime_result is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        runtime_window, is_local_window, is_remote_window = runtime_result
        if window.status is WindowStatus.disconnected and not is_remote_window:
            await websocket.accept()
            await websocket.send_text(
                terminal_status_message(
                    "unavailable",
                    reason="client_offline",
                    retry_after_ms=REMOTE_RECONNECT_RETRY_AFTER_MS,
                )
            )
            await websocket.close()
            return

    broker = _terminal_broker(websocket, tmux_manager)
    if is_remote_window:
        registry = _client_connection_registry(websocket)
        connection = registry.get(client_id)
        if connection is None or getattr(connection, "closed", False):
            await _mark_window_disconnected(client_id, window_id)
            await websocket.accept()
            await websocket.send_text(
                terminal_status_message(
                    "unavailable",
                    reason="client_offline",
                    retry_after_ms=REMOTE_RECONNECT_RETRY_AFTER_MS,
                )
            )
            await websocket.close(code=1013)
            return
        remote_runtime = RemoteRuntime(client_id=client_id, registry=registry)
        broker.register_runtime(client_id, remote_runtime)

    marker_extractor = TerminalStreamMarkerExtractor()
    current_window_id = window_id
    current_runtime_window = runtime_window
    browser_input_seen = False
    attach_started_at = time.monotonic()
    local_output_record_queue: asyncio.Queue[_LocalTerminalOutputRecordJob] = asyncio.Queue()
    local_output_record_worker: asyncio.Task[None] | None = None

    async def publish_selection(selected_runtime_window: RuntimeWindow) -> None:
        nonlocal current_window_id, current_runtime_window, marker_extractor
        selected_window_id = await _local_runtime_window_to_virtual_window_id(
            client_id,
            selected_runtime_window,
        )
        if selected_window_id is None:
            return
        current_window_id = selected_window_id
        current_runtime_window = selected_runtime_window
        marker_extractor = TerminalStreamMarkerExtractor()
        await websocket.send_text(json.dumps({
            "type": "terminal_selection",
            "client_id": str(client_id),
            "window_id": str(selected_window_id),
            "view_id": str(view_id),
        }, separators=(",", ":")))

    async def _record_local_terminal_output(
        target_window_id: UUID,
        clean_data: bytes,
        commands: list,
        *,
        is_attach_snapshot: bool,
    ) -> None:
        if is_attach_snapshot or (not clean_data and not commands):
            return
        try:
            async with SessionLocal() as session:
                command_events = await record_terminal_command_markers(
                    session,
                    client_id,
                    target_window_id,
                    commands,
                )
                if command_events:
                    with contextlib.suppress(Exception):
                        await _ui_event_hub(websocket).publish_invalidation(
                            ["agent_record", "command_history", "window", "search"],
                            client_id=client_id,
                            window_id=target_window_id,
                            reason="terminal_command",
                        )
                output_recorded = False
                if clean_data:
                    output_recorded = await record_terminal_output_chunk(
                        session,
                        client_id,
                        target_window_id,
                        clean_data,
                        _ready_es_client(websocket),
                    )
                if output_recorded:
                    with contextlib.suppress(Exception):
                        await _ui_event_hub(websocket).publish_debounced_invalidation(
                            ("terminal_output", client_id, target_window_id),
                            ["window", "search"],
                            client_id=client_id,
                            window_id=target_window_id,
                            reason="terminal_output",
                            delay_seconds=1.0,
                        )
        except Exception:
            logger.exception("terminal output recording failed")

    async def _record_local_git_worktree_tracking(
        target_window_id: UUID,
        commands: list,
        worktree_markers: list,
        *,
        is_attach_snapshot: bool,
    ) -> None:
        command_list = list(commands)
        commands_require_git = commands_need_git_worktree_tracking(command_list)
        if (
            is_attach_snapshot
            or (not worktree_markers and not commands_require_git)
        ):
            return
        try:
            changed = False
            async with SessionLocal() as session:
                for marker in worktree_markers:
                    if str(marker.get("window_id")) != str(target_window_id):
                        continue
                    await process_worktree_registration(
                        session,
                        client_id=client_id,
                        window_id=target_window_id,
                        marker=marker,
                        registry=None,
                    )
                    changed = True
                if commands_require_git:
                    await process_terminal_commands_for_git(
                        session,
                        client_id=client_id,
                        window_id=target_window_id,
                        commands=command_list,
                        registry=None,
                    )
                    changed = True
                if not changed:
                    return
                await session.commit()
                snapshot_changed = await process_git_worktree_snapshot_refresh(
                    session,
                    client_id=client_id,
                    window_id=target_window_id,
                    registry=None,
                    command_sequences=git_worktree_agent_run_sequences(command_list) or None,
                )
                if snapshot_changed:
                    await session.commit()
            if changed or snapshot_changed:
                with contextlib.suppress(Exception):
                    await _ui_event_hub(websocket).publish_invalidation(
                        ["window", "tree", "git_runs"],
                        client_id=client_id,
                        window_id=target_window_id,
                        reason="git_worktree",
                    )
        except Exception:
            logger.exception("local git worktree tracking failed")

    async def _record_local_git_worktree_tracking_task(
        target_window_id: UUID,
        commands: list,
        worktree_markers: list,
        *,
        is_attach_snapshot: bool,
    ) -> None:
        await _record_local_git_worktree_tracking(
            target_window_id,
            commands,
            worktree_markers,
            is_attach_snapshot=is_attach_snapshot,
        )

    async def _record_local_terminal_output_task(
        target_window_id: UUID,
        clean_data: bytes,
        commands: list,
        worktree_markers: list,
        *,
        is_attach_snapshot: bool,
    ) -> None:
        await _record_local_terminal_output(
            target_window_id,
            clean_data,
            commands,
            is_attach_snapshot=is_attach_snapshot,
        )
        if worktree_markers or commands_need_git_worktree_tracking(list(commands)):
            asyncio.create_task(
                _record_local_git_worktree_tracking_task(
                    target_window_id,
                    commands,
                    worktree_markers,
                    is_attach_snapshot=is_attach_snapshot,
                )
            )

    def _can_batch_local_output_recording(job: _LocalTerminalOutputRecordJob) -> bool:
        return (
            bool(job.clean_data)
            and not job.commands
            and not job.worktree_markers
            and not job.is_attach_snapshot
        )

    async def _local_output_recording_worker() -> None:
        pending_window_id: UUID | None = None
        pending_data = bytearray()

        async def flush_pending() -> None:
            nonlocal pending_window_id, pending_data
            if pending_window_id is None or not pending_data:
                return
            target_window_id = pending_window_id
            data = bytes(pending_data)
            pending_window_id = None
            pending_data = bytearray()
            await _record_local_terminal_output_task(
                target_window_id,
                data,
                [],
                [],
                is_attach_snapshot=False,
            )

        while True:
            if pending_data and len(pending_data) >= LOCAL_OUTPUT_RECORD_BATCH_BYTES:
                await flush_pending()
                continue

            try:
                if pending_data:
                    job = await asyncio.wait_for(
                        local_output_record_queue.get(),
                        timeout=LOCAL_OUTPUT_RECORD_BATCH_DELAY_SECONDS,
                    )
                else:
                    job = local_output_record_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            except asyncio.TimeoutError:
                await flush_pending()
                if local_output_record_queue.empty():
                    return
                continue

            try:
                if _can_batch_local_output_recording(job):
                    if (
                        pending_window_id is not None
                        and (
                            pending_window_id != job.target_window_id
                            or len(pending_data) + len(job.clean_data) > LOCAL_OUTPUT_RECORD_BATCH_BYTES
                        )
                    ):
                        await flush_pending()
                    pending_window_id = job.target_window_id
                    pending_data.extend(job.clean_data)
                    continue

                await flush_pending()
                await _record_local_terminal_output_task(
                    job.target_window_id,
                    job.clean_data,
                    job.commands,
                    job.worktree_markers,
                    is_attach_snapshot=job.is_attach_snapshot,
                )
            finally:
                local_output_record_queue.task_done()

    def _queue_local_output_recording(job: _LocalTerminalOutputRecordJob) -> None:
        nonlocal local_output_record_worker
        local_output_record_queue.put_nowait(job)
        if local_output_record_worker is None or local_output_record_worker.done():
            local_output_record_worker = asyncio.create_task(_local_output_recording_worker())

    async def record_and_publish_output(data: bytes) -> None:
        target_window_id = current_window_id
        clean_data, commands, worktree_markers = marker_extractor.feed(data)
        is_attach_snapshot = (
            bool(commands or worktree_markers or clean_data)
            and not browser_input_seen
            and time.monotonic() - attach_started_at <= ATTACH_SNAPSHOT_GRACE_SECONDS
        )
        if clean_data:
            await broker.publish_view_output(client_id, view_id, clean_data)
        if (commands or worktree_markers or clean_data) and not is_attach_snapshot:
            _queue_local_output_recording(
                _LocalTerminalOutputRecordJob(
                    target_window_id,
                    clean_data,
                    commands,
                    worktree_markers,
                    is_attach_snapshot=is_attach_snapshot,
                )
            )

    async def select_active_window(next_window_id: UUID) -> bool:
        nonlocal current_window_id, current_runtime_window, is_local_window, is_remote_window
        nonlocal marker_extractor, attach_started_at, browser_input_seen
        if next_window_id == current_window_id:
            return True
        async with SessionLocal() as session:
            next_window = await get_window_for_client(session, client_id, next_window_id)
            if next_window is None:
                return False
            runtime_result = _runtime_window_from_virtual_window(next_window)
            if runtime_result is None:
                return False
            next_runtime_window, next_is_local_window, next_is_remote_window = runtime_result

        selected_runtime_window = await broker.select_window(
            client_id,
            view_id,
            current_window_id,
            current_runtime_window,
            next_window_id,
            next_runtime_window,
        )
        if selected_runtime_window != next_runtime_window:
            await _persist_runtime_window(
                client_id,
                next_window_id,
                selected_runtime_window,
                is_local_window=next_is_local_window,
                is_remote_window=next_is_remote_window,
            )
            next_runtime_window = selected_runtime_window
        current_window_id = next_window_id
        current_runtime_window = next_runtime_window
        is_local_window = next_is_local_window
        is_remote_window = next_is_remote_window
        marker_extractor = TerminalStreamMarkerExtractor()
        attach_started_at = time.monotonic()
        browser_input_seen = False
        await _mark_window_active(client_id, next_window_id)
        await websocket.send_text(json.dumps({
            "type": "terminal_selection",
            "client_id": str(client_id),
            "window_id": str(next_window_id),
            "view_id": str(view_id),
        }, separators=(",", ":")))
        return True

    await websocket.accept()
    output_sender = websocket.send_bytes
    await broker.subscribe(client_id, view_id, output_sender, websocket.send_text)
    try:
        try:
            attached_runtime_window = await broker.attach(
                client_id,
                window_id,
                runtime_window,
                output_callback=record_and_publish_output if is_local_window else None,
                selection_callback=publish_selection if is_local_window else None,
                view_id=view_id,
            )
            if attached_runtime_window is not None and attached_runtime_window != runtime_window:
                runtime_window = attached_runtime_window
                current_runtime_window = attached_runtime_window
                await _persist_runtime_window(
                    client_id,
                    window_id,
                    runtime_window,
                    is_local_window=is_local_window,
                    is_remote_window=is_remote_window,
                )
        except RemoteClientUnavailable:
            await _mark_window_disconnected(client_id, window_id)
            await websocket.send_text(
                terminal_status_message(
                    "unavailable",
                    reason="client_offline",
                    retry_after_ms=REMOTE_RECONNECT_RETRY_AFTER_MS,
                )
            )
            await websocket.close(code=1013)
            return
        except (TerminalRuntimeUnavailable, TmuxCommandError, RemoteTerminalError, RuntimeError):
            await _mark_window_error(client_id, window_id)
            await websocket.send_text(
                terminal_status_message("error", reason="attach_failed")
            )
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            return
        await _mark_window_active(client_id, window_id)
        await websocket.send_text(terminal_status_message("connected"))

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                try:
                    browser_input_seen = True
                    await broker.send_input(
                        client_id,
                        current_window_id,
                        current_runtime_window,
                        message["bytes"],
                        view_id=view_id,
                    )
                except RemoteClientUnavailable:
                    await _mark_window_disconnected(client_id, current_window_id)
                    await broker.clear_client(
                        client_id,
                        status_message=terminal_status_message(
                            "unavailable",
                            reason="client_offline",
                            retry_after_ms=REMOTE_RECONNECT_RETRY_AFTER_MS,
                        ),
                    )
                    await websocket.close(code=1013)
                    return
                continue
            if message.get("text") is not None:
                action = parse_text_input(message["text"])
                if isinstance(action, SelectWindowControl):
                    try:
                        if not await select_active_window(action.window_id):
                            await websocket.send_text(
                                terminal_status_message("error", reason="select_failed")
                            )
                    except RemoteClientUnavailable:
                        await _mark_window_disconnected(client_id, current_window_id)
                        await broker.clear_client(
                            client_id,
                            status_message=terminal_status_message(
                                "unavailable",
                                reason="client_offline",
                                retry_after_ms=REMOTE_RECONNECT_RETRY_AFTER_MS,
                            ),
                        )
                        await websocket.close(code=1013)
                        return
                    except (TerminalRuntimeUnavailable, TmuxCommandError, RemoteTerminalError, RuntimeError):
                        await websocket.send_text(
                            terminal_status_message("error", reason="select_failed")
                        )
                elif isinstance(action, ResizeControl):
                    try:
                        await broker.resize(
                            client_id,
                            current_window_id,
                            current_runtime_window,
                            cols=action.cols,
                            rows=action.rows,
                            view_id=view_id,
                        )
                    except RemoteClientUnavailable:
                        await _mark_window_disconnected(client_id, current_window_id)
                        await broker.clear_client(
                            client_id,
                            status_message=terminal_status_message(
                                "unavailable",
                                reason="client_offline",
                                retry_after_ms=REMOTE_RECONNECT_RETRY_AFTER_MS,
                            ),
                        )
                        await websocket.close(code=1013)
                        return
                elif isinstance(action, OutputAckControl):
                    await broker.acknowledge_output(
                        client_id,
                        view_id,
                        output_sender,
                        bytes_acked=action.bytes_acked,
                    )
                elif isinstance(action, bytes):
                    browser_input_seen = True
                    try:
                        await broker.send_input(
                            client_id,
                            current_window_id,
                            current_runtime_window,
                            action,
                            view_id=view_id,
                        )
                    except RemoteClientUnavailable:
                        await _mark_window_disconnected(client_id, current_window_id)
                        await broker.clear_client(
                            client_id,
                            status_message=terminal_status_message(
                                "unavailable",
                                reason="client_offline",
                                retry_after_ms=REMOTE_RECONNECT_RETRY_AFTER_MS,
                            ),
                        )
                        await websocket.close(code=1013)
                        return
    except WebSocketDisconnect:
        return
    except RemoteClientUnavailable:
        await _mark_window_disconnected(client_id, current_window_id)
        await websocket.close(code=1013)
        return
    finally:
        await broker.unsubscribe(client_id, view_id, output_sender, websocket.send_text)


@router.websocket("/api/terminal/{window_id}")
async def local_terminal_websocket(
    websocket: WebSocket,
    window_id: UUID,
    tmux_manager: TmuxManager = Depends(get_tmux_manager),
) -> None:
    await terminal_websocket(websocket, LOCAL_CLIENT_ID, window_id, tmux_manager)


@router.websocket("/api/clients/{client_id}/terminal-selection")
async def terminal_selection_websocket(websocket: WebSocket, client_id: UUID) -> None:
    if not await require_websocket_auth(websocket):
        return

    async with SessionLocal() as session:
        client = await get_client(session, client_id)
        if client is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    hub = _terminal_selection_hub(websocket)
    await websocket.accept()
    await hub.subscribe(client_id, websocket.send_text)
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except WebSocketDisconnect:
        return
    finally:
        await hub.unsubscribe(client_id, websocket.send_text)
