from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from app.services.runtime.types import (
    RuntimeWindow,
    TerminalRuntime,
    TerminalSelectionCallback,
    TerminalSender,
)

logger = logging.getLogger(__name__)

# Maximum time a single subscriber is allowed to spend processing a publish
# before the broker considers it unresponsive and drops it. A subscriber that
# blocks here (e.g. a browser WebSocket whose TCP send buffer is full because
# the client is too slow / has died without RST) would otherwise stall the
# bulk-WS worker that publishes terminal output, which in turn back-pressures
# all the way to the client agent and starves new output from any other
# terminal sharing the same bulk connection.
PUBLISH_OUTPUT_SUBSCRIBER_TIMEOUT_SECONDS = 5.0


class TerminalRuntimeUnavailable(RuntimeError):
    """Raised when no terminal runtime is registered for a client."""


TerminalStatusSender = Callable[[str], Awaitable[None]]


def terminal_status_message(
    status: str,
    *,
    reason: str | None = None,
    retry_after_ms: int | None = None,
) -> str:
    payload: dict[str, object] = {"type": "terminal_status", "status": status}
    if reason is not None:
        payload["reason"] = reason
    if retry_after_ms is not None:
        payload["retry_after_ms"] = retry_after_ms
    return json.dumps(payload, separators=(",", ":"))


