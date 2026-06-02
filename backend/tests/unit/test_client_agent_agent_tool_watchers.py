from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from pathlib import Path
from uuid import UUID

import pytest

import app.client_agent.agent_tool_watchers as watchers
from app.client_agent.agent_tool_watchers import (
    AGENT_TOOL_COLLECTORS,
    AgentToolWatcherState,
    UnifiedAgentToolWatcher,
    _collect_all_events,
    collect_claude_code_watch_events,
    collect_codex_watch_events,
    collect_cursor_watch_events,
    enqueue_managed_ai_event,
    initialize_agent_tool_watcher_state,
    read_all_claude_history_session_ids,
    read_claude_history_session_ids,
    watch_agent_tool_events,
)
from app.services.runtime.protocol import AgentMessage

CLIENT_ID = UUID("12345678-1234-5678-1234-567812345678")
WINDOW_ID = UUID("87654321-4321-8765-4321-876543218765")


def test_agent_tool_collectors_are_centralized_by_provider() -> None:
    assert AGENT_TOOL_COLLECTORS == (
        ("codex", "collect_codex_watch_events"),
        ("claude_code", "collect_claude_code_watch_events"),
        ("cursor_cli", "collect_cursor_watch_events"),
        ("antigravity_cli", "collect_antigravity_watch_events"),
    )


def test_collect_all_events_can_filter_to_declared_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def collect(state, *, client_id, window_id, project_path):
        called.append("cursor_cli")
        return []

    monkeypatch.setattr("app.client_agent.agent_tool_watchers.collect_cursor_watch_events", collect)

    _collect_all_events(
        AgentToolWatcherState(),
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
        providers=frozenset({"cursor_cli"}),
    )

    assert called == ["cursor_cli"]


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


def append_cursor_blob(path: Path, blob_id: str, role: str, text: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "insert into blobs (id, data) values (?, ?)",
        (blob_id, json.dumps({"role": role, "content": text}).encode("utf-8")),
    )
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


def test_initialize_agent_tool_watcher_state_starts_stale_jsonl_collectors_at_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_session = tmp_path / "rollout-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    codex_session.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "old codex"}],
                },
                "timestamp": "2026-05-23T00:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    claude_session = tmp_path / "claude-session.jsonl"
    claude_session.write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "old claude"}}) + "\n",
        encoding="utf-8",
    )
    now = 1_780_000_000.0
    old = now - watchers.CODEX_ACTIVE_SESSION_BOOTSTRAP_SECONDS - 1
    os.utime(codex_session, (old, old))
    monkeypatch.setattr(watchers.time, "time", lambda: now)
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.iter_codex_session_files",
        lambda window_id: [codex_session],
    )
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.iter_claude_code_jsonl_files",
        lambda window_id: [claude_session],
    )
    state = AgentToolWatcherState()

    initialize_agent_tool_watcher_state(state, window_id=WINDOW_ID)

    assert collect_codex_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    ) == []
    assert collect_claude_code_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    ) == []

    codex_new_offset = codex_session.stat().st_size
    codex_session.write_text(
        codex_session.read_text(encoding="utf-8")
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "new codex"}],
                },
                "timestamp": "2026-05-23T00:00:01Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    claude_new_offset = claude_session.stat().st_size
    claude_session.write_text(
        claude_session.read_text(encoding="utf-8")
        + json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "new claude"}})
        + "\n",
        encoding="utf-8",
    )

    codex_events = collect_codex_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )
    claude_events = collect_claude_code_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert [event.offset for event in codex_events] == [codex_new_offset]
    assert codex_events[0].payload["payload"]["content"][0]["text"] == "new codex"
    assert [event.offset for event in claude_events] == [claude_new_offset]
    assert claude_events[0].payload["message"]["content"] == "new claude"


