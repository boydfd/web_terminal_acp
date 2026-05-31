from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.model_base import Base
from app.models import Event, EventSourceType, VirtualWindow, WindowStatus
from app.services import terminal_work_status
from app.services.terminal_work_status import (
    load_tree_window_activity,
    load_last_agent_task_completed_at_by_window,
    load_work_statuses,
    load_work_status,
    work_status_from_activity,
)


def codex_completion_payload(
    *, event_type: str = "task_completed", timestamp: datetime | None = None
) -> dict:
    payload = {
        "provider": "codex",
        "raw_type": "event_msg",
        "payload": {"type": event_type},
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp.isoformat()
    return payload


def codex_message_payload(text: str, *, timestamp: datetime | None = None) -> dict:
    payload = {
        "provider": "codex",
        "raw_type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp.isoformat()
    return payload


def claude_completion_payload() -> dict:
    return {
        "provider": "claude_code",
        "type": "assistant",
        "message": {
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
        },
    }


def claude_turn_duration_payload(*, timestamp: datetime | None = None) -> dict:
    payload = {
        "provider": "claude_code",
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": 166049,
        "messageCount": 37,
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp.isoformat()
    return payload


def claude_local_command_payload(text: str = "<bash-stdout>done</bash-stdout>") -> dict:
    return {
        "provider": "claude_code",
        "type": "user",
        "message": {
            "role": "user",
            "content": text,
        },
        "isMeta": True,
    }


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session

    await engine.dispose()


@pytest.fixture
async def counted_db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def count_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session, statements

    await engine.dispose()


def test_work_status_from_activity_returns_long_idle_after_recent_window() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(minutes=11),
        last_working_activity_at=now - timedelta(minutes=11),
    )

    assert status.state == "LONG_IDLE"
    assert status.label == "长时间没有工作了"
    assert status.color == "gray"


def test_work_status_from_activity_prefers_working_for_recent_agent_activity() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(seconds=20),
        last_agent_active_at=now - timedelta(seconds=25),
        last_agent_output_at=now - timedelta(seconds=20),
    )

    assert status.state == "WORKING"
    assert status.label == "Agent 工作中"
    assert status.color == "orange"


def test_work_status_from_activity_returns_recent_active_for_stale_unmanaged_agent_activity() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(minutes=2),
        last_working_activity_at=now - timedelta(minutes=2),
    )

    assert status.state == "RECENT_ACTIVE"


def test_work_status_from_activity_returns_recent_active_for_recent_input_only() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(minutes=2),
        last_working_activity_at=None,
    )

    assert status.state == "RECENT_ACTIVE"
    assert status.label == "Terminal 活跃"
    assert status.color == "green"


def test_work_status_from_activity_does_not_treat_output_before_terminal_activity_as_active_agent() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(seconds=20),
        last_terminal_activity_at=now - timedelta(seconds=20),
        last_agent_output_at=now - timedelta(seconds=30),
    )

    assert status.state == "RECENT_ACTIVE"
    assert status.label == "Terminal 活跃"


def test_work_status_from_activity_returns_finished_for_recent_explicit_completion() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(seconds=20),
        last_agent_active_at=now - timedelta(minutes=2),
        last_agent_output_at=now - timedelta(seconds=30),
        last_agent_completed_at=now - timedelta(seconds=20),
    )

    assert status.state == "FINISHED"
    assert status.label == "Agent 已完成"
    assert status.color == "green"


def test_work_status_from_activity_returns_working_when_output_follows_completion() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(seconds=10),
        last_agent_active_at=now - timedelta(seconds=10),
        last_agent_output_at=now - timedelta(seconds=10),
        last_agent_completed_at=now - timedelta(seconds=30),
    )

    assert status.state == "WORKING"


def test_work_status_from_activity_returns_aborted_for_agent_without_output_over_one_hour() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(minutes=61),
        last_agent_active_at=now - timedelta(minutes=70),
        last_agent_output_at=now - timedelta(minutes=61),
    )

    assert status.state == "ABORTED"
    assert status.label == "Agent 可能已中断"
    assert status.color == "red"


