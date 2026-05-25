from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, ClientRuntime, ClientStatus, VirtualWindow, WindowStatus
from app.services.ui_events import UiEventHub

logger = logging.getLogger(__name__)

OFFLINE_TIMEOUT = timedelta(minutes=2)
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
InventoryWindow = Mapping[str, Any]


def _inventory_window_keys(windows: Iterable[Any]) -> set[tuple[UUID, str, str]]:
    keys: set[tuple[UUID, str, str]] = set()
    for window in windows:
        if not isinstance(window, Mapping):
            continue
        local_window_id = _parse_uuid(window.get("local_window_id"))
        remote_session_id = window.get("remote_session_id")
        remote_window_id = window.get("remote_window_id")
        if (
            local_window_id is not None
            and isinstance(remote_session_id, str)
            and isinstance(remote_window_id, str)
        ):
            keys.add((local_window_id, remote_session_id, remote_window_id))
    return keys


def _parse_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


async def mark_stale_clients_offline(
    session: AsyncSession,
    now: datetime | None = None,
    timeout: timedelta = OFFLINE_TIMEOUT,
) -> int:
    current_time = now or datetime.now(UTC)
    cutoff = current_time - timeout
    stale_clients = list(
        await session.scalars(
            select(Client).where(
                Client.runtime == ClientRuntime.remote,
                Client.status == ClientStatus.ONLINE,
                Client.last_seen_at.is_not(None),
                Client.last_seen_at < cutoff,
            )
        )
    )
    if not stale_clients:
        return 0

    stale_client_ids = [client.id for client in stale_clients]
    for client in stale_clients:
        client.status = ClientStatus.OFFLINE

    active_windows = list(
        await session.scalars(
            select(VirtualWindow).where(
                VirtualWindow.client_id.in_(stale_client_ids),
                VirtualWindow.status == WindowStatus.active,
            )
        )
    )
    for window in active_windows:
        window.status = WindowStatus.disconnected

    await session.flush()
    return len(stale_clients)


async def mark_remote_client_disconnected(session: AsyncSession, client_id: UUID) -> bool:
    client = await session.scalar(
        select(Client).where(
            Client.id == client_id,
            Client.runtime == ClientRuntime.remote,
        )
    )
    if client is None:
        return False

    changed = client.status != ClientStatus.OFFLINE
    client.status = ClientStatus.OFFLINE

    active_windows = list(
        await session.scalars(
            select(VirtualWindow).where(
                VirtualWindow.client_id == client_id,
                VirtualWindow.status == WindowStatus.active,
            )
        )
    )
    for window in active_windows:
        window.status = WindowStatus.disconnected
    changed = changed or bool(active_windows)

    if changed:
        await session.flush()
    return changed


async def mark_all_remote_clients_disconnected(session: AsyncSession) -> int:
    online_clients = list(
        await session.scalars(
            select(Client).where(
                Client.runtime == ClientRuntime.remote,
                Client.status == ClientStatus.ONLINE,
            )
        )
    )
    changed_count = 0
    for client in online_clients:
        if await mark_remote_client_disconnected(session, client.id):
            changed_count += 1
    return changed_count


async def reconcile_inventory(
    session: AsyncSession,
    client_id: UUID,
    windows: Iterable[InventoryWindow],
) -> int:
    inventory_keys = _inventory_window_keys(windows)
    recoverable_windows = list(
        await session.scalars(
            select(VirtualWindow).where(
                VirtualWindow.client_id == client_id,
                VirtualWindow.status.in_([WindowStatus.disconnected, WindowStatus.error]),
            )
        )
    )

    changed_count = 0
    for window in recoverable_windows:
        remote_key = (window.id, window.remote_session_id, window.remote_window_id)
        next_status = WindowStatus.active if remote_key in inventory_keys else WindowStatus.error
        if window.status is next_status:
            continue
        window.status = next_status
        changed_count += 1

    if changed_count:
        await session.flush()
    return changed_count


async def run_offline_monitor_loop(
    session_factory: SessionFactory,
    interval_seconds: float = 10.0,
    ui_event_hub: UiEventHub | None = None,
) -> None:
    while True:
        try:
            async with session_factory() as session:
                marked_count = await mark_stale_clients_offline(session)
                await session.commit()
                if marked_count:
                    if ui_event_hub is not None:
                        await ui_event_hub.publish_invalidation(
                            ["clients", "tree", "window"],
                            reason="clients_marked_offline",
                        )
                    logger.info(
                        "marked stale clients offline",
                        extra={"marked_count": marked_count},
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("offline monitor iteration failed")

        await asyncio.sleep(interval_seconds)