def test_initialize_agent_tool_watcher_state_bootstraps_latest_recent_codex_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_session = tmp_path / "rollout-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    active_session = tmp_path / "rollout-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl"
    stale_session.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "old skipped"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    active_session.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "bootstrap me"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    now = 1_780_000_000.0
    old = now - watchers.CODEX_ACTIVE_SESSION_BOOTSTRAP_SECONDS - 1
    os.utime(stale_session, (old, old))
    os.utime(active_session, (now - 1, now - 1))
    monkeypatch.setattr(watchers.time, "time", lambda: now)
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.iter_codex_session_files",
        lambda window_id: [stale_session, active_session],
    )
    state = AgentToolWatcherState()

    initialize_agent_tool_watcher_state(state, window_id=WINDOW_ID)
    events = collect_codex_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert [event.offset for event in events] == [0]
    assert events[0].source_path == str(active_session)
    assert events[0].payload["payload"]["content"][0]["text"] == "bootstrap me"
    assert state.codex_offsets[stale_session] == stale_session.stat().st_size


def test_initialize_agent_tool_watcher_state_resumes_stale_codex_session_at_complete_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_file = tmp_path / "rollout-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    first_line = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "complete"}],
            },
        }
    )
    session_file.write_text(first_line + "\n" + '{"type":"response_item"', encoding="utf-8")
    now = 1_780_000_000.0
    old = now - watchers.CODEX_ACTIVE_SESSION_BOOTSTRAP_SECONDS - 1
    os.utime(session_file, (old, old))
    monkeypatch.setattr(watchers.time, "time", lambda: now)
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.iter_codex_session_files",
        lambda window_id: [session_file],
    )
    state = AgentToolWatcherState()

    initialize_agent_tool_watcher_state(state, window_id=WINDOW_ID)

    assert state.codex_offsets[session_file] == len((first_line + "\n").encode("utf-8"))


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


