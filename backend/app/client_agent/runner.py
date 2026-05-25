from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import socket
from collections.abc import Callable
from dataclasses import asdict
from uuid import UUID

import websockets

from app.client_agent.agent_tool_watchers import watch_agent_tool_events
from app.client_agent.git_worktree import handle_git_worktree_request
from app.client_agent.config import ClientAgentConfig
from app.client_agent.outbound import BulkUploadWriter, ControlMessageWriter
from app.client_agent.terminal import ClientTerminalMultiplexer
from app.client_agent.tmux_runtime import ClientRuntimeWindow, ClientTmuxRuntime
from app.client_agent.updater import start_self_update
from app.services.runtime.protocol import (
    AgentMessage,
    TerminalPayload,
    decode_agent_message,
    encode_agent_message,
)
from app.version import __version__

HEARTBEAT_INTERVAL_SECONDS = 10
ATTACH_SNAPSHOT_GRACE_SECONDS = 0.25
logger = logging.getLogger(__name__)


async def run_client_agent(config: ClientAgentConfig) -> None:
    reconnect_delay = config.reconnect_initial_delay_seconds
    max_reconnect_delay = config.reconnect_max_delay_seconds

    while True:
        try:
            shutdown_requested = await _run_client_agent_once(config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "client-agent connection failed; reconnecting",
                extra={
                    "client_id": str(config.client_id),
                    "websocket_url": config.websocket_url,
                    "exception_type": type(exc).__name__,
                    "reconnect_delay_seconds": reconnect_delay,
                },
                exc_info=True,
            )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(max_reconnect_delay, reconnect_delay * 2)
            continue

        if shutdown_requested:
            return
        reconnect_delay = config.reconnect_initial_delay_seconds


async def _run_client_agent_once(config: ClientAgentConfig) -> bool:
    headers = {
        "Authorization": f"Bearer {config.token}",
        "X-Client-Id": str(config.client_id),
    }

    header_argument = "extra_headers"
    if "additional_headers" in inspect.signature(websockets.connect).parameters:
        header_argument = "additional_headers"

    logger.info(
        "client-agent connecting",
        extra={"client_id": str(config.client_id), "websocket_url": config.websocket_url},
    )
    async with websockets.connect(
        config.websocket_url,
        ping_interval=None,
        **{header_argument: headers},
    ) as control_websocket:
        await control_websocket.send(
            encode_agent_message(
                AgentMessage(
                    type="hello",
                    client_id=config.client_id,
                    payload={
                        "hostname": socket.gethostname(),
                        "name": config.name,
                        "version": __version__,
                    },
                )
            )
        )
        hello_ack = decode_agent_message(await control_websocket.recv())
        logger.info(
            "client-agent hello acknowledged",
            extra={"client_id": str(config.client_id), "message_type": hello_ack.type},
        )

        logger.info(
            "client-agent bulk websocket connecting",
            extra={"client_id": str(config.client_id), "websocket_url": config.bulk_websocket_url},
        )
        async with websockets.connect(
            config.bulk_websocket_url,
            ping_interval=None,
            **{header_argument: headers},
        ) as bulk_websocket:
            await bulk_websocket.send(
                encode_agent_message(
                    AgentMessage(
                        type="bulk_hello",
                        client_id=config.client_id,
                        payload={"version": __version__},
                    )
                )
            )
            bulk_hello_ack = decode_agent_message(await bulk_websocket.recv())
            if bulk_hello_ack.type != "bulk_hello_ack":
                raise RuntimeError(f"unexpected bulk websocket ack: {bulk_hello_ack.type}")
            logger.info(
                "client-agent bulk hello acknowledged",
                extra={"client_id": str(config.client_id), "message_type": bulk_hello_ack.type},
            )

            control_writer = ControlMessageWriter(control_websocket.send)
            bulk_writer = BulkUploadWriter(bulk_websocket.send)
            control_writer.start()
            bulk_writer.start()

            runtime = ClientTmuxRuntime(
                client_id=config.client_id,
                server_url=config.server_url,
                pool_session=config.tmux_pool_session,
                default_shell=config.default_shell,
            )
            terminal = ClientTerminalMultiplexer()
            agent_watch_tasks: dict[UUID, asyncio.Task[None]] = {}
            attach_snapshot_tasks: dict[UUID, asyncio.Task[None]] = {}
            heartbeat_task: asyncio.Task[None] | None = None
            try:
                inventory = await runtime.list_windows()
                _register_inventory_windows(terminal, inventory)
                for window in inventory:
                    if _should_restore_agent_tool_watcher(window):
                        _ensure_agent_tool_watcher(
                            agent_watch_tasks,
                            bulk_writer,
                            config.client_id,
                            window.local_window_id,
                            window.cwd,
                            terminal=terminal,
                            runtime=runtime,
                        )
                await _send_inventory(control_writer, config.client_id, inventory)
                logger.info(
                    "client-agent inventory sent",
                    extra={"client_id": str(config.client_id), "window_count": len(inventory)},
                )

                heartbeat_task = asyncio.create_task(_heartbeat_loop(control_writer, config.client_id))
                while True:
                    message = decode_agent_message(await control_websocket.recv())
                    try:
                        if await _handle_agent_message(
                            control_writer,
                            bulk_writer,
                            config,
                            runtime,
                            terminal,
                            agent_watch_tasks,
                            attach_snapshot_tasks,
                            message,
                        ):
                            return True
                    except Exception as exc:
                        await _send_terminal_error(
                            control_writer,
                            message.client_id,
                            message.window_id,
                            request_id=message.request_id,
                            message=str(exc),
                        )
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                for task in agent_watch_tasks.values():
                    task.cancel()
                for task in attach_snapshot_tasks.values():
                    task.cancel()
                if heartbeat_task is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await heartbeat_task
                for task in agent_watch_tasks.values():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                for task in attach_snapshot_tasks.values():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                await terminal.close()
                await control_writer.close()
                await bulk_writer.close()

    return False


