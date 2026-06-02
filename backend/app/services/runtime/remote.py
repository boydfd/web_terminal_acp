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
        agent_config_selection: dict[str, object] | None = None,
        agent_profile_id: str | None = None,
        agent_profile_agent: str | None = None,
    ) -> RuntimeWindow:
        if window_id is None:
            raise ValueError("remote runtime create_window requires window_id")

        connection = self._connection()
        payload: dict[str, object] = {"cwd": cwd, "shell_command": shell_command}
        if agent_config_selection is not None:
            payload["agent_config_selection"] = agent_config_selection
        if agent_profile_id is not None:
            payload["agent_profile_id"] = agent_profile_id
        if agent_profile_agent is not None:
            payload["agent_profile_agent"] = agent_profile_agent
        request = AgentMessage(
            type="create_window",
            client_id=self._client_id,
            window_id=window_id,
            request_id=str(uuid4()),
            payload=payload,
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
        return _runtime_window_from_create_response(response, cwd=cwd, shell_command=shell_command)

    async def attach(
        self,
        window: RuntimeWindow,
        sender: TerminalSender,
        *,
        local_window_id: object | None = None,
        selection_callback: TerminalSelectionCallback | None = None,
        view_id: UUID | str | None = None,
    ) -> RuntimeWindow | None:
        if local_window_id is None:
            raise ValueError("remote runtime attach requires local_window_id")
        window_id = UUID(str(local_window_id))
        effective_view_id = UUID(str(view_id)) if view_id is not None else window_id
        request = AgentMessage(
            type="terminal_attach",
            client_id=self._client_id,
            window_id=window_id,
            request_id=str(uuid4()),
            payload={
                "remote_session_id": window.session_id,
                "remote_window_id": window.window_id,
                "view_id": str(effective_view_id),
                "cwd": window.cwd,
                "shell_command": window.shell_command,
            },
        )
        _drop_none_payload_values(request.payload, "cwd", "shell_command")
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
        self._sizes.pop(effective_view_id, None)
        return _runtime_window_from_response(response, fallback=window)

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

    async def get_agent_config(self, *, agent: str, window_id: UUID | None = None) -> dict[str, object]:
        response = await self._request_agent_config(
            AgentMessage(
                type="agent_config_get",
                client_id=self._client_id,
                window_id=window_id,
                request_id=str(uuid4()),
                payload={"agent": agent},
            )
        )
        return dict(response.payload)

    async def get_agent_profile_config(self, *, profile_id: str, agent: str) -> dict[str, object]:
        response = await self._request_agent_config(
            AgentMessage(
                type="agent_profile_config_get",
                client_id=self._client_id,
                request_id=str(uuid4()),
                payload={"profile_id": profile_id, "agent": agent},
            )
        )
        return dict(response.payload)

    async def list_agent_profiles(self) -> dict[str, object]:
        response = await self._request_agent_profile(
            AgentMessage(
                type="agent_profile_list",
                client_id=self._client_id,
                request_id=str(uuid4()),
                payload={},
            )
        )
        return dict(response.payload)

    async def list_agent_clients(self) -> dict[str, object]:
        response = await self._request_agent_client(
            AgentMessage(
                type="agent_clients_list",
                client_id=self._client_id,
                request_id=str(uuid4()),
                payload={},
            )
        )
        return dict(response.payload)

    async def create_agent_profile(self, payload: dict[str, object]) -> dict[str, object]:
        response = await self._request_agent_profile(
            AgentMessage(
                type="agent_profile_create",
                client_id=self._client_id,
                request_id=str(uuid4()),
                payload=payload,
            )
        )
        return dict(response.payload)

    async def update_agent_profile(self, profile_id: str, payload: dict[str, object]) -> dict[str, object]:
        response = await self._request_agent_profile(
            AgentMessage(
                type="agent_profile_update",
                client_id=self._client_id,
                request_id=str(uuid4()),
                payload={"profile_id": profile_id, **payload},
            )
        )
        return dict(response.payload)

    async def delete_agent_profile(self, profile_id: str) -> None:
        await self._request_agent_profile(
            AgentMessage(
                type="agent_profile_delete",
                client_id=self._client_id,
                request_id=str(uuid4()),
                payload={"profile_id": profile_id},
            )
        )

    async def set_agent_profile_config_enabled(
        self,
        *,
        profile_id: str,
        agent: str,
        section_id: str,
        item_id: str,
        enabled: bool,
    ) -> dict[str, object]:
        response = await self._request_agent_profile(
            AgentMessage(
                type="agent_profile_config_set_enabled",
                client_id=self._client_id,
                request_id=str(uuid4()),
                payload={
                    "profile_id": profile_id,
                    "agent": agent,
                    "section_id": section_id,
                    "item_id": item_id,
                    "enabled": enabled,
                },
            )
        )
        return dict(response.payload)

    async def set_agent_config_enabled(
        self,
        *,
        window_id: UUID,
        agent: str,
        section_id: str,
        item_id: str,
        enabled: bool,
    ) -> dict[str, object]:
        response = await self._request_agent_config(
            AgentMessage(
                type="agent_config_set_enabled",
                client_id=self._client_id,
                window_id=window_id,
                request_id=str(uuid4()),
                payload={
                    "agent": agent,
                    "section_id": section_id,
                    "item_id": item_id,
                    "enabled": enabled,
                },
            )
        )
        return dict(response.payload)

    async def _request_agent_config(self, request: AgentMessage) -> AgentMessage:
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
        if response.type != "agent_config_result":
            raise RemoteTerminalError(f"unexpected agent config response: {response.type}")
        return response

    async def _request_agent_profile(self, request: AgentMessage) -> AgentMessage:
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
        if response.type not in {"agent_profile_result", "agent_config_result"}:
            raise RemoteTerminalError(f"unexpected agent profile response: {response.type}")
        return response

    async def _request_agent_client(self, request: AgentMessage) -> AgentMessage:
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
        if response.type != "agent_client_result":
            raise RemoteTerminalError(f"unexpected agent client response: {response.type}")
        return response

    async def detach(
        self,
        window: RuntimeWindow,
        *,
        local_window_id: object | None = None,
        view_id: UUID | str | None = None,
    ) -> None:
        if local_window_id is None:
            return
        window_id = UUID(str(local_window_id))
        effective_view_id = UUID(str(view_id)) if view_id is not None else window_id
        try:
            await self._connection().send(
                AgentMessage(
                    type="terminal_detach",
                    client_id=self._client_id,
                    window_id=window_id,
                    payload={
                        "remote_session_id": window.session_id,
                        "remote_window_id": window.window_id,
                        "view_id": str(effective_view_id),
                    },
                )
            )
        except ClientConnectionClosed as exc:
            raise RemoteClientUnavailable(
                f"remote client unavailable: {self._client_id}",
                reason="connection_closed",
            ) from exc
        finally:
            self._sizes.pop(effective_view_id, None)

    async def send_input(
        self,
        window: RuntimeWindow,
        data: bytes,
        *,
        local_window_id: UUID | None = None,
        view_id: UUID | str | None = None,
    ) -> None:
        if local_window_id is None:
            raise ValueError("remote runtime send_input requires local_window_id")
        effective_view_id = UUID(str(view_id)) if view_id is not None else local_window_id
        try:
            await self._connection().send(
                AgentMessage(
                    type="terminal_input",
                    client_id=self._client_id,
                    window_id=local_window_id,
                    payload={
                        **TerminalPayload.from_bytes(local_window_id, data).model_dump(mode="json"),
                        "view_id": str(effective_view_id),
                    },
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
        view_id: UUID | str | None = None,
    ) -> None:
        if local_window_id is None:
            raise ValueError("remote runtime resize requires local_window_id")
        effective_view_id = UUID(str(view_id)) if view_id is not None else local_window_id
        size = (cols, rows)
        if self._sizes.get(effective_view_id) == size:
            return
        try:
            await self._connection().send(
                AgentMessage(
                    type="terminal_resize",
                    client_id=self._client_id,
                    window_id=local_window_id,
                    payload={"cols": cols, "rows": rows, "view_id": str(effective_view_id)},
                )
            )
        except ClientConnectionClosed as exc:
            raise RemoteClientUnavailable(
                f"remote client unavailable: {self._client_id}",
                reason="connection_closed",
            ) from exc
        self._sizes[effective_view_id] = size

    async def select_window(
        self,
        current_window: RuntimeWindow,
        next_window: RuntimeWindow,
        *,
        local_window_id: object,
        view_id: UUID | str | None = None,
    ) -> RuntimeWindow | None:
        next_window_id = UUID(str(local_window_id))
        effective_view_id = UUID(str(view_id)) if view_id is not None else next_window_id
        request = AgentMessage(
            type="terminal_select_window",
            client_id=self._client_id,
            window_id=next_window_id,
            request_id=str(uuid4()),
            payload={
                "remote_session_id": next_window.session_id,
                "remote_window_id": next_window.window_id,
                "view_id": str(effective_view_id),
                "cwd": next_window.cwd,
                "shell_command": next_window.shell_command,
            },
        )
        _drop_none_payload_values(request.payload, "cwd", "shell_command")
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
        return _runtime_window_from_response(response, fallback=next_window)

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


def _runtime_window_from_response(
    response: AgentMessage,
    *,
    fallback: RuntimeWindow,
) -> RuntimeWindow:
    remote_session_id = response.payload.get("remote_session_id")
    remote_window_id = response.payload.get("remote_window_id")
    if not isinstance(remote_session_id, str) or not isinstance(remote_window_id, str):
        return fallback
    response_cwd = response.payload.get("cwd")
    response_shell = response.payload.get("shell_command")
    return RuntimeWindow(
        session_id=remote_session_id,
        window_id=remote_window_id,
        cwd=response_cwd if isinstance(response_cwd, str) else fallback.cwd,
        shell_command=response_shell if isinstance(response_shell, str) else fallback.shell_command,
    )


def _runtime_window_from_create_response(
    response: AgentMessage,
    *,
    cwd: str | None,
    shell_command: str | None,
) -> RuntimeWindow:
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


def _drop_none_payload_values(payload: dict[str, object], *keys: str) -> None:
    for key in keys:
        if payload.get(key) is None:
            payload.pop(key, None)
