from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.model_base import Base
from app.models import Event, EventSourceType, VirtualWindow, WindowStatus
from app.services.terminal_work_status import (
    load_last_agent_task_completed_at_by_window,
    load_work_status,
    work_status_from_activity,
)


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


def test_work_status_from_activity_returns_long_idle_after_recent_window() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(minutes=6),
        last_working_activity_at=now - timedelta(minutes=6),
    )

    assert status.state == "LONG_IDLE"
    assert status.label == "长时间没有工作了"
    assert status.color == "gray"


def test_work_status_from_activity_prefers_working_for_recent_agent_activity() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(seconds=20),
        last_working_activity_at=now - timedelta(seconds=20),
    )

    assert status.state == "WORKING"
    assert status.label == "正在工作中"
    assert status.color == "orange"


def test_work_status_from_activity_returns_working_for_in_progress_agent_command() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(minutes=10),
        last_working_activity_at=now - timedelta(minutes=10),
        agent_command_in_progress=True,
    )

    assert status.state == "WORKING"


def test_work_status_from_activity_returns_recent_active_for_recent_input_only() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(minutes=2),
        last_working_activity_at=None,
    )

    assert status.state == "RECENT_ACTIVE"
    assert status.label == "最近刚活跃过"
    assert status.color == "green"


@pytest.mark.asyncio
async def test_load_work_status_treats_agent_tool_records_as_working_activity(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    db_session.add(
        Event(
            client_id=client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="cursor-session-1",
            kind="assistant_message",
            virtual_window_id=window.id,
            payload_json={"provider": "cursor_cli", "role": "assistant", "content": "working"},
            fingerprint="cursor-agent-work-status",
            created_at=now - timedelta(seconds=20),
        )
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "WORKING"
    assert status.last_activity_at == now - timedelta(seconds=20)
    assert status.last_working_activity_at == now - timedelta(seconds=20)


@pytest.mark.asyncio
async def test_load_work_status_ignores_non_agent_terminal_output(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    db_session.add(
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_output",
            virtual_window_id=window.id,
            payload_json={"text": "prompt\n"},
            fingerprint="terminal-output-only",
            created_at=now - timedelta(seconds=5),
        )
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "RECENT_ACTIVE"


@pytest.mark.asyncio
async def test_load_work_status_returns_working_for_in_progress_agent_command(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    db_session.add(
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_input_command",
            virtual_window_id=window.id,
            payload_json={"command": "codex exec 'fix tests'", "sequence": 7},
            fingerprint="terminal-input-codex",
            created_at=now - timedelta(minutes=5),
        )
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "WORKING"


@pytest.mark.asyncio
async def test_load_last_agent_task_completed_at_by_window(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    completed_at = now - timedelta(seconds=30)
    db_session.add_all([
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_command_finished",
            virtual_window_id=window.id,
            payload_json={"command": "pwd", "sequence": 1},
            fingerprint="terminal-finished-shell",
            created_at=now - timedelta(seconds=10),
        ),
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_command_finished",
            virtual_window_id=window.id,
            payload_json={"command": "codex exec 'done'", "sequence": 2},
            fingerprint="terminal-finished-codex",
            created_at=completed_at,
        ),
    ])
    await db_session.flush()

    latest = await load_last_agent_task_completed_at_by_window(db_session, client_id, [window.id])

    stored = latest[window.id]
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == completed_at


@pytest.mark.asyncio
async def test_load_last_agent_task_completed_resolves_agent_from_input_command(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    completed_at = now - timedelta(seconds=30)
    db_session.add_all([
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_input_command",
            virtual_window_id=window.id,
            payload_json={"command": "codex exec 'done'", "sequence": 2},
            fingerprint="terminal-input-codex",
            created_at=completed_at - timedelta(seconds=5),
        ),
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_command_finished",
            virtual_window_id=window.id,
            payload_json={"command": "", "sequence": 2},
            fingerprint="terminal-finished-codex",
            created_at=completed_at,
        ),
    ])
    await db_session.flush()

    latest = await load_last_agent_task_completed_at_by_window(db_session, client_id, [window.id])

    stored = latest[window.id]
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == completed_at


@pytest.mark.asyncio
async def test_load_last_agent_task_completed_from_idle_agent_work(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    working_at = now - timedelta(seconds=120)
    db_session.add(
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="agent_work_presence",
            virtual_window_id=window.id,
            payload_json={"providers": ["claude_code"], "reasons": ["process"]},
            fingerprint="agent-work-presence-claude",
            created_at=working_at,
        )
    )
    await db_session.flush()

    latest = await load_last_agent_task_completed_at_by_window(
        db_session,
        client_id,
        [window.id],
        now=now,
    )

    stored = latest[window.id]
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == working_at + timedelta(seconds=60)