def _should_restore_agent_tool_watcher(window: ClientRuntimeWindow) -> bool:
    return window.local_window_id is not None and window.managed_agent_tools


def _register_inventory_windows(
    terminal: ClientTerminalMultiplexer,
    windows: list[ClientRuntimeWindow],
) -> None:
    for window in windows:
        if window.local_window_id is not None:
            terminal.register_window(
                window.local_window_id,
                window.remote_session_id,
                window.remote_window_id,
            )


async def _send_inventory(
    writer: ControlMessageWriter,
    client_id: UUID,
    windows: list[ClientRuntimeWindow],
) -> None:
    await writer.send(
        AgentMessage(
            type="inventory",
            client_id=client_id,
            payload={"windows": [asdict(window) for window in windows]},
        )
    )


async def _heartbeat_loop(writer: ControlMessageWriter, client_id: UUID) -> None:
    while True:
        await writer.send(
            AgentMessage(
                type="heartbeat",
                client_id=client_id,
                payload={"version": __version__},
            )
        )
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)



async def _handle_agent_message(
    control_writer: ControlMessageWriter,
    bulk_writer: BulkUploadWriter,
    config: ClientAgentConfig,
    runtime: ClientTmuxRuntime,
    terminal: ClientTerminalMultiplexer,
    agent_watch_tasks: dict[UUID, asyncio.Task[None]],
    attach_snapshot_tasks: dict[UUID, asyncio.Task[None]],
    message: AgentMessage,
) -> bool:
    if message.type == "shutdown":
        return True

    if message.type == "self_update_prepare":
        result = await start_self_update(config, message.payload)
        await control_writer.send(
            AgentMessage(
                type="self_update_started",
                client_id=message.client_id,
                request_id=message.request_id,
                payload=result,
            )
        )
        return False

    if message.type == "create_window":
        window_id = _message_window_id(message)
        logger.info(
            "client-agent create_window started",
            extra={"client_id": str(message.client_id), "window_id": str(window_id)},
        )
        project_path = _optional_payload_string(message, "cwd")
        runtime_window = await runtime.create_window(
            window_id,
            cwd=project_path,
            shell_command=_optional_payload_string(message, "shell_command"),
        )
        terminal.register_window(
            window_id,
            runtime_window.remote_session_id,
            runtime_window.remote_window_id,
        )
        _ensure_agent_tool_watcher(
            agent_watch_tasks,
            bulk_writer,
            message.client_id,
            window_id,
            project_path,
            terminal=terminal,
            runtime=runtime,
        )
        await control_writer.send(
            AgentMessage(
                type="create_window_result",
                client_id=message.client_id,
                window_id=window_id,
                request_id=message.request_id,
                payload=asdict(runtime_window),
            )
        )
        logger.info(
            "client-agent create_window completed",
            extra={
                "client_id": str(message.client_id),
                "window_id": str(window_id),
                "remote_session_id": runtime_window.remote_session_id,
                "remote_window_id": runtime_window.remote_window_id,
            },
        )
        return False

    if message.type == "kill_window":
        window_id = _message_window_id(message)
        await terminal.remove_window(window_id)
        await runtime.kill_window(window_id)
        await control_writer.send(
            AgentMessage(
                type="kill_window_result",
                client_id=message.client_id,
                window_id=window_id,
                request_id=message.request_id,
                payload={},
            )
        )
        return False

    if message.type == "terminal_attach":
        window_id = _message_window_id(message)
        current_window_id = window_id
        first_output_seen = asyncio.Event()
        snapshot_pending = True

        def consume_attach_snapshot() -> bool:
            nonlocal snapshot_pending
            if not snapshot_pending:
                return False
            snapshot_pending = False
            return True

        async def send_selected_window(selected_window_id: UUID) -> None:
            nonlocal current_window_id
            current_window_id = selected_window_id
            await _send_terminal_selection(control_writer, message.client_id, selected_window_id)

        async def send_active_terminal_output(data: bytes) -> None:
            first_output_seen.set()
            await _send_terminal_output(
                bulk_writer,
                message.client_id,
                current_window_id,
                data,
                is_snapshot=consume_attach_snapshot(),
            )

        terminal.register_window(
            window_id,
            _required_payload_string(message, "remote_session_id"),
            _required_payload_string(message, "remote_window_id"),
        )
        await terminal.attach_with_selection(
            window_id,
            send_active_terminal_output,
            selection_sender=send_selected_window,
        )
        existing_snapshot_task = attach_snapshot_tasks.pop(window_id, None)
        if existing_snapshot_task is not None:
            existing_snapshot_task.cancel()
        attach_snapshot_tasks[window_id] = asyncio.create_task(
            _send_attach_snapshot_if_silent(
                bulk_writer,
                message.client_id,
                window_id,
                terminal,
                first_output_seen,
                consume_attach_snapshot,
            )
        )
        await _send_terminal_attach_result(
            control_writer,
            message.client_id,
            window_id,
            request_id=message.request_id,
        )
        return False

    if message.type == "terminal_detach":
        window_id = _message_window_id(message)
        snapshot_task = attach_snapshot_tasks.pop(window_id, None)
        if snapshot_task is not None:
            snapshot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await snapshot_task
        await terminal.detach(window_id)
        return False

    if message.type == "terminal_input":
        payload = TerminalPayload.model_validate(message.payload)
        await terminal.send_input(payload.window_id, payload.to_bytes())
        return False

    if message.type == "terminal_resize":
        window_id = _message_window_id(message)
        await terminal.resize(
            window_id,
            cols=int(message.payload["cols"]),
            rows=int(message.payload["rows"]),
        )
        return False

    if message.type == "git_worktree_request":
        result = await handle_git_worktree_request(message.payload)
        await control_writer.send(
            AgentMessage(
                type="git_worktree_result",
                client_id=message.client_id,
                request_id=message.request_id,
                payload=result,
            )
        )
        return False

    return False


