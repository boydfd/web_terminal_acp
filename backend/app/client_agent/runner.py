from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass
from uuid import UUID

import websockets

from app.client_agent.agent_tool_watchers import UnifiedAgentToolWatcher
from app.client_agent.agent_idle import AgentIdleSupervisor
from app.services import agent_config as agent_config_service
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
GIT_WORKTREE_REQUEST_CONCURRENCY = 1
logger = logging.getLogger(__name__)


def _view_id_for_message(message: AgentMessage) -> UUID:
    value = message.payload.get("view_id")
    if value is None:
        return _message_window_id(message)
    return UUID(str(value))


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
            idle_supervisor = AgentIdleSupervisor(terminal=terminal, runtime=runtime)
            agent_tool_watcher = UnifiedAgentToolWatcher(
                bulk_writer.send_ai_event,
                config.client_id,
                send_presence=bulk_writer.send_ai_event,
                terminal=terminal,
                runtime=runtime,
                idle_supervisor=idle_supervisor,
            )
            agent_tool_watcher.start()
            attach_snapshot_tasks: dict[UUID, asyncio.Task[None]] = {}
            git_worktree_tasks: set[asyncio.Task[None]] = set()
            git_worktree_semaphore = asyncio.Semaphore(GIT_WORKTREE_REQUEST_CONCURRENCY)
            terminal_view_window_ids: dict[UUID, UUID] = {}
            heartbeat_task: asyncio.Task[None] | None = None
            try:
                inventory = await runtime.list_windows()
                _register_inventory_windows(terminal, inventory)
                for window in inventory:
                    if _should_restore_agent_tool_watcher(window):
                        agent_tool_watcher.watch_window(
                            window.local_window_id,
                            window.cwd,
                        )
                        idle_supervisor.register_window(window.local_window_id, window.cwd)
                await _send_inventory(control_writer, config.client_id, inventory)
                await control_writer.drain()
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
                            idle_supervisor,
                            agent_tool_watcher,
                            attach_snapshot_tasks,
                            git_worktree_tasks,
                            git_worktree_semaphore,
                            terminal_view_window_ids,
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
                pending_git_worktree_tasks = tuple(git_worktree_tasks)
                for task in attach_snapshot_tasks.values():
                    task.cancel()
                for task in pending_git_worktree_tasks:
                    task.cancel()
                if heartbeat_task is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await heartbeat_task
                for task in attach_snapshot_tasks.values():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                for task in pending_git_worktree_tasks:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await agent_tool_watcher.close()
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


@dataclass(frozen=True)
class RuntimeWindowAvailability:
    window: ClientRuntimeWindow
    recreated: bool


async def _ensure_runtime_window_available(
    runtime: ClientTmuxRuntime,
    terminal: ClientTerminalMultiplexer,
    idle_supervisor: AgentIdleSupervisor,
    window_id: UUID,
    *,
    remote_session_id: str,
    remote_window_id: str,
    cwd: str | None = None,
    shell_command: str | None = None,
) -> RuntimeWindowAvailability:
    if await runtime.has_window(remote_window_id, remote_session_id=remote_session_id):
        runtime_window = ClientRuntimeWindow(
            remote_session_id=remote_session_id,
            remote_window_id=remote_window_id,
            local_window_id=window_id,
            cwd=cwd,
            shell_command=shell_command,
            managed_agent_tools=True,
        )
        recreated = False
    else:
        terminal.unregister_window(window_id)
        runtime_window = await runtime.recreate_window(
            window_id,
            cwd=cwd,
            shell_command=shell_command,
        )
        recreated = True

    terminal.register_window(
        window_id,
        runtime_window.remote_session_id,
        runtime_window.remote_window_id,
    )
    idle_supervisor.register_window(window_id, runtime_window.cwd)
    return RuntimeWindowAvailability(window=runtime_window, recreated=recreated)


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


def _agent_config_selection_from_payload(value: object) -> agent_config_service.AgentConfigSelection | None:
    if not isinstance(value, dict):
        return None
    agent = value.get("agent")
    if not isinstance(agent, str):
        return None
    sections: list[agent_config_service.AgentConfigSectionSelection] = []
    raw_sections = value.get("sections")
    if isinstance(raw_sections, list):
        for raw_section in raw_sections:
            if not isinstance(raw_section, dict):
                continue
            section_id = raw_section.get("id")
            if section_id not in {"skills", "plugins", "hooks"}:
                continue
            items: list[agent_config_service.AgentConfigItemSelection] = []
            raw_items = raw_section.get("items")
            if isinstance(raw_items, list):
                for raw_item in raw_items:
                    if not isinstance(raw_item, dict):
                        continue
                    item_id = raw_item.get("id")
                    enabled = raw_item.get("enabled")
                    if isinstance(item_id, str) and item_id and isinstance(enabled, bool):
                        items.append(agent_config_service.AgentConfigItemSelection(item_id, enabled))
            sections.append(agent_config_service.AgentConfigSectionSelection(section_id, items))
    return agent_config_service.AgentConfigSelection(
        agent=agent_config_service.normalize_agent_kind(agent),
        sections=sections,
    )



