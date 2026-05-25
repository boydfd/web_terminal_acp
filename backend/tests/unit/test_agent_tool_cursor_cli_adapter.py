import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from app.agent_tools.adapters.cursor_cli import CursorCliAdapter
from app.client_agent.cursor_watcher import read_cursor_store_events
from app.models import Event, EventSourceType


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
            json.dumps({"role": "user", "content": [{"type": "text", "text": "hi"}]}).encode(
                "utf-8"
            ),
        ),
    )
    conn.execute(
        "insert into blobs (id, data) values (?, ?)",
        ("assistant-blob", json.dumps({"role": "assistant", "content": "hello"}).encode("utf-8")),
    )
    conn.execute("insert into blobs (id, data) values (?, ?)", ("binary-blob", b"\x0a\x02hi"))
    conn.commit()
    conn.close()


def test_cursor_store_reads_json_message_blobs_and_skips_binary(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    write_cursor_store(store)

    events, cursor, _max_rowid = read_cursor_store_events(store, seen_blob_ids=set())

    assert cursor == "root-1"
    assert [(event["role"], event["text"], event["blob_id"]) for event in events] == [
        ("user", "hi", "user-blob"),
        ("assistant", "hello", "assistant-blob"),
    ]
    assert all(event["agentId"] == "cursor-agent-1" for event in events)


def test_cursor_cli_storage_uses_managed_per_window_home() -> None:
    storage = CursorCliAdapter().prepare_storage("window-1")

    assert storage.env == {
        "CURSOR_AGENT_HOME": "~/.web-terminal-acp/cursor-homes/window-1",
        "CURSOR_CONFIG_DIR": "~/.web-terminal-acp/cursor-homes/window-1",
        "CURSOR_DATA_DIR": "~/.web-terminal-acp/cursor-homes/window-1",
    }
    assert [str(path) for path in storage.directories] == ["~/.web-terminal-acp/cursor-homes/window-1"]


def test_cursor_store_honors_seen_blob_ids(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    write_cursor_store(store)

    events, cursor, _max_rowid = read_cursor_store_events(store, seen_blob_ids={"user-blob"})

    assert cursor == "root-1"
    assert [event["blob_id"] for event in events] == ["assistant-blob"]


def test_cursor_store_returns_empty_for_missing_tables(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    conn = sqlite3.connect(store)
    conn.close()

    events, cursor, _max_rowid = read_cursor_store_events(store, seen_blob_ids=set())

    assert events == []
    assert cursor is None


def test_cursor_store_parses_blobs_with_invalid_meta(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    conn = sqlite3.connect(store)
    conn.execute("create table meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("create table blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn.execute("insert into meta (key, value) values (?, ?)", ("0", "not-hex-json"))
    conn.execute(
        "insert into blobs (id, data) values (?, ?)",
        ("user-blob", json.dumps({"role": "user", "content": "hi"}).encode("utf-8")),
    )
    conn.commit()
    conn.close()

    events, cursor, _max_rowid = read_cursor_store_events(store, seen_blob_ids=set())

    assert cursor is None
    assert [(event["role"], event["text"], event["blob_id"]) for event in events] == [
        ("user", "hi", "user-blob")
    ]


def test_cursor_store_parses_blobs_with_null_meta(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    conn = sqlite3.connect(store)
    conn.execute("create table meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("create table blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn.execute("insert into meta (key, value) values (?, ?)", ("0", None))
    conn.execute(
        "insert into blobs (id, data) values (?, ?)",
        ("assistant-blob", json.dumps({"role": "assistant", "content": "hello"}).encode("utf-8")),
    )
    conn.commit()
    conn.close()

    events, cursor, _max_rowid = read_cursor_store_events(store, seen_blob_ids=set())

    assert cursor is None
    assert [(event["role"], event["text"], event["blob_id"]) for event in events] == [
        ("assistant", "hello", "assistant-blob")
    ]


def test_cursor_store_parses_blobs_with_unsupported_binary_meta(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    conn = sqlite3.connect(store)
    conn.execute("create table meta (key TEXT PRIMARY KEY, value BLOB)")
    conn.execute("create table blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn.execute("insert into meta (key, value) values (?, ?)", ("0", b"\xff"))
    conn.execute(
        "insert into blobs (id, data) values (?, ?)",
        ("user-blob", json.dumps({"role": "user", "content": "hi"}).encode("utf-8")),
    )
    conn.commit()
    conn.close()

    events, cursor, _max_rowid = read_cursor_store_events(store, seen_blob_ids=set())

    assert cursor is None
    assert [(event["role"], event["text"], event["blob_id"]) for event in events] == [
        ("user", "hi", "user-blob")
    ]


def test_cursor_normalize_uses_generic_source_type_and_stable_fingerprint() -> None:
    payload = {"agentId": "cursor-agent-1", "blob_id": "user-blob", "role": "user", "text": "hi"}

    event = CursorCliAdapter().normalize(payload, source_path="/tmp/store.db", cursor="root-1")

    assert event.source_type == EventSourceType.agent_tool_record
    assert event.source_id == "cursor-agent-1"
    assert event.kind == "user_message"
    assert event.text == "hi"
    assert event.payload_json["provider"] == "cursor_cli"
    assert event.fingerprint.startswith("agent_tool_record:cursor_cli:")


def test_cursor_normalize_fingerprint_excludes_cursor_and_scopes_source_blob() -> None:
    payload = {"agentId": "cursor-agent-1", "blob_id": "user-blob", "role": "user", "text": "hi"}
    adapter = CursorCliAdapter()

    first = adapter.normalize(payload, source_path="/tmp/store.db", cursor="root-1")
    different_cursor = adapter.normalize(payload, source_path="/tmp/store.db", cursor="root-2")
    different_source = adapter.normalize(payload, source_path="/tmp/other.db", cursor="root-1")
    different_blob = adapter.normalize({**payload, "blob_id": "other-blob"}, source_path="/tmp/store.db", cursor="root-1")
    different_root = adapter.normalize({**payload, "root_blob_id": "root-2"}, source_path="/tmp/store.db", cursor="root-1")
    different_text = adapter.normalize({**payload, "text": "bye"}, source_path="/tmp/store.db", cursor="root-1")
    missing_source = adapter.normalize(payload, source_path=None, cursor="root-1")
    missing_source_different_cursor = adapter.normalize(payload, source_path=None, cursor="root-2")

    assert different_cursor.fingerprint == first.fingerprint
    assert different_root.fingerprint == first.fingerprint
    assert different_text.fingerprint != first.fingerprint
    assert different_source.fingerprint != first.fingerprint
    assert different_blob.fingerprint != first.fingerprint
    assert missing_source_different_cursor.fingerprint == missing_source.fingerprint
    assert missing_source.fingerprint != first.fingerprint


def test_cursor_projects_chat_and_detail() -> None:
    row = Event(
        client_id=uuid4(),
        source_type=EventSourceType.agent_tool_record,
        source_id="cursor-agent-1",
        kind="assistant_message",
        payload_json={"provider": "cursor_cli", "role": "assistant", "text": "hello"},
        fingerprint=str(uuid4()),
    )

    adapter = CursorCliAdapter()
    chat = adapter.project_chat(row)
    detail = adapter.project_event(row)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.body == "hello"
    assert detail.tone == "agent"
    assert detail.body == "hello"


def test_cursor_store_incremental_read_uses_rowid_cursor(tmp_path: Path) -> None:
    store = tmp_path / "store.db"
    write_cursor_store(store)

    first, cursor, max_rowid = read_cursor_store_events(store, seen_blob_ids=set())
    second, _, _ = read_cursor_store_events(store, seen_blob_ids={"user-blob", "assistant-blob"}, after_rowid=max_rowid)

    assert cursor == "root-1"
    assert len(first) == 2
    assert second == []


def test_cursor_summary_and_index_text_use_message_text() -> None:
    row = Event(
        client_id=uuid4(),
        source_type=EventSourceType.agent_tool_record,
        source_id="cursor-agent-1",
        kind="user_message",
        payload_json={"provider": "cursor_cli", "role": "user", "content": [{"type": "text", "text": "search docs"}]},
        fingerprint=str(uuid4()),
    )

    adapter = CursorCliAdapter()

    assert adapter.summary_text(row) == "search docs"
    assert adapter.index_text(row) == "search docs"