def _ensure_agent_tool_watcher(
    tasks: dict[UUID, asyncio.Task[None]],
    bulk_writer: BulkUploadWriter,
    client_id: UUID,
    window_id: UUID,
    project_path: str | None,
    *,
    terminal: ClientTerminalMultiplexer,
    runtime: ClientTmuxRuntime,
) -> None:
    existing = tasks.get(window_id)
    if existing is not None and not existing.done():
        return
    tasks[window_id] = asyncio.create_task(
        watch_agent_tool_events(
            bulk_writer.send_ai_event,
            client_id,
            window_id,
            project_path,
            send_presence=bulk_writer.send_ai_event,
            terminal=terminal,
            runtime=runtime,
        )
    )


async def _send_terminal_output(
    writer: BulkUploadWriter,
    client_id: UUID,
    window_id: UUID,
    data: bytes,
    *,
    is_snapshot: bool = False,
) -> None:
    if not data:
        return
    payload = TerminalPayload.from_bytes(window_id, data).model_dump(mode="json")
    if is_snapshot:
        payload["is_snapshot"] = True
    await writer.send_terminal_output(
        AgentMessage(
            type="terminal_output",
            client_id=client_id,
            window_id=window_id,
            payload=payload,
        )
    )


async def _send_attach_snapshot_if_silent(
    writer: BulkUploadWriter,
    client_id: UUID,
    window_id: UUID,
    terminal: ClientTerminalMultiplexer,
    first_output_seen: asyncio.Event,
    consume_attach_snapshot: Callable[[], bool],
) -> None:
    try:
        await asyncio.wait_for(first_output_seen.wait(), timeout=ATTACH_SNAPSHOT_GRACE_SECONDS)
        return
    except asyncio.TimeoutError:
        pass

    try:
        snapshot = await terminal.capture_output_bytes(window_id)
    except Exception:
        logger.exception(
            "client-agent terminal attach snapshot failed",
            extra={"client_id": str(client_id), "window_id": str(window_id)},
        )
        return
    if not snapshot.strip() or not consume_attach_snapshot():
        return
    await _send_terminal_output(writer, client_id, window_id, snapshot, is_snapshot=True)