def test_work_status_from_activity_returns_aborted_for_agent_command_with_no_output_over_one_hour() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(minutes=70),
        last_agent_active_at=now - timedelta(minutes=70),
        last_agent_output_at=None,
    )

    assert status.state == "ABORTED"
    assert status.label == "Agent 可能已中断"
    assert status.color == "red"


def test_work_status_from_activity_returns_sleeping_after_abort_notice_window() -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)

    status = work_status_from_activity(
        now=now,
        last_activity_at=now - timedelta(minutes=75),
        last_agent_active_at=now - timedelta(minutes=90),
        last_agent_output_at=now - timedelta(minutes=75),
    )

    assert status.state == "LONG_IDLE"


@pytest.mark.asyncio
async def test_load_work_statuses_batches_latest_activity_queries(counted_db_session) -> None:
    db_session, statements = counted_db_session
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    windows = [
        VirtualWindow(id=uuid4(), client_id=client_id, title=f"Terminal {index}", status=WindowStatus.active)
        for index in range(3)
    ]
    db_session.add_all(windows)
    await db_session.flush()
    windows[0].terminal_last_output_at = now - timedelta(seconds=15)
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(windows[1].id),
                kind="terminal_input_command",
                virtual_window_id=windows[1].id,
                payload_json={"command": "codex exec 'fix'", "sequence": 1},
                fingerprint="terminal-input-agent-tool-record-latest",
                created_at=now - timedelta(seconds=12),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="assistant_message",
                virtual_window_id=windows[1].id,
                payload_json={"provider": "codex", "role": "assistant", "content": "working"},
                fingerprint="agent-tool-record-latest",
                created_at=now - timedelta(seconds=10),
            ),
        ]
    )
    await db_session.flush()
    statements.clear()

    statuses = await load_work_statuses(
        db_session,
        client_id,
        [window.id for window in windows],
        now=now,
    )

    assert statuses[windows[0].id].state == "RECENT_ACTIVE"
    assert statuses[windows[1].id].state == "WORKING"
    latest_activity_queries = [
        statement
        for statement in statements
        if "events.created_at" in statement
        and "virtual_windows" in statement
        and "SELECT virtual_windows.id" in statement
    ]
    assert len(latest_activity_queries) <= 2


@pytest.mark.asyncio
async def test_load_work_statuses_uses_bounded_agent_event_queries(counted_db_session) -> None:
    db_session, statements = counted_db_session
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    windows = [
        VirtualWindow(id=uuid4(), client_id=client_id, title=f"Terminal {index}", status=WindowStatus.active)
        for index in range(2)
    ]
    db_session.add_all(windows)
    await db_session.flush()
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(windows[0].id),
                kind="terminal_input_command",
                virtual_window_id=windows[0].id,
                payload_json={"command": "codex exec 'fix'", "sequence": 1},
                fingerprint="bounded-agent-query-command",
                created_at=now - timedelta(seconds=20),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="response_item",
                virtual_window_id=windows[0].id,
                payload_json=codex_message_payload("working"),
                fingerprint="bounded-agent-query-output",
                created_at=now - timedelta(seconds=10),
            ),
        ]
    )
    await db_session.flush()
    statements.clear()

    statuses = await load_work_statuses(
        db_session,
        client_id,
        [window.id for window in windows],
        now=now,
    )

    assert statuses[windows[0].id].state == "WORKING"
    agent_event_queries = [
        statement
        for statement in statements
        if "FROM events" in statement and "events.source_type IN" in statement
    ]
    assert len(agent_event_queries) == 1
    assert all("'agent_tool_record'" in statement for statement in agent_event_queries)
    assert all("'codex_trace'" in statement for statement in agent_event_queries)
    assert all("'claude_jsonl'" in statement for statement in agent_event_queries)
    assert all("row_number" in statement.lower() for statement in agent_event_queries)
    assert all("partition by" in statement.lower() for statement in agent_event_queries)


