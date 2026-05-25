from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.models import ClientRuntime, ClientStatus, VirtualWindow, WindowStatus
from app.repositories.clients import create_client, ensure_local_client
from app.repositories.windows import create_window
from app.services.runtime.offline_monitor import (
    mark_all_remote_clients_disconnected,
    mark_remote_client_disconnected,
    mark_stale_clients_offline,
    reconcile_inventory,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as db_session:
        yield db_session

    await engine.dispose()


@pytest.mark.asyncio
async def test_mark_stale_clients_offline_marks_only_stale_remote_online_clients(session):
    now = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    stale_client, _ = await create_client(session, name="stale", runtime=ClientRuntime.remote)
    fresh_client, _ = await create_client(session, name="fresh", runtime=ClientRuntime.remote)
    local_client = await ensure_local_client(session)

    stale_client.status = ClientStatus.ONLINE
    stale_client.last_seen_at = now - timedelta(minutes=3)
    fresh_client.status = ClientStatus.ONLINE
    fresh_client.last_seen_at = now - timedelta(seconds=30)
    local_client.status = ClientStatus.ONLINE
    local_client.last_seen_at = now - timedelta(minutes=10)

    stale_window = await create_window(session, stale_client.id, cwd="/tmp", shell_command="/bin/bash")
    fresh_window = await create_window(session, fresh_client.id, cwd="/tmp", shell_command="/bin/bash")
    local_window = await create_window(session, local_client.id, cwd="/tmp", shell_command="/bin/bash")
    await session.commit()

    marked_count = await mark_stale_clients_offline(session, now=now, timeout=timedelta(minutes=2))
    await session.commit()

    assert marked_count == 1
    assert stale_client.status is ClientStatus.OFFLINE
    assert fresh_client.status is ClientStatus.ONLINE
    assert local_client.status is ClientStatus.ONLINE
    assert stale_window.status is WindowStatus.disconnected
    assert fresh_window.status is WindowStatus.active
    assert local_window.status is WindowStatus.active


@pytest.mark.asyncio
async def test_mark_remote_client_disconnected_marks_active_windows_disconnected(session):
    client, _ = await create_client(session, name="remote", runtime=ClientRuntime.remote)
    client.status = ClientStatus.ONLINE
    active_window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
    error_window = await create_window(session, client.id, cwd="/tmp", shell_command="/bin/bash")
    error_window.status = WindowStatus.error
    await session.commit()

    changed = await mark_remote_client_disconnected(session, client.id)
    await session.commit()

    assert changed is True
    assert client.status is ClientStatus.OFFLINE
    assert active_window.status is WindowStatus.disconnected
    assert error_window.status is WindowStatus.error


@pytest.mark.asyncio
async def test_mark_all_remote_clients_disconnected_only_touches_online_remote_clients(session):
    online_remote, _ = await create_client(session, name="online", runtime=ClientRuntime.remote)
    offline_remote, _ = await create_client(session, name="offline", runtime=ClientRuntime.remote)
    local_client = await ensure_local_client(session)
    online_remote.status = ClientStatus.ONLINE
    offline_remote.status = ClientStatus.OFFLINE
    local_client.status = ClientStatus.ONLINE
    online_window = await create_window(session, online_remote.id, cwd="/tmp", shell_command="/bin/bash")
    offline_window = await create_window(session, offline_remote.id, cwd="/tmp", shell_command="/bin/bash")
    local_window = await create_window(session, local_client.id, cwd="/tmp", shell_command="/bin/bash")
    await session.commit()

    changed_count = await mark_all_remote_clients_disconnected(session)
    await session.commit()

    assert changed_count == 1
    assert online_remote.status is ClientStatus.OFFLINE
    assert offline_remote.status is ClientStatus.OFFLINE
    assert local_client.status is ClientStatus.ONLINE
    assert online_window.status is WindowStatus.disconnected
    assert offline_window.status is WindowStatus.active
    assert local_window.status is WindowStatus.active


@pytest.mark.asyncio
async def test_reconcile_inventory_restores_present_disconnected_windows_and_errors_missing(session):
    client, _ = await create_client(session, name="remote", runtime=ClientRuntime.remote)
    present = await create_window(
        session,
        client.id,
        cwd="/tmp",
        shell_command="/bin/bash",
        remote_session_id="session-present",
        remote_window_id="@1",
    )
    missing = await create_window(
        session,
        client.id,
        cwd="/tmp",
        shell_command="/bin/bash",
        remote_session_id="session-missing",
        remote_window_id="@2",
    )
    present.status = WindowStatus.disconnected
    missing.status = WindowStatus.disconnected
    await session.commit()

    reconciled_count = await reconcile_inventory(
        session,
        client.id,
        [
            {
                "local_window_id": str(present.id),
                "remote_session_id": "session-present",
                "remote_window_id": "@1",
            }
        ],
    )
    await session.commit()

    assert reconciled_count == 2
    assert present.status is WindowStatus.active
    assert missing.status is WindowStatus.error


@pytest.mark.asyncio
async def test_reconcile_inventory_restores_present_error_windows(session):
    client, _ = await create_client(session, name="remote", runtime=ClientRuntime.remote)
    present = await create_window(
        session,
        client.id,
        cwd="/tmp",
        shell_command="/bin/bash",
        remote_session_id="session-present",
        remote_window_id="@1",
    )
    present.status = WindowStatus.error
    await session.commit()

    reconciled_count = await reconcile_inventory(
        session,
        client.id,
        [
            {
                "local_window_id": str(present.id),
                "remote_session_id": "session-present",
                "remote_window_id": "@1",
            }
        ],
    )
    await session.commit()

    assert reconciled_count == 1
    assert present.status is WindowStatus.active


@pytest.mark.asyncio
async def test_reconcile_inventory_does_not_count_unchanged_error_windows(session):
    client, _ = await create_client(session, name="remote", runtime=ClientRuntime.remote)
    missing = await create_window(
        session,
        client.id,
        cwd="/tmp",
        shell_command="/bin/bash",
        remote_session_id="session-missing",
        remote_window_id="@2",
    )
    missing.status = WindowStatus.error
    await session.commit()

    reconciled_count = await reconcile_inventory(session, client.id, [])
    await session.commit()

    assert reconciled_count == 0
    assert missing.status is WindowStatus.error


@pytest.mark.asyncio
async def test_reconcile_inventory_only_touches_windows_for_given_client(session):
    client, _ = await create_client(session, name="remote", runtime=ClientRuntime.remote)
    other_client, _ = await create_client(session, name="other", runtime=ClientRuntime.remote)
    target_window = await create_window(
        session,
        client.id,
        cwd="/tmp",
        shell_command="/bin/bash",
        remote_session_id="session-target",
        remote_window_id="@1",
    )
    other_window = await create_window(
        session,
        other_client.id,
        cwd="/tmp",
        shell_command="/bin/bash",
        remote_session_id="session-other",
        remote_window_id="@2",
    )
    target_window.status = WindowStatus.disconnected
    other_window.status = WindowStatus.disconnected
    await session.commit()

    await reconcile_inventory(session, client.id, [])
    await session.commit()

    rows = (await session.execute(select(VirtualWindow))).scalars().all()
    assert {row.id for row in rows} == {target_window.id, other_window.id}
    assert target_window.status is WindowStatus.error
    assert other_window.status is WindowStatus.disconnected
