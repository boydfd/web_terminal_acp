from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.client_agent.agent_work_presence import (
    _descendant_pids,
    _provider_from_cmdline,
    agent_command_tokens,
)
from app.model_base import Base
from app.models import Event, EventSourceType, VirtualWindow, WindowStatus
from app.services.agent_work_presence import (
    AGENT_WORK_PRESENCE_KIND,
    touch_agent_work_presence,
)
from app.services.terminal_work_status import load_work_status, work_status_from_activity


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


def test_provider_from_cmdline_detects_codex_and_acpx_wrapper() -> None:
    tokens = agent_command_tokens()
    assert _provider_from_cmdline("codex exec fix tests", tokens) == "codex"
    assert _provider_from_cmdline("acpx codex exec fix tests", tokens) == "codex"
    assert _provider_from_cmdline("claude -p hi", tokens) == "claude_code"
    assert _provider_from_cmdline("agent -p hi", tokens) == "cursor_cli"


def test_descendant_pids_includes_children() -> None:
    parent_map = {10: 1, 11: 10, 20: 2}
    assert _descendant_pids([10], parent_map) == {10, 11}


def test_work_status_from_activity_treats_recent_presence_as_working() -> None:
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(minutes=10),
        last_working_activity_at=now - timedelta(seconds=20),
    )
    assert status.state == "WORKING"


@pytest.mark.asyncio
async def test_touch_agent_work_presence_refreshes_bucketed_event(db_session) -> None:
    client_id = uuid4()
    window_id = uuid4()
    window = VirtualWindow(id=window_id, client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()

    first_at = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    second_at = first_at + timedelta(seconds=10)

    first = await touch_agent_work_presence(
        db_session,
        client_id=client_id,
        window_id=window_id,
        providers=["codex"],
        reasons=["process"],
        observed_at=first_at,
    )
    second = await touch_agent_work_presence(
        db_session,
        client_id=client_id,
        window_id=window_id,
        providers=["codex"],
        reasons=["process"],
        observed_at=second_at,
    )

    assert first.id == second.id
    assert second.created_at == second_at


@pytest.mark.asyncio
async def test_load_work_status_uses_agent_work_presence_while_command_in_progress(db_session) -> None:
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'fix tests'", "sequence": 3},
                fingerprint="terminal-input-codex",
                created_at=now - timedelta(seconds=30),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind=AGENT_WORK_PRESENCE_KIND,
                virtual_window_id=window.id,
                payload_json={"providers": ["codex"], "reasons": ["process"]},
                fingerprint="presence-test",
                created_at=now - timedelta(seconds=15),
            ),
        ]
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "WORKING"
    assert status.last_working_activity_at == now - timedelta(seconds=15)


@pytest.mark.asyncio
async def test_load_work_status_ignores_stale_agent_work_presence_without_active_command(
    db_session,
) -> None:
    now = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'fix tests'", "sequence": 3},
                fingerprint="terminal-input-codex",
                created_at=now - timedelta(minutes=5),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'fix tests'", "sequence": 3},
                fingerprint="terminal-finished-codex",
                created_at=now - timedelta(minutes=4),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind=AGENT_WORK_PRESENCE_KIND,
                virtual_window_id=window.id,
                payload_json={"providers": ["codex"], "reasons": ["process"]},
                fingerprint="presence-test",
                created_at=now - timedelta(seconds=15),
            ),
        ]
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    # Presence still counts as recent terminal activity, but must not keep WORKING.
    assert status.state == "RECENT_ACTIVE"
