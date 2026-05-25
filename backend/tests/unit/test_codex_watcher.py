import json
from uuid import UUID

from app.client_agent.codex_watcher import read_new_codex_events


CLIENT_ID = UUID("12345678-1234-5678-1234-567812345678")
WINDOW_ID = UUID("87654321-4321-8765-4321-876543218765")


def test_read_new_codex_events_attributes_session_lines_to_window(tmp_path) -> None:
    session_path = tmp_path / "rollout-2026-05-21T00-00-00-session-1.jsonl"
    session_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-21T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "session-1", "cwd": "/tmp"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "timestamp": "2026-05-21T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "token_count"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    events, next_offset = read_new_codex_events(
        session_path,
        0,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
    )

    assert next_offset == session_path.stat().st_size
    assert len(events) == 1
    payload, line_offset = events[0]
    assert line_offset == 0
    assert payload["trace_id"] == "session-1"
    assert payload["id"] == "session-1:0"
    assert payload["name"] == "session_meta"
    assert payload["client_id"] == str(CLIENT_ID)
    assert payload["virtual_window_id"] == str(WINDOW_ID)
    assert payload["source_path"] == str(session_path)


def test_read_new_codex_events_keeps_partial_line_unconsumed(tmp_path) -> None:
    session_path = tmp_path / "rollout-session-2.jsonl"
    complete = json.dumps({"type": "response_item", "payload": {"type": "message"}}) + "\n"
    partial = json.dumps({"type": "response_item", "payload": {"type": "reasoning"}})
    session_path.write_text(complete + partial, encoding="utf-8")

    events, next_offset = read_new_codex_events(
        session_path,
        0,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
    )

    assert len(events) == 1
    assert events[0][0]["trace_id"] == "session-2"
    assert next_offset == len(complete.encode("utf-8"))


def test_read_new_codex_events_uses_trailing_rollout_uuid_as_session_id(tmp_path) -> None:
    session_path = tmp_path / "rollout-2026-05-21T17-38-25-019e4b9d-fdd5-7b50-956a-a0a17cdd4963.jsonl"
    session_path.write_text(
        json.dumps({"type": "response_item", "payload": {"type": "message"}}) + "\n",
        encoding="utf-8",
    )

    events, _next_offset = read_new_codex_events(
        session_path,
        0,
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
    )

    assert events[0][0]["trace_id"] == "019e4b9d-fdd5-7b50-956a-a0a17cdd4963"