@pytest.mark.asyncio
async def test_load_work_statuses_uses_window_agent_activity_state(counted_db_session) -> None:
    db_session, statements = counted_db_session
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(
        id=uuid4(),
        client_id=client_id,
        title="Terminal",
        status=WindowStatus.active,
        agent_activity_latest_at=now - timedelta(seconds=10),
    )
    db_session.add(window)
    await db_session.flush()
    statements.clear()

    statuses = await load_work_statuses(db_session, client_id, [window.id], now=now)

    assert statuses[window.id].state == "WORKING"
    assert not [
        statement
        for statement in statements
        if "FROM events" in statement and "events.source_type IN" in statement
    ]


@pytest.mark.asyncio
async def test_postgres_activity_projection_does_not_scan_empty_agent_windows(
    counted_db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session, statements = counted_db_session
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(
        id=uuid4(),
        client_id=client_id,
        title="Terminal",
        status=WindowStatus.active,
    )
    db_session.add(window)
    await db_session.flush()
    monkeypatch.setattr("app.services.terminal_work_status._dialect_name", lambda _session: "postgresql")
    statements.clear()

    activity = await terminal_work_status._agent_activity_state_by_window(
        db_session,
        client_id,
        [window.id],
    )

    assert window.id not in activity.latest_activity
    assert not [
        statement
        for statement in statements
        if "FROM events" in statement and "events.source_type IN" in statement
    ]


@pytest.mark.asyncio
async def test_postgres_activity_projection_repairs_stale_latest_event_without_scan(
    counted_db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_session, statements = counted_db_session
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    completed_at = now - timedelta(minutes=2)
    local_command_at = now - timedelta(seconds=10)
    window = VirtualWindow(
        id=uuid4(),
        client_id=client_id,
        title="Terminal",
        status=WindowStatus.active,
        agent_activity_latest_at=local_command_at,
        agent_activity_latest_completed_at=completed_at,
    )
    local_command = Event(
        id=uuid4(),
        client_id=client_id,
        source_type=EventSourceType.agent_tool_record,
        source_id="claude-session-1",
        kind="user_message",
        virtual_window_id=window.id,
        payload_json=claude_local_command_payload(),
        fingerprint="claude-local-command-after-finish-projection",
        created_at=local_command_at,
    )
    window.agent_activity_latest_event_id = local_command.id
    db_session.add_all([window, local_command])
    await db_session.flush()
    monkeypatch.setattr("app.services.terminal_work_status._dialect_name", lambda _session: "postgresql")
    statements.clear()

    activity = await terminal_work_status._agent_activity_state_by_window(
        db_session,
        client_id,
        [window.id],
    )

    assert activity.latest_activity[window.id] == completed_at
    assert activity.latest_completed_at[window.id] == completed_at
    assert not [
        statement
        for statement in statements
        if "FROM events" in statement and "events.source_type IN" in statement
    ]


@pytest.mark.asyncio
async def test_finished_command_sequence_query_starts_at_latest_agent_command(counted_db_session) -> None:
    db_session, statements = counted_db_session
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    window.agent_activity_latest_at = now - timedelta(seconds=10)
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"sequence": 1},
                fingerprint="finished-before-latest-agent-command",
                created_at=now - timedelta(minutes=10),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'fix'", "sequence": 2},
                fingerprint="latest-agent-command-for-finished-scope",
                created_at=now - timedelta(seconds=20),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_message_payload("working"),
                fingerprint="agent-output-after-latest-command",
                created_at=now - timedelta(seconds=10),
            ),
        ]
    )
    await db_session.flush()
    statements.clear()

    statuses = await load_work_statuses(db_session, client_id, [window.id], now=now)

    assert statuses[window.id].state == "WORKING"
    finished_queries = [
        statement
        for statement in statements
        if "FROM events" in statement and "events.kind = ?" in statement and "events.created_at >=" in statement
    ]
    assert len(finished_queries) == 1


@pytest.mark.asyncio
async def test_load_work_status_treats_agent_tool_records_as_working_activity(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    db_session.add_all([
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_input_command",
            virtual_window_id=window.id,
            payload_json={"command": "cursor", "sequence": 1},
            fingerprint="terminal-input-cursor-work-status",
            created_at=now - timedelta(seconds=25),
        ),
        Event(
            client_id=client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="cursor-session-1",
            kind="assistant_message",
            virtual_window_id=window.id,
            payload_json={"provider": "cursor_cli", "role": "assistant", "content": "working"},
            fingerprint="cursor-agent-work-status",
            created_at=now - timedelta(seconds=20),
        ),
    ])
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "WORKING"
    assert status.last_activity_at == now - timedelta(seconds=20)
    assert status.last_working_activity_at == now - timedelta(seconds=20)


