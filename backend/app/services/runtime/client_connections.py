from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from uuid import UUID

from fastapi import WebSocket

from app.services.runtime.protocol import AgentMessage, encode_agent_message

logger = logging.getLogger(__name__)


class ClientConnectionClosed(RuntimeError):
    """Raised when a request waits on a closed client-agent connection."""


class ClientConnection:
    def __init__(self, *, websocket: WebSocket, client_id: UUID):
        self.websocket = websocket
        self.client_id = client_id
        self.last_seen_at = datetime.now(UTC)
        self._pending: dict[str, asyncio.Future[AgentMessage]] = {}
        self._closed = False
        self._send_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    async def send(self, message: AgentMessage) -> None:
        if self._closed:
            raise ClientConnectionClosed(f"client connection closed: {self.client_id}")
        started_at = time.perf_counter()
        try:
            async with self._send_lock:
                await self.websocket.send_text(encode_agent_message(message))
        except RuntimeError as exc:
            self.abort(exc)
            raise ClientConnectionClosed(f"client connection closed: {self.client_id}") from exc
        elapsed = time.perf_counter() - started_at
        if elapsed >= 1.0:
            logger.warning(
                "client-agent websocket send was slow",
                extra={
                    "client_id": str(self.client_id),
                    "message_type": message.type,
                    "request_id": message.request_id,
                    "elapsed_seconds": round(elapsed, 3),
                },
            )

    async def request(self, message: AgentMessage, *, timeout: float) -> AgentMessage:
        if self._closed:
            raise ClientConnectionClosed(f"client connection closed: {self.client_id}")
        if message.request_id is None:
            raise ValueError("client-agent requests require request_id")
        if message.request_id in self._pending:
            raise ValueError(f"request_id already pending: {message.request_id}")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[AgentMessage] = loop.create_future()
        self._pending[message.request_id] = future
        try:
            await self.send(message)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(message.request_id, None)

    def abort(self, exc: BaseException | None = None) -> None:
        self._closed = True
        failure = exc or ClientConnectionClosed(f"client connection closed: {self.client_id}")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        self._pending.clear()

    async def close(self, *, code: int = 1000) -> None:
        self.abort()
        with contextlib.suppress(Exception):
            await self.websocket.close(code=code)

    def resolve(self, message: AgentMessage) -> bool:
        if message.request_id is None:
            return False
        future = self._pending.get(message.request_id)
        if future is None or future.done():
            return False
        future.set_result(message)
        return True

    def mark_seen(self) -> datetime:
        self.last_seen_at = datetime.now(UTC)
        return self.last_seen_at


class ClientConnectionRegistry:
    def __init__(self) -> None:
        self._connections: dict[UUID, ClientConnection] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        client_id_or_connection: UUID | ClientConnection,
        connection: ClientConnection | None = None,
    ) -> ClientConnection:
        if connection is None:
            if not isinstance(client_id_or_connection, ClientConnection):
                raise TypeError("connection is required when registering by client_id")
            connection = client_id_or_connection
            client_id = connection.client_id
        else:
            client_id = client_id_or_connection
            if not isinstance(client_id, UUID):
                raise TypeError("client_id must be a UUID")

        async with self._lock:
            existing = self._connections.get(client_id)
            if existing is not None and existing is not connection:
                logger.info(
                    "replacing client-agent connection",
                    extra={"client_id": str(client_id)},
                )
                await existing.close()
            self._connections[client_id] = connection
        return connection

    async def unregister(
        self,
        client_id_or_connection: UUID | ClientConnection,
        connection: ClientConnection | None = None,
    ) -> None:
        if isinstance(client_id_or_connection, ClientConnection):
            connection = client_id_or_connection
            client_id = connection.client_id
        else:
            client_id = client_id_or_connection

        async with self._lock:
            current = self._connections.get(client_id)
            if connection is None or current is connection:
                self._connections.pop(client_id, None)

    def get(self, client_id: UUID) -> ClientConnection | None:
        return self._connections.get(client_id)
