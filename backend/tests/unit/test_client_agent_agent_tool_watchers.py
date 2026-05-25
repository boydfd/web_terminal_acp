from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import UUID

import pytest

from app.client_agent.agent_tool_watchers import (
    AGENT_TOOL_COLLECTORS,
    AgentToolWatcherState,
    collect_claude_code_watch_events,
    collect_codex_watch_events,
    collect_cursor_watch_events,
    enqueue_managed_ai_event,
)
from app.services.runtime.protocol import AgentMessage

CLIENT_ID = UUID("12345678-1234-5678-1234-567812345678")
WINDOW_ID = UUID("87654321-4321-8765-4321-876543218765")


def test_agent_tool_collectors_are_centralized_by_provider() -> None:
    assert AGENT_TOOL_COLLECTORS == (
        ("codex", "collect_codex_watch_events"),
        ("claude_code", "collect_claude_code_watch_events"),
        ("cursor_cli", "collect_cursor_watch_events"),
    )


def write_cursor_store(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("create table meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("create table blobs (id TEXT PRIMARY KEY, data BLOB)")
    meta = {
        "agentId": "cursor-agent-1",
        "latestRootBlobId": "root-1",
        "name": "Cursor Test Chat",
        "createdAt": 1779520336671,
        "lastUsedModel": "default",
    }
    conn.execute("insert into meta (key, value) values (?, ?)", ("0", json.dumps(meta).encode("utf-8").hex()))
    conn.execute(
        "insert into blobs (id, data) values (?, ?)",
        (
            "user-blob",
            json.dumps({"role": "user", "content": [{"type": "text", "text": "hi"}]}).encode("utf-8"),
        ),
    )
    conn.execute(
        "insert into blobs (id, data) values (?, ?)",
        ("assistant-blob", json.dumps({"role": "assistant", "content": "hello"}).encode("utf-8")),
    )
    conn.execute("insert into blobs (id, data) values (?, ?)", ("binary-blob", b"\x0a\x02hi"))
    conn.commit()
    conn.close()


def test_collect_codex_watch_events_preserves_payload_shape_and_project_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_file = tmp_path / "rollout-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
                "timestamp": "2026-05-23T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.iter_codex_session_files",
        lambda window_id: [session_file],
    )
    state = AgentToolWatcherState()

    events = collect_codex_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert len(events) == 1
    event = events[0]
    assert event.provider == "codex"
    assert event.source_path == str(session_file)
    assert event.offset == 0
    assert event.cursor == 0
    assert event.project_path == "/workspace/project"
    assert event.payload["client_id"] == str(CLIENT_ID)
    assert event.payload["virtual_window_id"] == str(WINDOW_ID)
    assert event.payload["project_path"] == "/workspace/project"
    assert state.codex_offsets[session_file] == session_file.stat().st_size


def test_collect_codex_watch_events_resets_offset_after_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_file = tmp_path / "rollout-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "after rotate"}],
                },
                "timestamp": "2026-05-23T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.iter_codex_session_files",
        lambda window_id: [session_file],
    )
    state = AgentToolWatcherState(codex_offsets={session_file: 10_000})

    events = collect_codex_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert len(events) == 1
    assert events[0].offset == 0
    assert events[0].cursor == 0
    assert events[0].payload["payload"]["content"][0]["text"] == "after rotate"
    assert state.codex_offsets[session_file] == session_file.stat().st_size


def test_collect_claude_code_watch_events_reads_managed_home_and_tracks_offsets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_file = tmp_path / "managed" / "nested" / "session.jsonl"
    session_file.parent.mkdir(parents=True)
    session_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "claude-session-1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.iter_claude_code_jsonl_files",
        lambda window_id: [session_file],
    )
    state = AgentToolWatcherState()

    first = collect_claude_code_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )
    second = collect_claude_code_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert len(first) == 1
    event = first[0]
    assert event.provider == "claude_code"
    assert event.source_path == str(session_file)
    assert event.offset == 0
    assert event.cursor == 0
    assert event.project_path == "/workspace/project"
    assert event.payload["WEB_TERMINAL_CLIENT_ID"] == str(CLIENT_ID)
    assert event.payload["WEB_TERMINAL_WINDOW_ID"] == str(WINDOW_ID)
    assert event.payload["WEB_TERMINAL_PROJECT_PATH"] == "/workspace/project"
    assert event.payload["type"] == "assistant"
    assert second == []
    assert state.claude_code_offsets[session_file] == session_file.stat().st_size


