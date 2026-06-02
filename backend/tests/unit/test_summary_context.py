import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, select
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


@pytest.fixture
async def counted_session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/counted.db")
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def record_statement(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield Session, statements
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
async def test_collect_summary_context_includes_session_messages_when_commands_are_absent(session_factory):
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
                kind="assistant_message",
                virtual_window_id=window.id,
                payload_json={
                    "raw_type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "用 pytest 验证修复"}],
                    },
                },
                fingerprint="codex-assistant-message",
                created_at=created_at + timedelta(seconds=1),
            )
        )
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    payload = context[0]["payload"]
    assert payload["commands"] == []
    assert payload["session_messages"] == [
        {"role": "user", "content": "帮我修复 codex summary 缺少输入的问题"},
        {"role": "assistant", "content": "用 pytest 验证修复"},
    ]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "claude-session-1" not in serialized
    assert "codex-trace-1" not in serialized
    assert "created_at" not in serialized


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

    assert context[0]["payload"]["session_messages"] == [
        {"role": "assistant", "content": "Cursor adapter summary text"}
    ]


@pytest.mark.asyncio
async def test_collect_summary_context_excludes_subagent_prompt_from_session_messages(session_factory):
    created_at = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        window = await create_local_window(session)
        main_session = AiSession(
            client_id=window.client_id,
            provider="claude_code",
            source_id="main-session-1",
            project_path=window.cwd,
            virtual_window_id=window.id,
        )
        sub_session = AiSession(
            client_id=window.client_id,
            provider="claude_code",
            source_id="agent-subagent-1",
            project_path=window.cwd,
            virtual_window_id=window.id,
        )
        session.add_all([main_session, sub_session])
        await session.flush()
        session.add_all(
            [
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="main-session-1",
                    kind="user_message",
                    virtual_window_id=window.id,
                    ai_session_id=main_session.id,
                    payload_json={
                        "provider": "claude_code",
                        "type": "user",
                        "message": {"role": "user", "content": "主 agent 用户需求"},
                    },
                    fingerprint="summary-main-user-prompt",
                    created_at=created_at,
                ),
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="agent-subagent-1",
                    kind="user_message",
                    virtual_window_id=window.id,
                    ai_session_id=sub_session.id,
                    payload_json={
                        "provider": "claude_code",
                        "type": "user",
                        "sessionId": "main-session-1",
                        "agentId": "subagent-1",
                        "isSidechain": True,
                        "subagent": {"toolUseId": "call-subagent-1"},
                        "message": {"role": "user", "content": "subagent 内部 prompt"},
                    },
                    fingerprint="summary-subagent-user-prompt",
                    created_at=created_at + timedelta(seconds=1),
                ),
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="agent-subagent-1",
                    kind="assistant_message",
                    virtual_window_id=window.id,
                    ai_session_id=sub_session.id,
                    payload_json={
                        "provider": "claude_code",
                        "type": "assistant",
                        "sessionId": "main-session-1",
                        "agentId": "subagent-1",
                        "isSidechain": True,
                        "message": {"role": "assistant", "content": "subagent 返回的信息"},
                    },
                    fingerprint="summary-subagent-answer",
                    created_at=created_at + timedelta(seconds=2),
                ),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    assert context[0]["payload"]["session_messages"] == [
        {"role": "user", "content": "主 agent 用户需求"},
        {"role": "assistant", "content": "subagent 返回的信息"},
    ]
    assert "subagent 内部 prompt" not in json.dumps(context, ensure_ascii=False)