@pytest.mark.asyncio
async def test_load_work_status_uses_lightweight_terminal_output_activity(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    window.terminal_last_output_at = now - timedelta(seconds=5)
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "RECENT_ACTIVE"


@pytest.mark.asyncio
async def test_load_work_status_keeps_agent_command_start_as_terminal_active_until_agent_output(db_session) -> None:
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
            created_at=now - timedelta(seconds=30),
        )
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "RECENT_ACTIVE"
    assert status.last_working_activity_at is None


@pytest.mark.asyncio
async def test_load_work_status_keeps_agent_output_working_until_abort_threshold(
    db_session,
) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    window.agent_activity_latest_at = now - timedelta(seconds=30)
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'fix tests'", "sequence": 7},
                fingerprint="terminal-input-codex",
                created_at=now - timedelta(minutes=11),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="assistant_message",
                virtual_window_id=window.id,
                payload_json={"provider": "codex", "role": "assistant", "content": "working"},
                fingerprint="codex-agent-stale-work-status",
                created_at=now - timedelta(minutes=11),
            ),
        ]
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "WORKING"
    assert status.last_activity_at == now - timedelta(seconds=30)
    assert status.last_working_activity_at == now - timedelta(seconds=30)


@pytest.mark.asyncio
async def test_load_work_status_stops_agent_activity_after_shell_exit_without_completion(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    window.agent_activity_latest_at = now - timedelta(seconds=30)
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "claude", "sequence": 8},
                fingerprint="terminal-input-claude-exit-without-completion",
                created_at=now - timedelta(seconds=40),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="claude-session-1",
                kind="assistant_message",
                virtual_window_id=window.id,
                payload_json={"provider": "claude_code", "role": "assistant", "content": "working"},
                fingerprint="claude-agent-output-before-exit",
                created_at=now - timedelta(seconds=30),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"command": "", "sequence": 8, "exit_status": 0},
                fingerprint="terminal-finished-claude-exit-without-completion",
                created_at=now - timedelta(seconds=20),
            ),
        ]
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "RECENT_ACTIVE"
    assert status.last_activity_at == now - timedelta(seconds=20)
    assert status.last_working_activity_at is None


@pytest.mark.asyncio
async def test_load_work_status_allows_recent_late_agent_output_after_shell_exit(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    window.agent_activity_latest_at = now - timedelta(seconds=5)
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'done'", "sequence": 9},
                fingerprint="terminal-input-codex-late-agent-output",
                created_at=now - timedelta(seconds=40),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"command": "", "sequence": 9, "exit_status": 0},
                fingerprint="terminal-finished-codex-late-agent-output",
                created_at=now - timedelta(seconds=20),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json={
                    "provider": "codex",
                    "raw_type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "late watcher write"}],
                    },
                },
                fingerprint="codex-agent-output-after-shell-exit",
                created_at=now - timedelta(seconds=5),
            ),
        ]
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "WORKING"
    assert status.last_activity_at == now - timedelta(seconds=5)
    assert status.last_working_activity_at == now - timedelta(seconds=5)


@pytest.mark.asyncio
async def test_load_work_status_ignores_late_agent_output_after_shell_exit_delay_window(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    window.agent_activity_latest_at = now - timedelta(seconds=40)
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'done'", "sequence": 10},
                fingerprint="terminal-input-codex-late-agent-output-delay",
                created_at=now - timedelta(seconds=80),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"command": "", "sequence": 10, "exit_status": 0},
                fingerprint="terminal-finished-codex-late-agent-output-delay",
                created_at=now - timedelta(seconds=60),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_message_payload("late watcher write"),
                fingerprint="codex-agent-output-after-shell-exit-delay",
                created_at=now - timedelta(seconds=5),
            ),
        ]
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "RECENT_ACTIVE"
    assert status.last_activity_at == now - timedelta(seconds=60)
    assert status.last_working_activity_at is None


