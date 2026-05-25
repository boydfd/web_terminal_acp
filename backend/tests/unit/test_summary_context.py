import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.model_base import Base
from app.models import AiSession, Client, ClientRuntime, ClientStatus, Event, EventSourceType, VirtualWindow
from app.repositories.clients import ensure_local_client, hash_client_token
from app.repositories.folders import get_or_create_folder_by_path
from app.repositories.summary_jobs import collect_summary_context
from app.repositories.windows import create_window


@pytest.fixture
async def session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield Session
    finally:
        await engine.dispose()


async def create_local_window(session):
    client = await ensure_local_client(session)
    return await create_window(session, client.id, cwd="/workspace/project", shell_command="/bin/bash")


def command_event(window: VirtualWindow, sequence: int, command: str, captured_at: datetime) -> Event:
    return Event(
        client_id=window.client_id,
        source_type=EventSourceType.terminal,
        source_id=f"terminal-command-{sequence}",
        kind="terminal_input_command",
        virtual_window_id=window.id,
        payload_json={
            "sequence": sequence,
            "command": command,
            "shell": "/bin/bash",
            "cwd": f"/workspace/project-{sequence}",
            "captured_at": captured_at.isoformat(),
        },
        fingerprint=f"terminal-command-{sequence}",
        created_at=captured_at,
    )


