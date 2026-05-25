from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from uuid import UUID

from elasticsearch import AsyncElasticsearch
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status

from app.db import SessionLocal
from app.models import LOCAL_CLIENT_ID, VirtualWindow, WindowStatus
from app.repositories.clients import get_client
from app.repositories.windows import get_window_for_client
from app.repositories.windows import get_window_for_local_tmux_target
from app.routers.ui_events import ui_event_hub_from_state
from app.services.runtime.broker import TerminalBroker, TerminalRuntimeUnavailable, terminal_status_message
from app.services.runtime.client_connections import ClientConnectionRegistry
from app.services.runtime.local import LocalTerminalRuntime
from app.services.runtime.remote import RemoteClientUnavailable, RemoteRuntime, RemoteTerminalError
from app.services.runtime.types import RuntimeWindow
from app.services.terminal_bridge import ResizeControl, parse_text_input
from app.services.terminal_command_marker import CommandMarkerExtractor
from app.services.terminal_output_recorder import (
    record_terminal_command_markers,
    record_terminal_output_chunk,
)
from app.services.terminal_selection import TerminalSelectionHub
from app.services.tmux_manager import TmuxCommandError, TmuxManager, TmuxTarget, get_tmux_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["terminal"])
REMOTE_RECONNECT_RETRY_AFTER_MS = 5000
ATTACH_SNAPSHOT_GRACE_SECONDS = 0.5


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
    async with SessionLocal() as session:
        window = await get_window_for_client(session, client_id, window_id)
        if window is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        is_local_window = window.tmux_session is not None and window.tmux_window_id is not None
        is_remote_window = window.remote_session_id is not None and window.remote_window_id is not None
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
        if is_local_window:
            runtime_window = RuntimeWindow(
                session_id=window.tmux_session,
                window_id=window.tmux_window_id,
            )
            if not await tmux_manager.has_window(
                TmuxTarget(session=window.tmux_session, window_id=window.tmux_window_id)
            ):
                mark_window_error(window)
                with contextlib.suppress(Exception):
                    await session.commit()
                await websocket.accept()
                await websocket.send_text(terminal_status_message("error", reason="attach_failed"))
                await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
                return
        elif is_remote_window:
            runtime_window = RuntimeWindow(
                session_id=window.remote_session_id,
                window_id=window.remote_window_id,
            )
        else:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
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

    marker_extractor = CommandMarkerExtractor()
    current_window_id = window_id
    browser_input_seen = False
    attach_started_at = time.monotonic()

    async def publish_selection(runtime_window: RuntimeWindow) -> None:
        nonlocal current_window_id
        selected_window_id = await _local_runtime_window_to_virtual_window_id(
            client_id,
            runtime_window,
        )
        if selected_window_id is None:
            return
        current_window_id = selected_window_id
        await _terminal_selection_hub(websocket).publish(client_id, selected_window_id)
        await _ui_event_hub(websocket).publish_terminal_selection(client_id, selected_window_id)

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
                            ["agent_record", "window", "tree", "search"],
                            client_id=client_id,
                            window_id=target_window_id,
                            reason="terminal_command",
                        )
                output_event = None
                if clean_data:
                    output_event = await record_terminal_output_chunk(
                        session,
                        client_id,
                        target_window_id,
                        clean_data,
                        _ready_es_client(websocket),
                    )
                if output_event is not None:
                    with contextlib.suppress(Exception):
                        await _ui_event_hub(websocket).publish_debounced_invalidation(
                            ("terminal_output", client_id, target_window_id),
                            ["window", "tree", "search"],
                            client_id=client_id,
                            window_id=target_window_id,
                            reason="terminal_output",
                            delay_seconds=1.0,
                        )
        except Exception:
            logger.exception("terminal output recording failed")

    async def record_and_publish_output(data: bytes) -> None:
        target_window_id = current_window_id
        clean_data, commands = marker_extractor.feed(data)
        is_attach_snapshot = (
            bool(commands or clean_data)
            and not browser_input_seen
            and time.monotonic() - attach_started_at <= ATTACH_SNAPSHOT_GRACE_SECONDS
        )
        if clean_data:
            await broker.publish_output(client_id, target_window_id, clean_data)
        if (commands or clean_data) and not is_attach_snapshot:
            asyncio.create_task(
                _record_local_terminal_output(
                    target_window_id,
                    clean_data,
                    commands,
                    is_attach_snapshot=is_attach_snapshot,
                )
            )

    await websocket.accept()
    await broker.subscribe(client_id, window_id, websocket.send_bytes, websocket.send_text)
    try:
        try:
            await broker.attach(
                client_id,
                window_id,
                runtime_window,
                output_callback=record_and_publish_output if is_local_window else None,
                selection_callback=publish_selection if is_local_window else None,
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
                    await broker.send_input(client_id, window_id, runtime_window, message["bytes"])
                except RemoteClientUnavailable:
                    await _mark_window_disconnected(client_id, window_id)
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
                if isinstance(action, ResizeControl):
                    try:
                        await broker.resize(
                            client_id,
                            window_id,
                            runtime_window,
                            cols=action.cols,
                            rows=action.rows,
                        )
                    except RemoteClientUnavailable:
                        await _mark_window_disconnected(client_id, window_id)
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
                elif isinstance(action, bytes):
                    browser_input_seen = True
                    try:
                        await broker.send_input(client_id, window_id, runtime_window, action)
                    except RemoteClientUnavailable:
                        await _mark_window_disconnected(client_id, window_id)
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
        await _mark_window_disconnected(client_id, window_id)
        await websocket.close(code=1013)
        return
    finally:
        await broker.unsubscribe(client_id, window_id, websocket.send_bytes, websocket.send_text)


@router.websocket("/api/terminal/{window_id}")
async def local_terminal_websocket(
    websocket: WebSocket,
    window_id: UUID,
    tmux_manager: TmuxManager = Depends(get_tmux_manager),
) -> None:
    await terminal_websocket(websocket, LOCAL_CLIENT_ID, window_id, tmux_manager)


@router.websocket("/api/clients/{client_id}/terminal-selection")
async def terminal_selection_websocket(websocket: WebSocket, client_id: UUID) -> None:
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