@pytest.mark.asyncio
async def test_load_work_status_ignores_old_agent_output_written_after_shell_exit(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
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
                payload_json={"command": "codex exec 'done'", "sequence": 12},
                fingerprint="terminal-input-codex-old-output-late-write",
                created_at=now - timedelta(seconds=80),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"command": "", "sequence": 12, "exit_status": 0},
                fingerprint="terminal-finished-codex-old-output-late-write",
                created_at=now - timedelta(seconds=20),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_message_payload(
                    "old output inserted late",
                    timestamp=now - timedelta(seconds=40),
                ),
                fingerprint="codex-agent-old-output-late-write",
                created_at=now - timedelta(seconds=5),
            ),
        ]
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "RECENT_ACTIVE"
    assert status.last_activity_at == now - timedelta(seconds=20)
    assert status.last_working_activity_at is None


@pytest.mark.asyncio
async def test_load_work_status_returns_to_working_after_completion_in_same_running_session(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
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
                payload_json={"command": "codex", "sequence": 11},
                fingerprint="terminal-input-codex-multi-turn",
                created_at=now - timedelta(minutes=3),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="event_msg",
                virtual_window_id=window.id,
                payload_json=codex_completion_payload(),
                fingerprint="codex-agent-first-turn-complete",
                created_at=now - timedelta(seconds=40),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_message_payload("second turn started"),
                fingerprint="codex-agent-second-turn-output",
                created_at=now - timedelta(seconds=10),
            ),
        ]
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)

    assert status.state == "WORKING"
    assert status.last_activity_at == now - timedelta(seconds=10)
    assert status.last_working_activity_at == now - timedelta(seconds=10)


@pytest.mark.asyncio
async def test_load_last_agent_task_completed_uses_codex_completion_event(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    started_at = now - timedelta(seconds=30)
    completed_at = now - timedelta(seconds=5)
    window.agent_activity_latest_at = completed_at
    window.agent_activity_latest_completed_at = completed_at
    db_session.add_all([
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_input_command",
            virtual_window_id=window.id,
            payload_json={"command": "codex exec 'done'", "sequence": 2},
            fingerprint="terminal-input-codex",
            created_at=started_at,
        ),
        Event(
            client_id=client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="codex-session-1",
            kind="event_msg",
            virtual_window_id=window.id,
            payload_json=codex_completion_payload(),
            fingerprint="codex-agent-completed",
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
async def test_load_last_agent_task_completed_uses_codex_task_complete_event(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    completed_at = now - timedelta(seconds=5)
    window.agent_activity_latest_at = completed_at
    window.agent_activity_latest_completed_at = completed_at
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_message_payload("done", timestamp=completed_at - timedelta(seconds=1)),
                fingerprint="codex-agent-final-message-before-task-complete",
                created_at=completed_at - timedelta(seconds=1),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="event_msg",
                virtual_window_id=window.id,
                payload_json=codex_completion_payload(event_type="task_complete", timestamp=completed_at),
                fingerprint="codex-agent-task-complete",
                created_at=completed_at,
            ),
        ]
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)
    latest = await load_last_agent_task_completed_at_by_window(
        db_session,
        client_id,
        [window.id],
        now=now,
    )
    activity = await load_tree_window_activity(db_session, client_id, [window.id], now=now)

    assert status.state == "FINISHED"
    assert latest[window.id] == completed_at
    task_status = activity.last_agent_task_status[window.id]
    assert task_status.state == "FINISHED"
    assert task_status.occurred_at == completed_at


@pytest.mark.asyncio
async def test_load_last_agent_task_completed_uses_claude_turn_duration_event(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    completed_at = now - timedelta(seconds=5)
    window.agent_activity_latest_at = completed_at
    window.agent_activity_latest_completed_at = completed_at
    db_session.add(
        Event(
            client_id=client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="claude-session-1",
            kind="system",
            virtual_window_id=window.id,
            payload_json=claude_turn_duration_payload(timestamp=completed_at),
            fingerprint="claude-agent-turn-duration-complete",
            created_at=completed_at,
        )
    )
    await db_session.flush()

    status = await load_work_status(db_session, client_id, window.id, now=now)
    latest = await load_last_agent_task_completed_at_by_window(
        db_session,
        client_id,
        [window.id],
        now=now,
    )
    activity = await load_tree_window_activity(db_session, client_id, [window.id], now=now)

    assert status.state == "FINISHED"
    assert latest[window.id] == completed_at
    task_status = activity.last_agent_task_status[window.id]
    assert task_status.state == "FINISHED"
    assert task_status.occurred_at == completed_at


@pytest.mark.asyncio
async def test_claude_local_command_events_do_not_override_turn_completion(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    completed_at = now - timedelta(minutes=2)
    local_command_at = now - timedelta(seconds=10)
    window.agent_activity_latest_at = local_command_at
    window.agent_activity_latest_completed_at = completed_at
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="claude-session-1",
                kind="system",
                virtual_window_id=window.id,
                payload_json=claude_turn_duration_payload(timestamp=completed_at),
                fingerprint="claude-agent-turn-duration-before-local-command",
                created_at=completed_at,
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="claude-session-1",
                kind="user_message",
                virtual_window_id=window.id,
                payload_json=claude_local_command_payload("<bash-stdout>WEB_TERMINAL_CLAUDE_CODE_HOME=...</bash-stdout>"),
                fingerprint="claude-local-command-after-finish",
                created_at=local_command_at,
            ),
        ]
    )
    await db_session.flush()

    activity = await load_tree_window_activity(db_session, client_id, [window.id], now=now)

    assert activity.work_statuses[window.id].state == "FINISHED"
    assert activity.work_statuses[window.id].last_working_activity_at == completed_at
    task_status = activity.last_agent_task_status[window.id]
    assert task_status.state == "FINISHED"
    assert task_status.occurred_at == completed_at