async def _send_terminal_selection(
    writer: ControlMessageWriter,
    client_id: UUID,
    window_id: UUID,
) -> None:
    await writer.send(
        AgentMessage(
            type="terminal_selection",
            client_id=client_id,
            window_id=window_id,
        )
    )


async def _send_terminal_attach_result(
    writer: ControlMessageWriter,
    client_id: UUID,
    window_id: UUID,
    *,
    request_id: str | None,
) -> None:
    await writer.send(
        AgentMessage(
            type="terminal_attach_result",
            client_id=client_id,
            window_id=window_id,
            request_id=request_id,
            payload={"ok": True},
        )
    )


async def _send_terminal_error(
    writer: ControlMessageWriter,
    client_id: UUID,
    window_id: UUID | None,
    *,
    request_id: str | None,
    message: str,
) -> None:
    await writer.send(
        AgentMessage(
            type="terminal_error",
            client_id=client_id,
            window_id=window_id,
            request_id=request_id,
            payload={"message": message},
        )
    )


def _message_window_id(message: AgentMessage) -> UUID:
    if message.window_id is not None:
        return message.window_id
    payload_window_id = message.payload.get("window_id")
    if payload_window_id is not None:
        return UUID(str(payload_window_id))
    raise ValueError(f"agent message requires window_id: {message.type}")


def _required_payload_string(message: AgentMessage, key: str) -> str:
    value = message.payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"agent message requires payload string: {key}")
    return value


def _optional_payload_string(message: AgentMessage, key: str) -> str | None:
    value = message.payload.get(key)
    return value if isinstance(value, str) and value else None