def test_collect_cursor_watch_events_finds_window_stores_and_tracks_seen_blobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    store = home / ".web-terminal-acp" / "cursor-homes" / str(WINDOW_ID) / "state" / "store.db"
    store.parent.mkdir(parents=True)
    write_cursor_store(store)
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()

    first = collect_cursor_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )
    second = collect_cursor_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert [event.provider for event in first] == ["cursor_cli", "cursor_cli"]
    assert [event.source_path for event in first] == [str(store), str(store)]
    assert [event.cursor for event in first] == ["root-1", "root-1"]
    assert state.cursor_store_paths == [store]
    assert second == []
    assert state.cursor_seen_blob_ids[store] == {"user-blob", "assistant-blob"}
    assert [event.payload["blob_id"] for event in first] == ["user-blob", "assistant-blob"]
    assert all(event.payload["client_id"] == str(CLIENT_ID) for event in first)
    assert all(event.payload["virtual_window_id"] == str(WINDOW_ID) for event in first)
    assert all(event.payload["project_path"] == "/workspace/project" for event in first)


def test_collect_cursor_watch_events_discovers_store_created_after_initial_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()

    assert collect_cursor_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    ) == []

    store = home / ".web-terminal-acp" / "cursor-homes" / str(WINDOW_ID) / "state" / "store.db"
    store.parent.mkdir(parents=True)
    write_cursor_store(store)

    events = collect_cursor_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert [event.payload["blob_id"] for event in events] == ["user-blob", "assistant-blob"]
    assert state.cursor_store_paths == [store]


def test_collect_cursor_watch_events_finds_managed_cursor_data_dir_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    store = (
        home
        / ".web-terminal-acp"
        / "cursor-homes"
        / str(WINDOW_ID)
        / "chats"
        / "workspace-hash"
        / "session-id"
        / "store.db"
    )
    store.parent.mkdir(parents=True)
    write_cursor_store(store)
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()

    events = collect_cursor_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert [event.source_path for event in events] == [str(store), str(store)]
    assert state.cursor_store_paths == [store]


def test_collect_cursor_watch_events_discovers_additional_store_after_first_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    first_store = home / ".web-terminal-acp" / "cursor-homes" / str(WINDOW_ID) / "state-a" / "store.db"
    first_store.parent.mkdir(parents=True)
    write_cursor_store(first_store)
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()

    first = collect_cursor_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )
    second_store = home / ".web-terminal-acp" / "cursor-homes" / str(WINDOW_ID) / "state-b" / "store.db"
    second_store.parent.mkdir(parents=True)
    write_cursor_store(second_store)
    second = collect_cursor_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert [event.source_path for event in first] == [str(first_store), str(first_store)]
    assert [event.source_path for event in second] == [str(second_store), str(second_store)]
    assert state.cursor_store_paths == [first_store, second_store]


@pytest.mark.asyncio
async def test_enqueue_managed_ai_event_includes_cursor_and_project_path() -> None:
    from app.client_agent.ai_events import ManagedAiEvent

    messages: list[AgentMessage] = []

    async def send_message(message: AgentMessage) -> None:
        messages.append(message)

    payload = {
        "client_id": str(CLIENT_ID),
        "virtual_window_id": str(WINDOW_ID),
        "agentId": "cursor-agent-1",
        "blob_id": "assistant-blob",
        "role": "assistant",
        "text": "hello",
    }
    event = ManagedAiEvent(
        provider="cursor_cli",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        source_path="/tmp/store.db",
        offset=None,
        cursor="root-1",
        project_path="/workspace/project",
        payload=payload,
    )

    sent = await enqueue_managed_ai_event(send_message, event)

    assert sent is True
    assert len(messages) == 1
    message = messages[0]
    assert message.type == "ai_event"
    assert message.client_id == CLIENT_ID
    assert message.window_id == WINDOW_ID
    assert message.payload == {
        "provider": "cursor_cli",
        "source_path": "/tmp/store.db",
        "offset": None,
        "cursor": "root-1",
        "project_path": "/workspace/project",
        "payload": payload,
    }