@pytest.mark.asyncio
async def test_load_last_agent_task_completed_ignores_agent_output_without_completion(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    result_at = now - timedelta(seconds=10)
    window.agent_activity_latest_at = result_at
    db_session.add(
        Event(
            client_id=client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="codex-session-1",
            kind="response_item",
            virtual_window_id=window.id,
            payload_json={
                "provider": "codex",
                "raw_type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "still working"}],
                },
            },
            fingerprint="codex-agent-result-still-working",
            created_at=result_at,
        )
    )
    await db_session.flush()

    latest = await load_last_agent_task_completed_at_by_window(
        db_session,
        client_id,
        [window.id],
        now=now,
    )

    assert latest == {}


@pytest.mark.asyncio
async def test_load_last_agent_task_completed_ignores_agent_command_without_result_output(db_session) -> None:
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

    assert latest == {}


@pytest.mark.asyncio
async def test_load_last_agent_task_completed_does_not_treat_shell_exit_as_agent_completion(
    db_session,
) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    old_completed_at = now - timedelta(minutes=10)
    new_started_at = now - timedelta(minutes=2)
    new_finished_at = now - timedelta(minutes=1)
    window.agent_activity_latest_at = old_completed_at
    window.agent_activity_latest_completed_at = old_completed_at
    db_session.add_all([
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_input_command",
            virtual_window_id=window.id,
            payload_json={"command": "claude -p 'fix'", "sequence": 1},
            fingerprint="terminal-input-claude-old",
            created_at=old_completed_at - timedelta(seconds=10),
        ),
        Event(
            client_id=client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="claude-session-1",
            kind="assistant_message",
            virtual_window_id=window.id,
            payload_json=claude_completion_payload(),
            fingerprint="claude-agent-completed-old",
            created_at=old_completed_at,
        ),
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_input_command",
            virtual_window_id=window.id,
            payload_json={"command": "claude", "sequence": 2},
            fingerprint="terminal-input-claude-new",
            created_at=new_started_at,
        ),
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_command_finished",
            virtual_window_id=window.id,
            payload_json={"command": "", "sequence": 2, "exit_status": 0},
            fingerprint="terminal-finished-claude-new",
            created_at=new_finished_at,
        ),
    ])
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
    assert stored == old_completed_at


