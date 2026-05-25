from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from uuid import UUID
import asyncio

SelectionSender = Callable[[str], Awaitable[None]]


class TerminalSelectionHub:
    def __init__(self) -> None:
        self._subscribers: dict[UUID, set[SelectionSender]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, client_id: UUID, sender: SelectionSender) -> None:
        async with self._lock:
            self._subscribers.setdefault(client_id, set()).add(sender)

    async def unsubscribe(self, client_id: UUID, sender: SelectionSender) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(client_id)
            if subscribers is None:
                return
            subscribers.discard(sender)
            if not subscribers:
                self._subscribers.pop(client_id, None)

    async def publish(self, client_id: UUID, window_id: UUID) -> None:
        payload = json.dumps(
            {
                "type": "terminal_selection",
                "client_id": str(client_id),
                "window_id": str(window_id),
            }
        )
        async with self._lock:
            subscribers = tuple(self._subscribers.get(client_id, ()))

        failed_senders: list[SelectionSender] = []
        for sender in subscribers:
            try:
                await sender(payload)
            except Exception:
                failed_senders.append(sender)

        for sender in failed_senders:
            await self.unsubscribe(client_id, sender)