class TerminalBroker:
    def __init__(self) -> None:
        self._runtimes: dict[UUID, TerminalRuntime] = {}
        self._subscribers: dict[tuple[UUID, UUID], set[TerminalSender]] = {}
        self._status_subscribers: dict[tuple[UUID, UUID], set[TerminalStatusSender]] = {}
        self._attachments: dict[tuple[UUID, UUID], RuntimeWindow] = {}
        self._detaches: dict[tuple[UUID, UUID], asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    def register_runtime(self, client_id: UUID, runtime: TerminalRuntime) -> None:
        self._runtimes[client_id] = runtime

    def runtime_for(self, client_id: UUID) -> TerminalRuntime | None:
        return self._runtimes.get(client_id)

    async def subscribe(
        self,
        client_id: UUID,
        window_id: UUID,
        sender: TerminalSender,
        status_sender: TerminalStatusSender | None = None,
    ) -> None:
        async with self._lock:
            key = (client_id, window_id)
            self._subscribers.setdefault(key, set()).add(sender)
            if status_sender is not None:
                self._status_subscribers.setdefault(key, set()).add(status_sender)

    async def unsubscribe(
        self,
        client_id: UUID,
        window_id: UUID,
        sender: TerminalSender,
        status_sender: TerminalStatusSender | None = None,
    ) -> None:
        key = (client_id, window_id)
        detach_task: asyncio.Task[None] | None = None
        async with self._lock:
            subscribers = self._subscribers.get(key)
            if subscribers is not None:
                subscribers.discard(sender)
                if not subscribers:
                    self._subscribers.pop(key, None)

            status_subscribers = self._status_subscribers.get(key)
            if status_subscribers is not None and status_sender is not None:
                status_subscribers.discard(status_sender)
                if not status_subscribers:
                    self._status_subscribers.pop(key, None)

            if key not in self._subscribers and key not in self._status_subscribers:
                self._subscribers.pop(key, None)
                self._status_subscribers.pop(key, None)
                runtime_window = self._attachments.pop(key, None)
                runtime = self._runtimes.get(client_id)
                if runtime_window is not None and runtime is not None:
                    detach_task = asyncio.create_task(
                        runtime.detach(runtime_window, local_window_id=window_id)
                    )
                    self._detaches[key] = detach_task

        if detach_task is not None:
            with contextlib.suppress(Exception):
                await detach_task
            async with self._lock:
                if self._detaches.get(key) is detach_task:
                    self._detaches.pop(key, None)

    async def publish_output(self, client_id: UUID, window_id: UUID, data: bytes) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers.get((client_id, window_id), ()))

        if not subscribers:
            return

        # Fan out to every subscriber concurrently with an independent timeout
        # so a single slow / dead subscriber (e.g. browser TCP buffer full,
        # half-open WebSocket) cannot stall the bulk-WS worker. Anyone that
        # blocks past the timeout is dropped; remaining healthy subscribers
        # continue to receive output.
        async def _send_to_subscriber(target: TerminalSender) -> BaseException | None:
            try:
                await asyncio.wait_for(
                    target(data),
                    timeout=PUBLISH_OUTPUT_SUBSCRIBER_TIMEOUT_SECONDS,
                )
                return None
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                return exc

        results = await asyncio.gather(
            *(_send_to_subscriber(sender) for sender in subscribers),
            return_exceptions=False,
        )

        failed_senders: list[TerminalSender] = []
        for sender, error in zip(subscribers, results):
            if error is None:
                continue
            failed_senders.append(sender)
            if isinstance(error, asyncio.TimeoutError):
                logger.warning(
                    "broker dropping terminal output subscriber that timed out",
                    extra={
                        "client_id": str(client_id),
                        "window_id": str(window_id),
                        "timeout_seconds": PUBLISH_OUTPUT_SUBSCRIBER_TIMEOUT_SECONDS,
                    },
                )

        for sender in failed_senders:
            with contextlib.suppress(Exception):
                await self.unsubscribe(client_id, window_id, sender)

    async def publish_status(self, client_id: UUID, window_id: UUID, message: str) -> None:
        async with self._lock:
            subscribers = tuple(self._status_subscribers.get((client_id, window_id), ()))

        if not subscribers:
            return

        async def _send_status_to_subscriber(
            target: TerminalStatusSender,
        ) -> BaseException | None:
            try:
                await asyncio.wait_for(
                    target(message),
                    timeout=PUBLISH_OUTPUT_SUBSCRIBER_TIMEOUT_SECONDS,
                )
                return None
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                return exc

        results = await asyncio.gather(
            *(_send_status_to_subscriber(sender) for sender in subscribers),
            return_exceptions=False,
        )

        failed_senders: list[TerminalStatusSender] = []
        for sender, error in zip(subscribers, results):
            if error is None:
                continue
            failed_senders.append(sender)
            if isinstance(error, asyncio.TimeoutError):
                logger.warning(
                    "broker dropping terminal status subscriber that timed out",
                    extra={
                        "client_id": str(client_id),
                        "window_id": str(window_id),
                        "timeout_seconds": PUBLISH_OUTPUT_SUBSCRIBER_TIMEOUT_SECONDS,
                    },
                )

        for sender in failed_senders:
            with contextlib.suppress(Exception):
                await self._unsubscribe_status_sender(client_id, window_id, sender)

    async def clear_client(
        self,
        client_id: UUID,
        *,
        status_message: str | None = None,
    ) -> None:
        async with self._lock:
            for key in tuple(self._attachments):
                if key[0] == client_id:
                    self._attachments.pop(key, None)
            subscriber_keys = [
                key for key in self._status_subscribers
                if key[0] == client_id and self._status_subscribers.get(key)
            ]

        if status_message is not None:
            for _, window_id in subscriber_keys:
                await self.publish_status(client_id, window_id, status_message)

    async def attach(
        self,
        client_id: UUID,
        window_id: UUID,
        runtime_window: RuntimeWindow,
        output_callback: TerminalSender | None = None,
        selection_callback: TerminalSelectionCallback | None = None,
    ) -> None:
        runtime = self._require_runtime(client_id)
        key = (client_id, window_id)
        while True:
            async with self._lock:
                if key in self._attachments:
                    return
                detach_task = self._detaches.get(key)
            if detach_task is None:
                break
            with contextlib.suppress(Exception):
                await detach_task

        sender = output_callback or (lambda data: self.publish_output(client_id, window_id, data))
        await runtime.attach(
            runtime_window,
            sender,
            local_window_id=window_id,
            selection_callback=selection_callback,
        )
        async with self._lock:
            self._attachments[key] = runtime_window

    async def send_input(
        self,
        client_id: UUID,
        window_id: UUID,
        runtime_window: RuntimeWindow,
        data: bytes,
    ) -> None:
        runtime = self._require_runtime(client_id)
        await runtime.send_input(runtime_window, data, local_window_id=window_id)

    async def resize(
        self,
        client_id: UUID,
        window_id: UUID,
        runtime_window: RuntimeWindow,
        *,
        cols: int,
        rows: int,
    ) -> None:
        runtime = self._require_runtime(client_id)
        await runtime.resize(runtime_window, cols=cols, rows=rows, local_window_id=window_id)

    def _require_runtime(self, client_id: UUID) -> TerminalRuntime:
        runtime = self._runtimes.get(client_id)
        if runtime is None:
            raise TerminalRuntimeUnavailable(f"no terminal runtime registered for client: {client_id}")
        return runtime

    async def _unsubscribe_status_sender(
        self,
        client_id: UUID,
        window_id: UUID,
        sender: TerminalStatusSender,
    ) -> None:
        async with self._lock:
            key = (client_id, window_id)
            subscribers = self._status_subscribers.get(key)
            if subscribers is None:
                return
            subscribers.discard(sender)
            if not subscribers:
                self._status_subscribers.pop(key, None)