@pytest.mark.asyncio
async def test_load_last_agent_task_completed_prefers_newer_completion_event(
    db_session,
) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    older_completed_at = now - timedelta(minutes=2)
    newer_completed_at = now - timedelta(seconds=5)
    window.agent_activity_latest_at = newer_completed_at
    window.agent_activity_latest_completed_at = newer_completed_at
    db_session.add_all([
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_input_command",
            virtual_window_id=window.id,
            payload_json={"command": "codex exec 'newer done'", "sequence": 2},
            fingerprint="terminal-input-codex-newer",
            created_at=newer_completed_at - timedelta(seconds=30),
        ),
        Event(
            client_id=client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="claude-session-1",
            kind="assistant_message",
            virtual_window_id=window.id,
            payload_json=claude_completion_payload(),
            fingerprint="claude-agent-completed-older",
            created_at=older_completed_at,
        ),
        Event(
            client_id=client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="codex-session-1",
            kind="event_msg",
            virtual_window_id=window.id,
            payload_json=codex_completion_payload(),
            fingerprint="codex-agent-completed-newer",
            created_at=newer_completed_at,
        ),
    ])
    await db_session.flush()

    latest = await load_last_agent_task_completed_at_by_window(db_session, client_id, [window.id])

    stored = latest[window.id]
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored == newer_completed_at


@pytest.mark.asyncio
async def test_load_last_agent_task_completed_ignores_idle_agent_activity_without_completion(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    working_at = now - timedelta(seconds=120)
    window.agent_activity_latest_at = working_at
    db_session.add(
        Event(
            client_id=client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="claude-session-1",
            kind="assistant_message",
            virtual_window_id=window.id,
            payload_json={"provider": "claude_code", "role": "assistant", "content": "working"},
            fingerprint="claude-agent-idle-work",
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

    assert latest == {}


@pytest.mark.asyncio
async def test_load_tree_window_activity_reports_abort_notification_status(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    output_at = now - timedelta(minutes=61)
    window.agent_activity_latest_at = output_at
    db_session.add_all([
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_input_command",
            virtual_window_id=window.id,
            payload_json={"command": "codex exec 'hang'", "sequence": 2},
            fingerprint="terminal-input-codex-hang",
            created_at=now - timedelta(minutes=62),
        ),
        Event(
            client_id=client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="codex-session-1",
            kind="response_item",
            virtual_window_id=window.id,
            payload_json={
                "provider": "codex",
                "raw_type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "still running"}],
                },
            },
            fingerprint="codex-agent-hang-output",
            created_at=output_at,
        ),
    ])
    await db_session.flush()

    activity = await load_tree_window_activity(db_session, client_id, [window.id], now=now)

    assert activity.work_statuses[window.id].state == "ABORTED"
    task_status = activity.last_agent_task_status[window.id]
    assert task_status.state == "ABORTED"
    assert task_status.occurred_at == output_at + timedelta(hours=1)


@pytest.mark.asyncio
async def test_load_tree_window_activity_reports_abort_for_agent_command_with_no_output(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    command_at = now - timedelta(minutes=70)
    window.agent_activity_latest_at = command_at
    db_session.add(
        Event(
            client_id=client_id,
            source_type=EventSourceType.terminal,
            source_id=str(window.id),
            kind="terminal_input_command",
            virtual_window_id=window.id,
            payload_json={"command": "codex exec 'hang before output'", "sequence": 2},
            fingerprint="terminal-input-codex-no-output-hang",
            created_at=command_at,
        )
    )
    await db_session.flush()

    activity = await load_tree_window_activity(db_session, client_id, [window.id], now=now)

    assert activity.work_statuses[window.id].state == "ABORTED"
    task_status = activity.last_agent_task_status[window.id]
    assert task_status.state == "ABORTED"
    assert task_status.occurred_at == command_at + timedelta(hours=1)


@pytest.mark.asyncio
async def test_load_tree_window_activity_does_not_abort_after_shell_exit_without_completion(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    window.agent_activity_latest_at = now - timedelta(minutes=71)
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'exit without semantic completion'", "sequence": 2},
                fingerprint="terminal-input-codex-exit-no-completion",
                created_at=now - timedelta(minutes=72),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_message_payload("ran but no completion event"),
                fingerprint="codex-agent-output-exit-no-completion",
                created_at=now - timedelta(minutes=71),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"command": "", "sequence": 2, "exit_status": 0},
                fingerprint="terminal-finished-codex-exit-no-completion",
                created_at=now - timedelta(minutes=70),
            ),
        ]
    )
    await db_session.flush()

    activity = await load_tree_window_activity(db_session, client_id, [window.id], now=now)

    assert activity.work_statuses[window.id].state == "LONG_IDLE"
    assert window.id not in activity.last_agent_task_status


