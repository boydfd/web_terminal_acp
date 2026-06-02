from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_websocket_auth
from app.db import SessionLocal, get_session
from app.models import ClientRuntime
from app.repositories.clients import get_client
from app.repositories.windows import get_window_for_client
from app.services.aux_terminal import (
    AuxTerminalUnavailable,
    attach_local_aux_terminal,
    attach_remote_aux_terminal,
    aux_terminal_registry_from_state,
    cwd_for_aux_terminal,
    detach_local_aux_terminal,
    detach_remote_aux_terminal,
    ensure_remote_aux_terminal,
    resize_local_aux_terminal,
    resize_remote_aux_terminal,
    send_local_aux_input,
    send_remote_aux_input,
    shell_for_aux_terminal,
)
from app.services.runtime.client_connections import ClientConnectionRegistry
from app.services.runtime.broker import TerminalBroker, terminal_status_message
from app.services.terminal_bridge import OutputAckControl, ResizeControl, parse_text_input

router = APIRouter(prefix="/api", tags=["aux-terminal"])


def _client_connection_registry(scope) -> ClientConnectionRegistry:
    registry = getattr(scope.app.state, "client_connections", None)
    if registry is None:
        registry = ClientConnectionRegistry()
        scope.app.state.client_connections = registry
    return registry


def _terminal_broker(scope) -> TerminalBroker:
    broker = getattr(scope.app.state, "terminal_broker", None)
    if broker is None:
        broker = TerminalBroker()
        scope.app.state.terminal_broker = broker
    return broker


@router.post("/clients/{client_id}/windows/{window_id}/aux-terminal/ensure")
async def ensure_aux_terminal(
    request: Request,
    client_id: UUID,
    window_id: UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str | None]:
    client = await get_client(session, client_id)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client not found")
    window = await get_window_for_client(session, client_id, window_id)
    if window is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="window not found")

    cwd = cwd_for_aux_terminal(window)
    shell_command = shell_for_aux_terminal(window)
    if client.runtime is ClientRuntime.local:
        runtime = await aux_terminal_registry_from_state(request.app.state).local_runtime(
            client_id,
            window_id,
            cwd=cwd,
            shell_command=shell_command,
        )
        return {"status": "ready", "cwd": runtime.cwd}

    try:
        await ensure_remote_aux_terminal(
            client_id=client_id,
            parent_window_id=window_id,
            cwd=cwd,
            shell_command=shell_command,
            registry=_client_connection_registry(request),
        )
    except AuxTerminalUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="remote aux terminal unavailable",
        ) from exc
    return {"status": "ready", "cwd": cwd}


@router.websocket("/clients/{client_id}/windows/{window_id}/aux-terminal")
async def aux_terminal_websocket(
    websocket: WebSocket,
    client_id: UUID,
    window_id: UUID,
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
        client = await get_client(session, client_id)
        if client is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        window = await get_window_for_client(session, client_id, window_id)
        if window is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        cwd = cwd_for_aux_terminal(window)
        shell_command = shell_for_aux_terminal(window)
        client_runtime = client.runtime

    await websocket.accept()
    await websocket.send_text(terminal_status_message("connecting"))

    if client_runtime is ClientRuntime.local:
        runtime = await aux_terminal_registry_from_state(websocket.app.state).local_runtime(
            client_id,
            window_id,
            cwd=cwd,
            shell_command=shell_command,
        )
        attachment = await attach_local_aux_terminal(runtime, websocket.send_bytes)
        await websocket.send_text(terminal_status_message("connected"))
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    await send_local_aux_input(attachment, message["bytes"])
                    continue
                if message.get("text") is not None:
                    action = parse_text_input(message["text"])
                    if isinstance(action, ResizeControl):
                        await resize_local_aux_terminal(attachment, action)
                    elif isinstance(action, bytes):
                        await send_local_aux_input(attachment, action)
        except WebSocketDisconnect:
            return
        finally:
            await detach_local_aux_terminal(attachment)
        return

    registry = _client_connection_registry(websocket)
    broker = _terminal_broker(websocket)
    output_sender = websocket.send_bytes
    await broker.subscribe(client_id, view_id, output_sender, websocket.send_text)
    try:
        aux_terminal_id = await ensure_remote_aux_terminal(
            client_id=client_id,
            parent_window_id=window_id,
            cwd=cwd,
            shell_command=shell_command,
            registry=registry,
        )
        await attach_remote_aux_terminal(
            client_id=client_id,
            parent_window_id=window_id,
            aux_terminal_id=aux_terminal_id,
            view_id=view_id,
            registry=registry,
        )
    except AuxTerminalUnavailable:
        await websocket.send_text(
            terminal_status_message("unavailable", reason="client_offline", retry_after_ms=5000)
        )
        await websocket.close(code=1013)
        return

    await websocket.send_text(terminal_status_message("connected"))
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await send_remote_aux_input(
                    client_id=client_id,
                    parent_window_id=window_id,
                    aux_terminal_id=aux_terminal_id,
                    view_id=view_id,
                    data=message["bytes"],
                    registry=registry,
                )
                continue
            if message.get("text") is not None:
                action = parse_text_input(message["text"])
                if isinstance(action, ResizeControl):
                    await resize_remote_aux_terminal(
                        client_id=client_id,
                        parent_window_id=window_id,
                        aux_terminal_id=aux_terminal_id,
                        view_id=view_id,
                        resize=action,
                        registry=registry,
                    )
                elif isinstance(action, OutputAckControl):
                    await broker.acknowledge_output(
                        client_id,
                        view_id,
                        output_sender,
                        bytes_acked=action.bytes_acked,
                    )
                elif isinstance(action, bytes):
                    await send_remote_aux_input(
                        client_id=client_id,
                        parent_window_id=window_id,
                        aux_terminal_id=aux_terminal_id,
                        view_id=view_id,
                        data=action,
                        registry=registry,
                    )
    except WebSocketDisconnect:
        return
    except AuxTerminalUnavailable:
        await websocket.close(code=1013)
        return
    finally:
        await broker.unsubscribe(client_id, view_id, output_sender, websocket.send_text)
        await detach_remote_aux_terminal(
            client_id=client_id,
            parent_window_id=window_id,
            aux_terminal_id=aux_terminal_id,
            view_id=view_id,
            registry=registry,
        )
