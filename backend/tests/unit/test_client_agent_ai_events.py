import json
from uuid import UUID, uuid4

import pytest

from app.client_agent.ai_events import ManagedAiEvent, managed_event_from_payload
from app.client_agent.agent_tool_watchers import enqueue_managed_ai_event, read_new_jsonl_events
from app.services.runtime.protocol import AgentMessage


CLIENT_ID = UUID("12345678-1234-5678-1234-567812345678")
WINDOW_ID = UUID("87654321-4321-8765-4321-876543218765")


def test_managed_event_from_payload_accepts_managed_environment_attribution() -> None:
    payload = {
        "type": "assistant",
        "message": {"content": "hello"},
        "WEB_TERMINAL_CLIENT_ID": str(CLIENT_ID),
        "WEB_TERMINAL_WINDOW_ID": str(WINDOW_ID),
    }

    event = managed_event_from_payload(
        CLIENT_ID,
        WINDOW_ID,
        "claude",
        payload,
        source_path="/home/user/.claude/projects/session.jsonl",
        offset=42,
    )

    assert event is not None
    assert event.provider == "claude"
    assert event.client_id == CLIENT_ID
    assert event.window_id == WINDOW_ID
    assert event.source_path == "/home/user/.claude/projects/session.jsonl"
    assert event.offset == 42
    assert event.payload == payload


def test_managed_event_from_payload_accepts_client_and_virtual_window_fields() -> None:
    payload = {
        "trace_id": "trace-1",
        "client_id": str(CLIENT_ID),
        "virtual_window_id": str(WINDOW_ID),
    }

    event = managed_event_from_payload(CLIENT_ID, WINDOW_ID, "codex", payload)

    assert event is not None
    assert event.provider == "codex"
    assert event.payload == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "assistant", "WEB_TERMINAL_WINDOW_ID": str(WINDOW_ID)},
        {"type": "assistant", "WEB_TERMINAL_CLIENT_ID": str(CLIENT_ID)},
        {
            "type": "assistant",
            "WEB_TERMINAL_CLIENT_ID": str(uuid4()),
            "WEB_TERMINAL_WINDOW_ID": str(WINDOW_ID),
        },
        {
            "type": "assistant",
            "WEB_TERMINAL_CLIENT_ID": str(CLIENT_ID),
            "WEB_TERMINAL_WINDOW_ID": str(uuid4()),
        },
        {
            "type": "assistant",
            "client_id": "not-a-uuid",
            "virtual_window_id": str(WINDOW_ID),
        },
    ],
)
def test_managed_event_from_payload_rejects_unattributed_or_mismatched_payloads(payload) -> None:
    assert managed_event_from_payload(CLIENT_ID, WINDOW_ID, "claude", payload) is None


@pytest.mark.asyncio
async def test_enqueue_managed_ai_event_sends_attributed_agent_message() -> None:
    messages: list[AgentMessage] = []

    async def send_message(message: AgentMessage) -> None:
        messages.append(message)

    payload = {
        "type": "assistant",
        "message": {"content": "hello"},
        "WEB_TERMINAL_CLIENT_ID": str(CLIENT_ID),
        "WEB_TERMINAL_WINDOW_ID": str(WINDOW_ID),
    }

    sent = await enqueue_managed_ai_event(
        send_message,
        ManagedAiEvent(
            provider="claude",
            client_id=CLIENT_ID,
            window_id=WINDOW_ID,
            source_path="/tmp/session.jsonl",
            offset=7,
            cursor=9,
            project_path="/workspace/project",
            payload=payload,
        ),
    )

    assert sent is True
    assert len(messages) == 1
    message = messages[0]
    assert message.type == "ai_event"
    assert message.client_id == CLIENT_ID
    assert message.window_id == WINDOW_ID
    assert message.payload == {
        "provider": "claude",
        "source_path": "/tmp/session.jsonl",
        "offset": 7,
        "cursor": 9,
        "project_path": "/workspace/project",
        "payload": payload,
    }


@pytest.mark.asyncio
async def test_enqueue_managed_ai_event_drops_payload_without_full_attribution() -> None:
    messages: list[AgentMessage] = []

    async def send_message(message: AgentMessage) -> None:
        messages.append(message)

    sent = await enqueue_managed_ai_event(
        send_message,
        ManagedAiEvent(
            provider="claude",
            client_id=CLIENT_ID,
            window_id=WINDOW_ID,
            source_path=None,
            offset=None,
            cursor=None,
            project_path=None,
            payload={"type": "assistant", "WEB_TERMINAL_CLIENT_ID": str(CLIENT_ID)},
        ),
    )

    assert sent is False
    assert messages == []


def test_read_new_jsonl_events_limits_batch_and_returns_resume_offset(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    lines = [
        {"type": "assistant", "message": {"content": "one"}},
        {"type": "assistant", "message": {"content": "two"}},
        {"type": "assistant", "message": {"content": "three"}},
    ]
    path.write_text("".join(f"{json.dumps(line)}\n" for line in lines), encoding="utf-8")

    first_events, first_offset = read_new_jsonl_events(path, 0, max_events=2)
    second_events, second_offset = read_new_jsonl_events(path, first_offset, max_events=2)

    assert [event for event, _offset in first_events] == lines[:2]
    assert [event for event, _offset in second_events] == lines[2:]
    assert second_offset == path.stat().st_size