@pytest.mark.asyncio
async def test_load_tree_window_activity_allows_late_agent_output_within_delay_window(
    db_session,
) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    window.agent_activity_latest_at = now - timedelta(seconds=5)
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'done'", "sequence": 4},
                fingerprint="terminal-input-codex-late-output-tree",
                created_at=now - timedelta(seconds=40),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"command": "", "sequence": 4, "exit_status": 0},
                fingerprint="terminal-finished-codex-late-output-tree",
                created_at=now - timedelta(seconds=20),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_message_payload("late watcher write"),
                fingerprint="codex-agent-output-after-shell-exit-tree",
                created_at=now - timedelta(seconds=5),
            ),
        ]
    )
    await db_session.flush()

    activity = await load_tree_window_activity(db_session, client_id, [window.id], now=now)

    assert activity.work_statuses[window.id].state == "WORKING"
    assert activity.work_statuses[window.id].last_activity_at == now - timedelta(seconds=5)
    assert activity.work_statuses[window.id].last_working_activity_at == now - timedelta(seconds=5)
    assert window.id not in activity.last_agent_task_status


@pytest.mark.asyncio
async def test_load_tree_window_activity_ignores_late_agent_output_after_delay_window(
    db_session,
) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    window.agent_activity_latest_at = now - timedelta(seconds=5)
    db_session.add_all(
        [
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_input_command",
                virtual_window_id=window.id,
                payload_json={"command": "codex exec 'done'", "sequence": 5},
                fingerprint="terminal-input-codex-late-output-tree-delay",
                created_at=now - timedelta(seconds=80),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.terminal,
                source_id=str(window.id),
                kind="terminal_command_finished",
                virtual_window_id=window.id,
                payload_json={"command": "", "sequence": 5, "exit_status": 0},
                fingerprint="terminal-finished-codex-late-output-tree-delay",
                created_at=now - timedelta(seconds=60),
            ),
            Event(
                client_id=client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="codex-session-1",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json=codex_message_payload("late watcher write"),
                fingerprint="codex-agent-output-after-shell-exit-tree-delay",
                created_at=now - timedelta(seconds=5),
            ),
        ]
    )
    await db_session.flush()

    activity = await load_tree_window_activity(db_session, client_id, [window.id], now=now)

    assert activity.work_statuses[window.id].state == "RECENT_ACTIVE"
    assert activity.work_statuses[window.id].last_activity_at == now - timedelta(seconds=60)
    assert activity.work_statuses[window.id].last_working_activity_at is None
    assert window.id not in activity.last_agent_task_status


@pytest.mark.asyncio
async def test_load_tree_window_activity_reports_finished_notification_status(db_session) -> None:
    now = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)
    client_id = uuid4()
    window = VirtualWindow(id=uuid4(), client_id=client_id, title="Terminal", status=WindowStatus.active)
    db_session.add(window)
    await db_session.flush()
    completed_at = now - timedelta(seconds=30)
    window.agent_activity_latest_at = completed_at
    window.agent_activity_latest_completed_at = completed_at
    db_session.add(
        Event(
            client_id=client_id,
            source_type=EventSourceType.agent_tool_record,
            source_id="claude-session-1",
            kind="assistant_message",
            virtual_window_id=window.id,
            payload_json=claude_completion_payload(),
            fingerprint="claude-agent-finished-status",
            created_at=completed_at,
        )
    )
    await db_session.flush()

    activity = await load_tree_window_activity(db_session, client_id, [window.id], now=now)

    assert activity.work_statuses[window.id].state == "FINISHED"
    task_status = activity.last_agent_task_status[window.id]
    assert task_status.state == "FINISHED"
    assert task_status.occurred_at == completed_at