def test_collect_codex_watch_events_hardlinks_session_to_global_resume_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    session_file = (
        home
        / ".web-terminal-acp"
        / "codex-homes"
        / str(WINDOW_ID)
        / "sessions"
        / "2026"
        / "06"
        / "01"
        / "rollout-2026-06-01T00-00-00-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    )
    session_file.parent.mkdir(parents=True)
    session_file.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "global codex"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()

    events = collect_codex_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    global_session_file = (
        home
        / ".codex"
        / "sessions"
        / "2026"
        / "06"
        / "01"
        / "rollout-2026-06-01T00-00-00-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    )
    assert len(events) == 1
    assert global_session_file.samefile(session_file)


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


def test_collect_claude_code_watch_events_hardlinks_transcript_to_global_resume_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    session_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    transcript_file = (
        home
        / ".web-terminal-acp"
        / "claude-code-homes"
        / str(WINDOW_ID)
        / "projects"
        / "-workspace-project"
        / f"{session_id}.jsonl"
    )
    transcript_file.parent.mkdir(parents=True)
    transcript_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "message": {"role": "assistant", "content": "global claude"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()

    events = collect_claude_code_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    global_transcript_file = home / ".claude" / "projects" / "-workspace-project" / f"{session_id}.jsonl"
    assert len(events) == 1
    assert global_transcript_file.samefile(transcript_file)


def test_collect_claude_code_watch_events_enriches_subagent_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    transcript_dir = (
        home
        / ".web-terminal-acp"
        / "claude-code-homes"
        / str(WINDOW_ID)
        / "projects"
        / "-workspace-project"
    )
    main_file = transcript_dir / f"{session_id}.jsonl"
    subagent_file = transcript_dir / "subagents" / "agent-subagent-1.jsonl"
    subagent_meta = transcript_dir / "subagents" / "agent-subagent-1.meta.json"
    subagent_file.parent.mkdir(parents=True)
    main_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-subagent-1",
                            "name": "Agent",
                            "input": {"description": "Return one", "prompt": "Return exactly: 1"},
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    subagent_file.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": session_id,
                "agentId": "subagent-1",
                "isSidechain": True,
                "message": {"role": "user", "content": "Return exactly: 1"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    subagent_meta.write_text(
        json.dumps({"agentType": "claude", "description": "Return one", "toolUseId": "call-subagent-1"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()

    events = collect_claude_code_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert len(events) == 2
    main_event = next(event for event in events if event.source_path == str(main_file))
    subagent_event = next(event for event in events if event.source_path == str(subagent_file))
    assert main_event.payload["subagent_tool_use_results"] == [
        {
            "agent_id": "subagent-1",
            "tool_use_id": "call-subagent-1",
            "source_path": str(subagent_file),
        }
    ]
    assert subagent_event.payload["subagent"]["toolUseId"] == "call-subagent-1"
    assert subagent_event.payload["agentId"] == "subagent-1"
    assert subagent_event.payload["isSidechain"] is True


def test_read_claude_history_session_ids_extracts_sessions_and_tracks_offset(tmp_path: Path) -> None:
    history_file = tmp_path / "history.jsonl"
    history_file.write_text(
        json.dumps({"display": "hi", "sessionId": "claude-session-1"}) + "\n"
        + json.dumps({"display": "missing session"}) + "\n",
        encoding="utf-8",
    )

    session_ids, offset = read_claude_history_session_ids(history_file, 0)
    second_session_ids, second_offset = read_claude_history_session_ids(history_file, offset)

    assert session_ids == {"claude-session-1"}
    assert offset == history_file.stat().st_size
    assert second_session_ids == set()
    assert second_offset == offset


def test_read_all_claude_history_session_ids_reads_past_default_batch_limit(tmp_path: Path) -> None:
    history_file = tmp_path / "history.jsonl"
    history_file.write_text(
        "".join(
            json.dumps({"display": f"prompt {index}", "sessionId": f"claude-session-{index}"}) + "\n"
            for index in range(125)
        ),
        encoding="utf-8",
    )

    session_ids = read_all_claude_history_session_ids(history_file)

    assert len(session_ids) == 125
    assert "claude-session-0" in session_ids
    assert "claude-session-124" in session_ids


def test_collect_claude_code_watch_events_maps_history_session_to_managed_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    session_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    history_file = home / ".web-terminal-acp" / "claude-code-homes" / str(WINDOW_ID) / "history.jsonl"
    transcript_file = (
        home
        / ".web-terminal-acp"
        / "claude-code-homes"
        / str(WINDOW_ID)
        / "projects"
        / "-workspace-project"
        / f"{session_id}.jsonl"
    )
    history_file.parent.mkdir(parents=True)
    transcript_file.parent.mkdir(parents=True)
    history_file.write_text(
        json.dumps({"display": "fix bug", "sessionId": session_id}) + "\n",
        encoding="utf-8",
    )
    transcript_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "mapped transcript"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()

    events = collect_claude_code_watch_events(
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

    assert len(events) == 1
    event = events[0]
    assert event.provider == "claude_code"
    assert event.source_path == str(transcript_file)
    assert event.offset == 0
    assert event.cursor == 0
    assert event.payload["sessionId"] == session_id
    assert event.payload["message"]["content"][0]["text"] == "mapped transcript"
    assert event.payload["WEB_TERMINAL_CLIENT_ID"] == str(CLIENT_ID)
    assert event.payload["WEB_TERMINAL_WINDOW_ID"] == str(WINDOW_ID)
    assert event.payload["WEB_TERMINAL_PROJECT_PATH"] == "/workspace/project"
    assert second == []
    assert state.claude_code_history_session_ids == {session_id}
    assert state.claude_code_history_jsonl_files == {transcript_file}
    assert state.claude_code_offsets[transcript_file] == transcript_file.stat().st_size


def test_initialize_agent_tool_watcher_state_starts_linked_claude_history_at_eof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    session_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    history_file = home / ".web-terminal-acp" / "claude-code-homes" / str(WINDOW_ID) / "history.jsonl"
    transcript_file = (
        home
        / ".web-terminal-acp"
        / "claude-code-homes"
        / str(WINDOW_ID)
        / "projects"
        / "-workspace-project"
        / f"{session_id}.jsonl"
    )
    history_file.parent.mkdir(parents=True)
    transcript_file.parent.mkdir(parents=True)
    history_file.write_text(
        json.dumps({"display": "resume", "sessionId": session_id}) + "\n",
        encoding="utf-8",
    )
    transcript_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "message": {"role": "assistant", "content": "old transcript"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()

    initialize_agent_tool_watcher_state(state, window_id=WINDOW_ID)

    assert state.claude_code_history_offset == history_file.stat().st_size
    assert state.claude_code_history_session_ids == set()
    assert state.claude_code_pending_history_session_ids == set()
    assert state.claude_code_history_jsonl_files == set()
    assert collect_claude_code_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    ) == []

    new_history_offset = history_file.stat().st_size
    new_transcript_offset = transcript_file.stat().st_size
    history_file.write_text(
        history_file.read_text(encoding="utf-8")
        + json.dumps({"display": "continue", "sessionId": session_id})
        + "\n",
        encoding="utf-8",
    )
    transcript_file.write_text(
        transcript_file.read_text(encoding="utf-8")
        + json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "message": {"role": "assistant", "content": "new transcript"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    events = collect_claude_code_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert [event.offset for event in events] == [new_transcript_offset]
    assert events[0].payload["message"]["content"] == "new transcript"
    assert state.claude_code_history_offset > new_history_offset
    assert state.claude_code_history_session_ids == {session_id}


def test_collect_codex_watch_events_reuses_discovered_paths_until_refresh(
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
    calls: list[UUID] = []

    def discover(window_id: UUID) -> list[Path]:
        calls.append(window_id)
        return [session_file]

    monkeypatch.setattr("app.client_agent.agent_tool_watchers.iter_codex_session_files", discover)
    state = AgentToolWatcherState()

    collect_codex_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )
    collect_codex_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert calls == [WINDOW_ID]


def test_collect_codex_watch_events_caches_empty_discovery_results(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[UUID] = []

    def discover(window_id: UUID) -> list[Path]:
        calls.append(window_id)
        return []

    monkeypatch.setattr("app.client_agent.agent_tool_watchers.iter_codex_session_files", discover)
    state = AgentToolWatcherState()

    assert collect_codex_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    ) == []
    assert collect_codex_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    ) == []

    assert calls == [WINDOW_ID]


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


def test_initialize_agent_tool_watcher_state_starts_cursor_collector_after_existing_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    store = home / ".web-terminal-acp" / "cursor-homes" / str(WINDOW_ID) / "state" / "store.db"
    store.parent.mkdir(parents=True)
    write_cursor_store(store)
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()

    initialize_agent_tool_watcher_state(state, window_id=WINDOW_ID)

    assert collect_cursor_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    ) == []

    append_cursor_blob(store, "new-assistant-blob", "assistant", "new cursor")
    events = collect_cursor_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert [event.payload["blob_id"] for event in events] == ["new-assistant-blob"]
    assert events[0].payload["text"] == "new cursor"


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

    assert events == []
    assert state.cursor_store_paths == [store]
    assert state.cursor_last_rowids[store] == 3

    append_cursor_blob(store, "new-assistant-blob", "assistant", "new cursor")
    events = collect_cursor_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert [event.payload["blob_id"] for event in events] == ["new-assistant-blob"]
    assert events[0].payload["text"] == "new cursor"


@pytest.mark.asyncio
async def test_watch_agent_tool_events_notifies_idle_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_file = tmp_path / "rollout-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.iter_codex_session_files",
        lambda window_id: [session_file],
    )
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.initialize_agent_tool_watcher_state",
        lambda state, *, window_id: None,
    )

    supervisor = FakeIdleSupervisor()
    sent: list[object] = []

    async def send_event(message):
        sent.append(message)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await watch_agent_tool_events(
            send_event,
            CLIENT_ID,
            WINDOW_ID,
            "/workspace/project",
            idle_supervisor=supervisor,
        )

    assert len(supervisor.observed_batches) == 1
    assert supervisor.observed_batches[0][0].provider == "codex"


@pytest.mark.asyncio
async def test_watch_agent_tool_events_defers_idle_and_presence_checks_on_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.initialize_agent_tool_watcher_state",
        lambda state, *, window_id: None,
    )
    monkeypatch.setattr("app.client_agent.agent_tool_watchers._collect_all_events", lambda *args, **kwargs: [])
    presence_calls: list[UUID] = []

    async def detect_presence(window_id, *, terminal, runtime):
        presence_calls.append(window_id)
        return None

    async def send_presence(_message):
        raise AssertionError("presence should not be sent during the first watcher loop")

    async def stop_after_first_loop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.client_agent.agent_tool_watchers.detect_agent_work_presence", detect_presence)
    monkeypatch.setattr("app.client_agent.agent_tool_watchers.asyncio.sleep", stop_after_first_loop)

    supervisor = FakeIdleSupervisor()

    with pytest.raises(asyncio.CancelledError):
        await watch_agent_tool_events(
            lambda _message: asyncio.sleep(0),
            CLIENT_ID,
            WINDOW_ID,
            "/workspace/project",
            send_presence=send_presence,
            idle_supervisor=supervisor,
        )

    assert supervisor.checked_windows == []
    assert presence_calls == []


@pytest.mark.asyncio
async def test_watch_agent_tool_events_sends_presence_when_staggered_scan_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.initialize_agent_tool_watcher_state",
        lambda state, *, window_id: None,
    )
    monkeypatch.setattr("app.client_agent.agent_tool_watchers._collect_all_events", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.client_agent.agent_tool_watchers._initial_process_scan_delay", lambda window_id, interval: 0.0)

    class Signal:
        providers = ("codex",)
        reasons = ("process",)

    async def detect_presence(window_id, *, terminal, runtime):
        assert window_id == WINDOW_ID
        return Signal()

    sent_presence: list[AgentMessage] = []

    async def send_presence(message: AgentMessage):
        sent_presence.append(message)

    async def stop_after_first_loop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.client_agent.agent_tool_watchers.detect_agent_work_presence", detect_presence)
    monkeypatch.setattr("app.client_agent.agent_tool_watchers.asyncio.sleep", stop_after_first_loop)

    supervisor = FakeIdleSupervisor()

    with pytest.raises(asyncio.CancelledError):
        await watch_agent_tool_events(
            lambda _message: asyncio.sleep(0),
            CLIENT_ID,
            WINDOW_ID,
            "/workspace/project",
            send_presence=send_presence,
            idle_supervisor=supervisor,
        )

    assert supervisor.checked_windows == [WINDOW_ID]
    assert len(sent_presence) == 1
    assert sent_presence[0].type == "agent_work_presence"
    assert sent_presence[0].payload == {"providers": ["codex"], "reasons": ["process"]}


@pytest.mark.asyncio
async def test_watcher_scans_are_concurrency_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watchers, "AGENT_WATCH_COLLECTION_CONCURRENCY", 1)
    monkeypatch.setattr(watchers, "_WATCH_COLLECTION_SEMAPHORE", None)
    monkeypatch.setattr(watchers, "_WATCH_COLLECTION_SEMAPHORE_LOOP", None)
    entered: list[str] = []
    first_entered = threading.Event()
    release_first = threading.Event()

    def blocking_scan(name: str) -> str:
        entered.append(name)
        if name == "first":
            first_entered.set()
            release_first.wait(timeout=2)
        return name

    first = asyncio.create_task(watchers._run_watcher_scan(blocking_scan, "first"))
    assert await asyncio.to_thread(first_entered.wait, 1)
    second = asyncio.create_task(watchers._run_watcher_scan(blocking_scan, "second"))

    await asyncio.sleep(0.05)
    assert entered == ["first"]

    release_first.set()
    assert await first == "first"
    assert await second == "second"
    assert entered == ["first", "second"]


@pytest.mark.asyncio
async def test_unified_agent_tool_watcher_manages_multiple_windows_with_one_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.initialize_agent_tool_watcher_state",
        lambda state, *, window_id: None,
    )
    monkeypatch.setattr("app.client_agent.agent_tool_watchers._collect_all_events", lambda *args, **kwargs: [])

    scanned_windows: list[UUID] = []
    original_scan_window = UnifiedAgentToolWatcher._scan_window

    async def scan_once(self, window):
        scanned_windows.append(window.window_id)
        if len(scanned_windows) >= 2:
            raise asyncio.CancelledError
        await original_scan_window(self, window)

    monkeypatch.setattr(UnifiedAgentToolWatcher, "_scan_window", scan_once)

    watcher = UnifiedAgentToolWatcher(lambda _message: asyncio.sleep(0), CLIENT_ID)
    watcher.start()
    first_task = watcher._task
    other_window_id = UUID("11111111-2222-3333-4444-555555555555")
    watcher.watch_window(WINDOW_ID, "/workspace/one")
    watcher.watch_window(other_window_id, "/workspace/two")
    assert watcher._task is first_task

    with pytest.raises(asyncio.CancelledError):
        await first_task

    assert scanned_windows == [WINDOW_ID, other_window_id]


@pytest.mark.asyncio
async def test_unified_agent_tool_watcher_sends_presence_before_agent_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.client_agent.agent_tool_watchers.initialize_agent_tool_watcher_state",
        lambda state, *, window_id: None,
    )
    monkeypatch.setattr("app.client_agent.agent_tool_watchers._initial_process_scan_delay", lambda window_id, interval: 0.0)

    from app.client_agent.ai_events import ManagedAiEvent

    event = ManagedAiEvent(
        provider="cursor_cli",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        source_path="/tmp/store.db",
        offset=None,
        cursor="root-1",
        project_path="/workspace/project",
        payload={
            "client_id": str(CLIENT_ID),
            "virtual_window_id": str(WINDOW_ID),
            "agentId": "cursor-agent-1",
            "blob_id": "assistant-blob",
            "role": "assistant",
            "text": "hello",
        },
    )
    monkeypatch.setattr("app.client_agent.agent_tool_watchers._collect_all_events", lambda *args, **kwargs: [event])

    class Signal:
        providers = ("cursor_cli",)
        reasons = ("process",)

    async def detect_presence(window_id, *, terminal, runtime):
        return Signal()

    monkeypatch.setattr("app.client_agent.agent_tool_watchers.detect_agent_work_presence", detect_presence)

    sent: list[AgentMessage] = []

    async def send(message: AgentMessage):
        sent.append(message)
        if len(sent) == 2:
            raise asyncio.CancelledError

    watcher = UnifiedAgentToolWatcher(send, CLIENT_ID, send_presence=send)
    watcher.watch_window(WINDOW_ID, "/workspace/project")

    with pytest.raises(asyncio.CancelledError):
        await watcher._run()

    assert [message.type for message in sent] == ["agent_work_presence", "ai_event"]


@pytest.mark.asyncio
async def test_unified_agent_tool_watcher_keeps_running_after_window_scan_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.client_agent.agent_tool_watchers.AGENT_WATCH_IDLE_INTERVAL_SECONDS", 0.01)
    scanned_windows: list[UUID] = []
    other_window_id = UUID("11111111-2222-3333-4444-555555555555")

    async def scan_window(self, window):
        scanned_windows.append(window.window_id)
        if window.window_id == WINDOW_ID:
            raise RuntimeError("boom")
        raise asyncio.CancelledError

    monkeypatch.setattr(UnifiedAgentToolWatcher, "_scan_window", scan_window)
    monkeypatch.setattr("app.client_agent.agent_tool_watchers.time.perf_counter", lambda: 100.0)

    watcher = UnifiedAgentToolWatcher(lambda _message: asyncio.sleep(0), CLIENT_ID)
    watcher.watch_window(WINDOW_ID, "/workspace/one")
    watcher.watch_window(other_window_id, "/workspace/two")

    with pytest.raises(asyncio.CancelledError):
        await watcher._run()

    assert scanned_windows == [WINDOW_ID, other_window_id]


class FakeIdleSupervisor:
    def __init__(self) -> None:
        self.observed_batches: list[list[object]] = []
        self.checked_windows: list[UUID] = []

    async def observe_events(self, events):
        self.observed_batches.append(events)

    async def maybe_suspend_window(self, window_id: UUID) -> None:
        self.checked_windows.append(window_id)


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


def test_cursor_store_paths_for_window_ignores_linked_chats_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    source_chats = home / ".cursor" / "chats"
    store = source_chats / "workspace-hash" / "session-id" / "store.db"
    managed_chats = home / ".web-terminal-acp" / "cursor-homes" / str(WINDOW_ID) / "chats"
    store.parent.mkdir(parents=True)
    write_cursor_store(store)
    managed_chats.parent.mkdir(parents=True)
    managed_chats.symlink_to(source_chats)
    monkeypatch.setattr(Path, "home", lambda: home)

    assert watchers.cursor_store_paths_for_window(WINDOW_ID) == []


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
    assert second == []
    assert state.cursor_store_paths == [first_store, second_store]

    append_cursor_blob(second_store, "new-assistant-blob", "assistant", "new cursor")
    third = collect_cursor_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert [event.source_path for event in third] == [str(second_store)]
    assert [event.payload["blob_id"] for event in third] == ["new-assistant-blob"]


def test_collect_antigravity_watch_events_reads_managed_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    session_id = "antigravity-session-1"
    transcript = (
        home
        / ".web-terminal-acp"
        / "antigravity-cli-homes"
        / str(WINDOW_ID)
        / "brain"
        / session_id
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": "done",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()

    initialize_agent_tool_watcher_state(state, window_id=WINDOW_ID)
    bootstrapped = watchers.collect_antigravity_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert len(bootstrapped) == 1
    assert bootstrapped[0].payload["content"] == "done"

    offset = transcript.stat().st_size
    transcript.write_text(
        transcript.read_text(encoding="utf-8")
        + json.dumps(
            {
                "step_index": 2,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "content": "fix tests",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    events = watchers.collect_antigravity_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )

    assert len(events) == 1
    event = events[0]
    assert event.provider == "antigravity_cli"
    assert event.source_path == str(transcript)
    assert event.offset == offset
    assert event.cursor == offset
    assert event.project_path == "/workspace/project"
    assert event.payload["session_id"] == session_id
    assert event.payload["WEB_TERMINAL_CLIENT_ID"] == str(CLIENT_ID)
    assert event.payload["WEB_TERMINAL_WINDOW_ID"] == str(WINDOW_ID)
    assert event.payload["WEB_TERMINAL_PROJECT_PATH"] == "/workspace/project"


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


def test_collect_antigravity_watch_events_with_subagent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    main_session_id = "main-session-1"
    sub_session_id = "subagent-1"
    
    # 1. Create main session transcript
    main_transcript = (
        home
        / ".web-terminal-acp"
        / "antigravity-cli-homes"
        / str(WINDOW_ID)
        / "brain"
        / main_session_id
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    main_transcript.parent.mkdir(parents=True)
    
    # Tool call to invoke_subagent
    step_2 = {
        "step_index": 2,
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "tool_calls": [
            {
                "name": "invoke_subagent",
                "args": {
                    "Subagents": '[{"Prompt": "Return exactly: 1", "Role": "Greeter Agent", "TypeName": "self"}]'
                }
            }
        ]
    }
    
    # Tool result INVOKE_SUBAGENT
    step_3 = {
        "step_index": 3,
        "source": "MODEL",
        "type": "INVOKE_SUBAGENT",
        "status": "DONE",
        "content": f'Created the following subagents:\n{{\n  "conversationId": "{sub_session_id}",\n  "workspaceUris": []\n}}',
    }

    step_4 = {
        "step_index": 4,
        "source": "SYSTEM",
        "type": "SYSTEM_MESSAGE",
        "status": "DONE",
        "content": (
            "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n"
            "<SYSTEM_MESSAGE>\n"
            f"[Message] timestamp=2026-06-02T05:52:12Z sender={sub_session_id} "
            "priority=MESSAGE_PRIORITY_HIGH content=1\n"
            "</SYSTEM_MESSAGE>"
        ),
    }
    
    main_transcript.write_text(
        json.dumps(step_2) + "\n" + json.dumps(step_3) + "\n" + json.dumps(step_4) + "\n",
        encoding="utf-8",
    )
    
    # 2. Create subagent session transcript
    sub_transcript = (
        home
        / ".web-terminal-acp"
        / "antigravity-cli-homes"
        / str(WINDOW_ID)
        / "brain"
        / sub_session_id
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    sub_transcript.parent.mkdir(parents=True)
    
    sub_step_1 = {
        "step_index": 1,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "content": "Return exactly: 1",
    }
    
    sub_transcript.write_text(
        json.dumps(sub_step_1) + "\n",
        encoding="utf-8",
    )
    
    monkeypatch.setattr(Path, "home", lambda: home)
    state = AgentToolWatcherState()
    
    initialize_agent_tool_watcher_state(state, window_id=WINDOW_ID)
    state.antigravity_offsets[main_transcript] = 0
    state.antigravity_offsets[sub_transcript] = 0
    bootstrapped = watchers.collect_antigravity_watch_events(
        state,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        project_path="/workspace/project",
    )
    
    # Check that events from both main session and sub session were collected
    assert len(bootstrapped) == 4
    
    # Group events by source_path/session_id
    main_events = sorted([e for e in bootstrapped if "main-session-1" in e.source_path and "subagents" not in e.source_path], key=lambda x: x.payload["step_index"])
    sub_events = [e for e in bootstrapped if "subagents" in e.source_path]
    
    assert len(main_events) == 3
    assert len(sub_events) == 1
    
    # Check tool call matches got attached to main_events[0] (step 2)
    assert main_events[0].payload["subagent_tool_use_results"] == [
        {"tool_use_id": "step-2", "agent_id": sub_session_id}
    ]
    
    # Check tool use result got attached to main_events[1] (step 3)
    assert main_events[1].payload["toolUseResult"] == {
        "agentId": sub_session_id,
        "toolUseId": "step-2",
    }
    assert main_events[2].payload["toolUseResult"] == {
        "agentId": sub_session_id,
        "toolUseId": "step-2",
    }
    
    # Check subagent metadata got attached to sub_events[0]
    payload = sub_events[0].payload
    assert sub_events[0].source_path == f"/tmp/{main_session_id}/subagents/agent-{sub_session_id}.jsonl"
    assert payload["session_id"] == f"agent-{sub_session_id}"
    assert payload["isSidechain"] is True
    assert payload["agentId"] == sub_session_id
    assert payload["sessionId"] == main_session_id
    assert payload["subagent"] == {
        "toolUseId": "step-2",
        "tool_use_id": "step-2",
        "agentId": sub_session_id,
        "agent_id": sub_session_id,
    }