def output_event(window: VirtualWindow, text: str, created_at: datetime) -> Event:
    return Event(
        client_id=window.client_id,
        source_type=EventSourceType.terminal,
        source_id="terminal-output",
        kind="terminal_output",
        virtual_window_id=window.id,
        payload_json={"text": text},
        fingerprint="terminal-output",
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_collect_summary_context_is_input_command_first_for_terminal_windows(session_factory):
    captured_at = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        window = await create_local_window(session)
        session.add(output_event(window, "large-output-should-not-drive-summary" * 1000, captured_at))
        session.add(command_event(window, 1, "pwd", captured_at + timedelta(seconds=1)))
        session.add(command_event(window, 2, "pytest backend/tests", captured_at + timedelta(seconds=2)))
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    assert context[0]["source_type"] == "terminal"
    assert context[0]["kind"] == "terminal_input_context"
    payload = context[0]["payload"]
    assert payload["window"]["cwd"] == "/workspace/project"
    assert payload["window"]["shell_command"] == "/bin/bash"
    assert payload["date"]["year_month"] == datetime.now(timezone.utc).strftime("%Y-%m")
    assert payload["date"]["year_month_day"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert payload["commands"] == [
        {
            "sequence": 1,
            "command": "pwd",
            "shell": "/bin/bash",
            "cwd": "/workspace/project-1",
            "captured_at": "2026-05-20T12:00:01+00:00",
        },
        {
            "sequence": 2,
            "command": "pytest backend/tests",
            "shell": "/bin/bash",
            "cwd": "/workspace/project-2",
            "captured_at": "2026-05-20T12:00:02+00:00",
        },
    ]
    assert "large-output-should-not-drive-summary" not in json.dumps(context)


@pytest.mark.asyncio
async def test_collect_summary_context_includes_topic_tree_leaf_counts_and_language(session_factory, monkeypatch):
    monkeypatch.setattr(
        "app.repositories.summary_jobs.get_settings",
        lambda: Settings(_env_file=None, summary_output_language="English"),
        raising=False,
    )

    async with session_factory() as session:
        window = await create_local_window(session)
        await get_or_create_folder_by_path(session, window.client_id, "/开发调试")
        child = await get_or_create_folder_by_path(session, window.client_id, "/开发调试/后端摘要")
        window.folder_id = child.id
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    payload = context[0]["payload"]
    assert payload["summary_output_language"] == "English"
    assert "folder_paths" not in payload
    assert payload["topic_tree"] == [
        {
            "path": "/未分类",
            "name": "未分类",
            "is_leaf": True,
            "terminal_count": 0,
            "children": [],
        },
        {
            "path": "/开发调试",
            "name": "开发调试",
            "is_leaf": False,
            "terminal_count": 0,
            "children": [
                {
                    "path": "/开发调试/后端摘要",
                    "name": "后端摘要",
                    "is_leaf": True,
                    "terminal_count": 1,
                    "children": [],
                }
            ],
        },
    ]
    assert payload["topic_tree_truncation"] == {"truncated": False, "budget_bytes": 32768}


@pytest.mark.asyncio
async def test_collect_summary_context_topic_tree_is_client_scoped_and_ordered(session_factory):
    async with session_factory() as session:
        window = await create_local_window(session)
        await get_or_create_folder_by_path(session, window.client_id, "/z-last")
        await get_or_create_folder_by_path(session, window.client_id, "/a-first")
        other_client = Client(
            id=uuid4(),
            name="other-client",
            status=ClientStatus.ONLINE,
            runtime=ClientRuntime.remote,
            token_hash=hash_client_token("other-token"),
        )
        session.add(other_client)
        await session.flush()
        await get_or_create_folder_by_path(session, other_client.id, "/other-client-secret")
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    topic_tree = context[0]["payload"]["topic_tree"]
    root_paths = [node["path"] for node in topic_tree]
    assert root_paths == ["/未分类", "/a-first", "/z-last"]
    assert "/other-client-secret" not in json.dumps(topic_tree, ensure_ascii=False)


@pytest.mark.asyncio
async def test_collect_summary_context_prunes_topic_tree_to_fit_budget(session_factory, monkeypatch):
    class SmallBudgetSettings(Settings):
        terminal_summary_input_context_max_bytes: int = 1600

    monkeypatch.setattr(
        "app.repositories.summary_jobs.get_settings",
        lambda: SmallBudgetSettings(_env_file=None, summary_output_language="English"),
        raising=False,
    )

    async with session_factory() as session:
        window = await create_local_window(session)
        for index in range(40):
            await get_or_create_folder_by_path(
                session,
                window.client_id,
                f"/topic-{index:02d}-{'x' * 24}/child-{'y' * 24}",
            )
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    serialized_size = len(
        json.dumps(context[0], ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    payload = context[0]["payload"]
    assert serialized_size <= 1600
    assert payload["topic_tree_truncation"] == {"truncated": True, "budget_bytes": 1600}
    assert payload["summary_output_language"] == "English"
    assert payload["topic_tree"]


@pytest.mark.asyncio
async def test_collect_summary_context_includes_ai_events_when_commands_are_absent(session_factory):
    created_at = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        window = await create_local_window(session)
        session.add(
            Event(
                client_id=window.client_id,
                source_type=EventSourceType.claude_jsonl,
                source_id="claude-session-1",
                kind="user_message",
                virtual_window_id=window.id,
                payload_json={
                    "type": "user",
                    "sessionId": "claude-session-1",
                    "message": {"content": "帮我修复 codex summary 缺少输入的问题"},
                },
                fingerprint="claude-user-message",
                created_at=created_at,
            )
        )
        session.add(
            Event(
                client_id=window.client_id,
                source_type=EventSourceType.codex_trace,
                source_id="codex-trace-1",
                kind="tool_call",
                virtual_window_id=window.id,
                payload_json={
                    "trace_id": "codex-trace-1",
                    "span": {"name": "tool_call", "attributes": {"tool": "bash", "input": "pytest"}},
                },
                fingerprint="codex-tool-call",
                created_at=created_at + timedelta(seconds=1),
            )
        )
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    payload = context[0]["payload"]
    assert payload["commands"] == []
    assert [event["provider"] for event in payload["ai_events"]] == ["claude_code", "codex"]
    assert payload["ai_events"][0]["role"] == "user"
    assert "codex summary" in payload["ai_events"][0]["text"]
    assert "pytest" in payload["ai_events"][1]["text"]


@pytest.mark.asyncio
async def test_collect_summary_context_includes_generic_agent_tool_record_with_adapter_text(session_factory):
    created_at = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        window = await create_local_window(session)
        ai_session = AiSession(
            client_id=window.client_id,
            provider="cursor_cli",
            source_id="cursor-session-1",
            source_path="/tmp/cursor-records.jsonl",
            project_path=window.cwd,
            virtual_window_id=window.id,
        )
        session.add(ai_session)
        await session.flush()
        session.add(
            Event(
                client_id=window.client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="cursor-session-1",
                kind="assistant_message",
                virtual_window_id=window.id,
                ai_session_id=ai_session.id,
                payload_json={
                    "provider": "cursor_cli",
                    "role": "assistant",
                    "content": "Cursor adapter summary text",
                    "debug": {"ignored": "raw fallback should not be needed"},
                },
                fingerprint="cursor-agent-tool-record",
                created_at=created_at,
            )
        )
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    ai_events = context[0]["payload"]["ai_events"]
    assert ai_events == [
        {
            "source_type": "agent_tool_record",
            "provider": "cursor_cli",
            "source_id": "cursor-session-1",
            "kind": "assistant_message",
            "role": "assistant",
            "text": "Cursor adapter summary text",
            "created_at": ai_events[0]["created_at"],
        }
    ]


@pytest.mark.asyncio
async def test_collect_summary_context_over_budget_keeps_recent_commands_and_marks_truncation(
    session_factory, monkeypatch
):
    class SmallBudgetSettings(Settings):
        terminal_summary_input_context_max_bytes: int = 1600

    monkeypatch.setattr(
        "app.repositories.summary_jobs.get_settings",
        lambda: SmallBudgetSettings(_env_file=None),
        raising=False,
    )
    captured_at = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        window = await create_local_window(session)
        for sequence in range(20):
            session.add(
                command_event(
                    window,
                    sequence,
                    f"command-{sequence} " + ("x" * 120),
                    captured_at + timedelta(seconds=sequence),
                )
            )
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    payload = context[0]["payload"]
    included_sequences = [command["sequence"] for command in payload["commands"]]
    assert included_sequences
    assert included_sequences == list(range(20 - len(included_sequences), 20))
    assert payload["truncation"] == {
        "total_commands": 20,
        "included_commands": len(included_sequences),
        "truncated": True,
        "budget_bytes": 1600,
    }


def test_settings_default_terminal_summary_input_context_max_bytes():
    assert Settings(_env_file=None).terminal_summary_input_context_max_bytes in {32768, 65536}