@pytest.mark.asyncio
async def test_collect_summary_context_filters_non_summary_session_content(session_factory):
    created_at = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        window = await create_local_window(session)
        session.add_all(
            [
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="cursor-session-1",
                    kind="user_message",
                    virtual_window_id=window.id,
                    payload_json={
                        "provider": "cursor_cli",
                        "role": "user",
                        "content": "<user_info>\nOS Version: linux\n</user_info>",
                    },
                    fingerprint="cursor-user-info-context",
                    created_at=created_at,
                ),
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="cursor-session-1",
                    kind="user_message",
                    virtual_window_id=window.id,
                    payload_json={
                        "provider": "cursor_cli",
                        "role": "user",
                        "content": "<user_query>\n排查 summary 输入过多的问题\n</user_query>",
                    },
                    fingerprint="cursor-real-user-query",
                    created_at=created_at + timedelta(seconds=1),
                ),
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="cursor-session-1",
                    kind="user_message",
                    virtual_window_id=window.id,
                    payload_json={
                        "provider": "cursor_cli",
                        "role": "user",
                        "content": "<encrypted>ciphertext</encrypted>",
                    },
                    fingerprint="cursor-encrypted-user-message",
                    created_at=created_at + timedelta(seconds=2),
                ),
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window.id,
                    payload_json={
                        "provider": "codex",
                        "raw_type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "# AGENTS.md instructions for /workspace\n\n<INSTRUCTIONS>...</INSTRUCTIONS>",
                                }
                            ],
                        },
                    },
                    fingerprint="codex-agents-context",
                    created_at=created_at + timedelta(seconds=3),
                ),
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="claude-session-1",
                    kind="user_message",
                    virtual_window_id=window.id,
                    payload_json={
                        "provider": "claude_code",
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tool-1",
                                    "content": "command output",
                                }
                            ],
                        },
                    },
                    fingerprint="claude-tool-result-user",
                    created_at=created_at + timedelta(seconds=4),
                ),
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window.id,
                    payload_json={
                        "provider": "codex",
                        "raw_type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": "{\"cmd\":\"pytest\"}",
                        },
                    },
                    fingerprint="codex-ordinary-tool-call",
                    created_at=created_at + timedelta(seconds=5),
                ),
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window.id,
                    payload_json={
                        "provider": "codex",
                        "raw_type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": "pytest output",
                        },
                    },
                    fingerprint="codex-tool-result",
                    created_at=created_at + timedelta(seconds=6),
                ),
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window.id,
                    payload_json={
                        "provider": "codex",
                        "raw_type": "response_item",
                        "payload": {
                            "type": "reasoning",
                            "summary": [{"text": "internal thinking"}],
                        },
                    },
                    fingerprint="codex-reasoning",
                    created_at=created_at + timedelta(seconds=7),
                ),
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="codex-session-1",
                    kind="response_item",
                    virtual_window_id=window.id,
                    payload_json={
                        "provider": "codex",
                        "raw_type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "request_user_input",
                            "arguments": json.dumps(
                                {"questions": [{"question": "要覆盖标题和文件夹吗?"}]},
                                ensure_ascii=False,
                            ),
                        },
                    },
                    fingerprint="codex-ask-user-question",
                    created_at=created_at + timedelta(seconds=8),
                ),
                Event(
                    client_id=window.client_id,
                    source_type=EventSourceType.agent_tool_record,
                    source_id="claude-session-1",
                    kind="assistant_message",
                    virtual_window_id=window.id,
                    payload_json={
                        "provider": "claude_code",
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "需要确认一个选项"},
                                {
                                    "type": "tool_use",
                                    "name": "ask_user_question",
                                    "input": {"question": "保留旧字段兼容吗?"},
                                }
                            ],
                        },
                    },
                    fingerprint="claude-ask-user-question",
                    created_at=created_at + timedelta(seconds=9),
                ),
                Event(
                    client_id=window.client_id,
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
                            "content": [{"type": "output_text", "text": "已完成过滤调整"}],
                        },
                    },
                    fingerprint="codex-assistant-reply",
                    created_at=created_at + timedelta(seconds=10),
                ),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        context = await collect_summary_context(session, window)

    assert context[0]["payload"]["session_messages"] == [
        {"role": "user", "content": "排查 summary 输入过多的问题"},
        {"role": "tool_call", "name": "request_user_input", "content": "要覆盖标题和文件夹吗?"},
        {"role": "assistant", "content": "需要确认一个选项"},
        {"role": "tool_call", "name": "ask_user_question", "content": "保留旧字段兼容吗?"},
        {"role": "assistant", "content": "已完成过滤调整"},
    ]
    serialized = json.dumps(context, ensure_ascii=False)
    assert "<user_info>" not in serialized
    assert "<encrypted>" not in serialized
    assert "AGENTS.md instructions" not in serialized
    assert "command output" not in serialized
    assert "pytest output" not in serialized
    assert "internal thinking" not in serialized
    assert "exec_command" not in serialized
    assert "codex-session-1" not in serialized


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


@pytest.mark.asyncio
async def test_collect_summary_context_uses_index_friendly_agent_event_reads(
    counted_session_factory,
):
    session_factory, statements = counted_session_factory
    created_at = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        window = await create_local_window(session)
        session.add(
            Event(
                client_id=window.client_id,
                source_type=EventSourceType.agent_tool_record,
                source_id="agent-record",
                kind="response_item",
                virtual_window_id=window.id,
                payload_json={"provider": "codex", "role": "assistant", "content": "working"},
                fingerprint="agent-record-index-friendly",
                created_at=created_at,
            )
        )
        await session.commit()

    statements.clear()
    async with session_factory() as session:
        window = (await session.execute(select(VirtualWindow))).scalar_one()
        await collect_summary_context(session, window)

    event_reads = [
        statement
        for statement in statements
        if "FROM events" in statement and "SELECT events" in statement
    ]
    assert event_reads
    assert all("events.client_id" in statement for statement in event_reads)
    assert all("source_type IN" not in statement for statement in event_reads)


def test_settings_default_terminal_summary_input_context_max_bytes():
    assert Settings(_env_file=None).terminal_summary_input_context_max_bytes in {32768, 65536}