async def _handle_agent_message(
    control_writer: ControlMessageWriter,
    bulk_writer: BulkUploadWriter,
    config: ClientAgentConfig,
    runtime: ClientTmuxRuntime,
    terminal: ClientTerminalMultiplexer,
    idle_supervisor: AgentIdleSupervisor,
    agent_tool_watcher: UnifiedAgentToolWatcher,
    attach_snapshot_tasks: dict[UUID, asyncio.Task[None]],
    git_worktree_tasks: set[asyncio.Task[None]],
    git_worktree_semaphore: asyncio.Semaphore,
    terminal_view_window_ids: dict[UUID, UUID],
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
        agent_config_selection = _agent_config_selection_from_payload(
            message.payload.get("agent_config_selection")
        )
        if agent_config_selection is not None:
            agent_config_service.apply_agent_config_selection(
                agent_config_selection,
                window_id=str(window_id),
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
        idle_supervisor.register_window(window_id, project_path)
        agent_tool_watcher.watch_window(
            window_id,
            project_path,
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
        agent_tool_watcher.remove_window(window_id)
        idle_supervisor.remove_window(window_id)
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
        view_id = _view_id_for_message(message)
        terminal_view_window_ids[view_id] = window_id
        idle_supervisor.attach_view(view_id, window_id)
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
            terminal_view_window_ids[view_id] = selected_window_id
            idle_supervisor.attach_view(view_id, selected_window_id)
            await idle_supervisor.resume_window(selected_window_id)
            await _send_terminal_selection(
                control_writer,
                message.client_id,
                selected_window_id,
                view_id=view_id,
            )

        async def send_active_terminal_output(data: bytes) -> None:
            first_output_seen.set()
            await _send_terminal_output(
                bulk_writer,
                message.client_id,
                terminal_view_window_ids.get(view_id, current_window_id),
                data,
                view_id=view_id,
                is_snapshot=consume_attach_snapshot(),
            )

        availability = await _ensure_runtime_window_available(
            runtime,
            terminal,
            idle_supervisor,
            window_id,
            remote_session_id=_required_payload_string(message, "remote_session_id"),
            remote_window_id=_required_payload_string(message, "remote_window_id"),
            cwd=_optional_payload_string(message, "cwd"),
            shell_command=_optional_payload_string(message, "shell_command"),
        )
        runtime_window = availability.window
        await idle_supervisor.resume_window(
            window_id,
            allow_latest_session=availability.recreated,
        )
        await terminal.attach_with_selection(
            window_id,
            send_active_terminal_output,
            selection_sender=send_selected_window,
            view_id=view_id,
        )
        existing_snapshot_task = attach_snapshot_tasks.pop(view_id, None)
        if existing_snapshot_task is not None:
            existing_snapshot_task.cancel()
        attach_snapshot_tasks[view_id] = asyncio.create_task(
            _send_attach_snapshot_if_silent(
                bulk_writer,
                message.client_id,
                window_id,
                view_id,
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
            runtime_window=runtime_window,
        )
        return False

    if message.type == "terminal_detach":
        window_id = _message_window_id(message)
        view_id = _view_id_for_message(message)
        snapshot_task = attach_snapshot_tasks.pop(view_id, None)
        if snapshot_task is not None:
            snapshot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await snapshot_task
        await terminal.detach(window_id, view_id=view_id)
        terminal_view_window_ids.pop(view_id, None)
        idle_supervisor.detach_view(view_id)
        return False

    if message.type == "terminal_input":
        payload = TerminalPayload.model_validate(message.payload)
        view_id = _view_id_for_message(message)
        await bulk_writer.prioritize_terminal_window(payload.window_id)
        await terminal.send_input(payload.window_id, payload.to_bytes(), view_id=view_id)
        return False

    if message.type == "terminal_resize":
        window_id = _message_window_id(message)
        view_id = _view_id_for_message(message)
        await terminal.resize(
            window_id,
            cols=int(message.payload["cols"]),
            rows=int(message.payload["rows"]),
            view_id=view_id,
        )
        return False

    if message.type == "terminal_select_window":
        window_id = _message_window_id(message)
        view_id = _view_id_for_message(message)
        availability = await _ensure_runtime_window_available(
            runtime,
            terminal,
            idle_supervisor,
            window_id,
            remote_session_id=_required_payload_string(message, "remote_session_id"),
            remote_window_id=_required_payload_string(message, "remote_window_id"),
            cwd=_optional_payload_string(message, "cwd"),
            shell_command=_optional_payload_string(message, "shell_command"),
        )
        runtime_window = availability.window
        terminal_view_window_ids[view_id] = window_id
        idle_supervisor.attach_view(view_id, window_id)
        await idle_supervisor.resume_window(
            window_id,
            allow_latest_session=availability.recreated,
        )
        await terminal.select_window(window_id, view_id=view_id)
        await _send_terminal_attach_result(
            control_writer,
            message.client_id,
            window_id,
            request_id=message.request_id,
            runtime_window=runtime_window,
        )
        return False

    if message.type == "git_worktree_request":
        task = asyncio.create_task(
            _handle_git_worktree_request_job(
                control_writer,
                git_worktree_semaphore,
                message,
            )
        )
        git_worktree_tasks.add(task)
        task.add_done_callback(git_worktree_tasks.discard)
        return False

    if message.type == "agent_config_get":
        agent = _required_payload_string(message, "agent")
        await control_writer.send(
            AgentMessage(
                type="agent_config_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload=_agent_config_payload(agent_config_service.list_agent_config(agent)),
            )
        )
        return False

    if message.type == "agent_config_set_enabled":
        agent = _required_payload_string(message, "agent")
        section_id = _required_payload_string(message, "section_id")
        item_id = _required_payload_string(message, "item_id")
        enabled = message.payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("agent config enabled must be a boolean")
        await control_writer.send(
            AgentMessage(
                type="agent_config_result",
                client_id=message.client_id,
                window_id=message.window_id,
                request_id=message.request_id,
                payload=_agent_config_payload(
                    agent_config_service.set_agent_config_item_enabled(
                        agent,
                        section_id,
                        item_id,
                        enabled,
                    )
                ),
            )
        )
        return False

    return False


def _agent_config_payload(config: agent_config_service.AgentConfig) -> dict[str, object]:
    return {
        "agent": config.agent,
        "sections": [
            {
                "id": section.id,
                "name": section.name,
                "items": [
                    {
                        "id": item.id,
                        "name": item.name,
                        "enabled": item.enabled,
                        "path": item.path,
                    }
                    for item in section.items
                ],
            }
            for section in config.sections
        ],
    }


async def _handle_git_worktree_request_job(
    control_writer: ControlMessageWriter,
    semaphore: asyncio.Semaphore,
    message: AgentMessage,
) -> None:
    try:
        async with semaphore:
            result = await handle_git_worktree_request(message.payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "client-agent git worktree request failed",
            extra={
                "client_id": str(message.client_id),
                "request_id": message.request_id,
            },
        )
        result = {"ok": False, "error": str(exc)}
    await control_writer.send(
        AgentMessage(
            type="git_worktree_result",
            client_id=message.client_id,
            request_id=message.request_id,
            payload=result,
        )
    )


async def _send_terminal_output(
    writer: BulkUploadWriter,
    client_id: UUID,
    window_id: UUID,
    data: bytes,
    *,
    view_id: UUID | None = None,
    is_snapshot: bool = False,
) -> None:
    if not data:
        return
    payload = TerminalPayload.from_bytes(window_id, data).model_dump(mode="json")
    if view_id is not None:
        payload["view_id"] = str(view_id)
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
    view_id: UUID,
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
        snapshot = await terminal.capture_output_bytes(window_id, view_id=view_id)
    except Exception:
        logger.exception(
            "client-agent terminal attach snapshot failed",
            extra={"client_id": str(client_id), "window_id": str(window_id)},
        )
        return
    if not snapshot.strip() or not consume_attach_snapshot():
        return
    await _send_terminal_output(
        writer,
        client_id,
        window_id,
        snapshot,
        view_id=view_id,
        is_snapshot=True,
    )


async def _send_terminal_selection(
    writer: ControlMessageWriter,
    client_id: UUID,
    window_id: UUID,
    *,
    view_id: UUID | None = None,
) -> None:
    payload = {}
    if view_id is not None:
        payload["view_id"] = str(view_id)
    await writer.send(
        AgentMessage(
            type="terminal_selection",
            client_id=client_id,
            window_id=window_id,
            payload=payload,
        )
    )


async def _send_terminal_attach_result(
    writer: ControlMessageWriter,
    client_id: UUID,
    window_id: UUID,
    *,
    request_id: str | None,
    runtime_window: ClientRuntimeWindow | None = None,
) -> None:
    payload: dict[str, object] = {"ok": True}
    if runtime_window is not None:
        payload.update(
            {
                "remote_session_id": runtime_window.remote_session_id,
                "remote_window_id": runtime_window.remote_window_id,
                "cwd": runtime_window.cwd,
                "shell_command": runtime_window.shell_command,
            }
        )
    await writer.send(
        AgentMessage(
            type="terminal_attach_result",
            client_id=client_id,
            window_id=window_id,
            request_id=request_id,
            payload=payload,
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
