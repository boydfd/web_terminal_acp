from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from app.services.runtime.client_connections import ClientConnectionClosed, ClientConnectionRegistry
from app.services.runtime.protocol import AgentMessage, TerminalPayload
from app.services.runtime.types import RuntimeWindow, TerminalSelectionCallback, TerminalSender


class RemoteClientUnavailable(RuntimeError):
    """Raised when a remote client has no active client-agent connection."""

    def __init__(self, message: str, *, reason: str = "unknown") -> None:
        super().__init__(message)
        self.reason = reason


class RemoteTerminalError(RuntimeError):
    """Raised when the remote client reports a terminal operation failure."""


class RemoteRuntime:
    def __init__(
        self,
        *,
        client_id: UUID,
        registry: ClientConnectionRegistry,
        request_timeout: float = 10.0,
    ) -> None:
        self._client_id = client_id
        self._registry = registry
        self._request_timeout = request_timeout
        self._sizes: dict[UUID, tuple[int, int]] = {}

    async def create_window(
        self,
        cwd: str | None = None,
        shell_command: str | None = None,
        *,
        window_id: UUID | None = None,
    ) -> RuntimeWindow:
        if window_id is None:
            raise ValueError("remote runtime create_window requires window_id")

        connection = self._connection()
        request = AgentMessage(
            type="create_window",
            client_id=self._client_id,
            window_id=window_id,
            request_id=str(uuid4()),
            payload={"cwd": cwd, "shell_command": shell_command},
        )
        try:
            response = await connection.request(request, timeout=self._request_timeout)
        except ClientConnectionClosed as exc:
            raise RemoteClientUnavailable(
                f"remote client unavailable: {self._client_id}",
                reason="connection_closed",
            ) from exc
        except asyncio.TimeoutError as exc:
            raise RemoteClientUnavailable(
                f"remote client unavailable: {self._client_id}",
                reason="request_timeout",
            ) from exc
        if response.type == "terminal_error":
            raise RemoteTerminalError(_message_from_error_response(response))
        remote_session_id = response.payload.get("remote_session_id")
        remote_window_id = response.payload.get("remote_window_id")
        if not isinstance(remote_session_id, str) or not isinstance(remote_window_id, str):
            raise ValueError("create_window response missing remote session/window ids")
        response_cwd = response.payload.get("cwd")
        response_shell = response.payload.get("shell_command")
        return RuntimeWindow(
            session_id=remote_session_id,
            window_id=remote_window_id,
            cwd=response_cwd if isinstance(response_cwd, str) else cwd,
            shell_command=response_shell if isinstance(response_shell, str) else shell_command,
        )

    async def attach(
        self,
        window: RuntimeWindow,
        sender: TerminalSender,
        *,
        local_window_id: object | None = None,
        selection_callback: TerminalSelectionCallback | None = None,
    ) -> None:
        if local_window_id is None:
            raise ValueError("remote runtime attach requires local_window_id")
        window_id = UUID(str(local_window_id))
        request = AgentMessage(
            type="terminal_attach",
            client_id=self._client_id,
            window_id=window_id,
            request_id=str(uuid4()),
            payload={
                "remote_session_id": window.session_id,
                "remote_window_id": window.window_id,
            },
        )
        try:
            response = await self._connection().request(request, timeout=self._request_timeout)
        except ClientConnectionClosed as exc:
            raise RemoteClientUnavailable(
                f"remote client unavailable: {self._client_id}",
                reason="connection_closed",
            ) from exc
        except asyncio.TimeoutError as exc:
            raise RemoteClientUnavailable(
                f"remote client unavailable: {self._client_id}",
                reason="request_timeout",
            ) from exc
        if response.type == "terminal_error":
            raise RemoteTerminalError(_message_from_error_response(response))
        self._sizes.pop(window_id, None)

    async def kill_window(
        self,
        *,
        window_id: UUID,
        remote_session_id: str | None = None,
        remote_window_id: str | None = None,
    ) -> None:
        connection = self._registry.get(self._client_id)
        if connection is None or getattr(connection, "closed", False):
            return

        payload: dict[str, str] = {}
        if isinstance(remote_session_id, str):
            payload["remote_session_id"] = remote_session_id
        if isinstance(remote_window_id, str):
            payload["remote_window_id"] = remote_window_id
        request = AgentMessage(
            type="kill_window",
            client_id=self._client_id,
            window_id=window_id,
            request_id=str(uuid4()),
            payload=payload,
        )
        try:
            response = await connection.request(request, timeout=self._request_timeout)
        except (ClientConnectionClosed, asyncio.TimeoutError):
            return
        if response.type == "terminal_error":
            raise RemoteTerminalError(_message_from_error_response(response))

    async def detach(
        self,
        window: RuntimeWindow,
        *,
        local_window_id: object | None = None,
    ) -> None:
        if local_window_id is None:
            return
        window_id = UUID(str(local_window_id))
        try:
            await self._connection().send(
                AgentMessage(
                    type="terminal_detach",
                    client_id=self._client_id,
                    window_id=window_id,
                    payload={
                        "remote_session_id": window.session_id,
                        "remote_window_id": window.window_id,
                    },
                )
            )
        except ClientConnectionClosed as exc:
            raise RemoteClientUnavailable(
                f"remote client unavailable: {self._client_id}",
                reason="connection_closed",
            ) from exc
        finally:
            self._sizes.pop(window_id, None)

    async def send_input(
        self,
        window: RuntimeWindow,
        data: bytes,
        *,
        local_window_id: UUID | None = None,
    ) -> None:
        if local_window_id is None:
            raise ValueError("remote runtime send_input requires local_window_id")
        try:
            await self._connection().send(
                AgentMessage(
                    type="terminal_input",
                    client_id=self._client_id,
                    window_id=local_window_id,
                    payload=TerminalPayload.from_bytes(local_window_id, data).model_dump(mode="json"),
                )
            )
        except ClientConnectionClosed as exc:
            raise RemoteClientUnavailable(
                f"remote client unavailable: {self._client_id}",
                reason="connection_closed",
            ) from exc

    async def resize(
        self,
        window: RuntimeWindow,
        *,
        cols: int,
        rows: int,
        local_window_id: UUID | None = None,
    ) -> None:
        if local_window_id is None:
            raise ValueError("remote runtime resize requires local_window_id")
        size = (cols, rows)
        if self._sizes.get(local_window_id) == size:
            return
        try:
            await self._connection().send(
                AgentMessage(
                    type="terminal_resize",
                    client_id=self._client_id,
                    window_id=local_window_id,
                    payload={"cols": cols, "rows": rows},
                )
            )
        except ClientConnectionClosed as exc:
            raise RemoteClientUnavailable(
                f"remote client unavailable: {self._client_id}",
                reason="connection_closed",
            ) from exc
        self._sizes[local_window_id] = size

    def _connection(self):
        connection = self._registry.get(self._client_id)
        if connection is None or getattr(connection, "closed", False):
            reason = "no_connection" if connection is None else "connection_closed"
            raise RemoteClientUnavailable(
                f"remote client unavailable: {self._client_id}",
                reason=reason,
            )
        return connection


def _message_from_error_response(response: AgentMessage) -> str:
    message = response.payload.get("message")
    if isinstance(message, str) and message:
        return message
    return f"remote terminal operation failed: {response.type}"
