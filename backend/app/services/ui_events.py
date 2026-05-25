from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from uuid import UUID

UiEventSender = Callable[[str], Awaitable[None]]
UiResource = str


@dataclass
class _PendingInvalidation:
    resources: set[UiResource] = field(default_factory=set)
    client_id: UUID | None = None
    window_id: UUID | None = None
    reason: str | None = None
    task: asyncio.Task[None] | None = None


class UiEventHub:
    def __init__(self) -> None:
        self._subscribers: set[UiEventSender] = set()
        self._pending_invalidations: dict[tuple[object, ...], _PendingInvalidation] = {}
        self._seq = 0
        self._lock = asyncio.Lock()

    async def subscribe(self, sender: UiEventSender) -> None:
        async with self._lock:
            self._subscribers.add(sender)

    async def unsubscribe(self, sender: UiEventSender) -> None:
        async with self._lock:
            self._subscribers.discard(sender)

    async def publish_invalidation(
        self,
        resources: Iterable[UiResource],
        *,
        client_id: UUID | None = None,
        window_id: UUID | None = None,
        reason: str | None = None,
    ) -> None:
        normalized_resources = _normalize_resources(resources)
        if not normalized_resources:
            return
        await self._publish(
            {
                "type": "invalidate",
                "resources": normalized_resources,
                "client_id": str(client_id) if client_id is not None else None,
                "window_id": str(window_id) if window_id is not None else None,
                "reason": reason,
            }
        )

    async def publish_terminal_selection(self, client_id: UUID, window_id: UUID) -> None:
        await self._publish(
            {
                "type": "terminal_selection",
                "client_id": str(client_id),
                "window_id": str(window_id),
            }
        )

    async def publish_debounced_invalidation(
        self,
        key: tuple[object, ...],
        resources: Iterable[UiResource],
        *,
        client_id: UUID | None = None,
        window_id: UUID | None = None,
        reason: str | None = None,
        delay_seconds: float = 1.0,
    ) -> None:
        normalized_resources = set(_normalize_resources(resources))
        if not normalized_resources:
            return

        async with self._lock:
            pending = self._pending_invalidations.get(key)
            if pending is None:
                pending = _PendingInvalidation(client_id=client_id, window_id=window_id, reason=reason)
                self._pending_invalidations[key] = pending
                pending.task = asyncio.create_task(self._flush_pending_invalidation(key, delay_seconds))
            pending.resources.update(normalized_resources)
            pending.client_id = client_id
            pending.window_id = window_id
            pending.reason = reason

    async def _flush_pending_invalidation(self, key: tuple[object, ...], delay_seconds: float) -> None:
        await asyncio.sleep(delay_seconds)
        async with self._lock:
            pending = self._pending_invalidations.pop(key, None)
        if pending is None:
            return
        await self.publish_invalidation(
            pending.resources,
            client_id=pending.client_id,
            window_id=pending.window_id,
            reason=pending.reason,
        )

    async def _publish(self, payload: dict[str, object]) -> None:
        async with self._lock:
            self._seq += 1
            message = json.dumps(
                {
                    **payload,
                    "seq": self._seq,
                },
                separators=(",", ":"),
            )
            subscribers = tuple(self._subscribers)

        failed_senders: list[UiEventSender] = []
        for sender in subscribers:
            try:
                await sender(message)
            except Exception:
                failed_senders.append(sender)

        for sender in failed_senders:
            await self.unsubscribe(sender)


def _normalize_resources(resources: Iterable[UiResource]) -> list[UiResource]:
    seen: set[UiResource] = set()
    normalized: list[UiResource] = []
    for resource in resources:
        if resource in seen:
            continue
        seen.add(resource)
        normalized.append(resource)
    return normalized
