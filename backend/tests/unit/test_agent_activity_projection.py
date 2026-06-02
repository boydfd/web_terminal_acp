from datetime import datetime, timezone
from uuid import uuid4

from app.models import Event, EventSourceType
from app.services.agent_activity_projection import (
    event_is_agent_activity,
    event_is_agent_completion,
    event_is_agent_user_input,
)


def make_event(payload: dict, *, kind: str = "response_item") -> Event:
    return Event(
        client_id=uuid4(),
        source_type=EventSourceType.agent_tool_record,
        source_id="codex-session-1",
        kind=kind,
        virtual_window_id=uuid4(),
        payload_json=payload,
        fingerprint=str(uuid4()),
        created_at=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
    )


def test_codex_session_meta_is_not_agent_activity() -> None:
    event = make_event(
        {
            "provider": "codex",
            "raw_type": "session_meta",
            "payload": {"id": "codex-session-1"},
        },
        kind="session_meta",
    )

    assert event_is_agent_activity(event) is False
    assert event_is_agent_user_input(event) is False


def test_codex_real_user_message_starts_agent_activity() -> None:
    event = make_event(
        {
            "provider": "codex",
            "raw_type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "fix tests"}],
            },
        }
    )

    assert event_is_agent_user_input(event) is True
    assert event_is_agent_activity(event) is True


def test_codex_synthetic_user_message_is_context_not_activity() -> None:
    event = make_event(
        {
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
        }
    )

    assert event_is_agent_user_input(event) is False
    assert event_is_agent_activity(event) is False


def test_claude_turn_duration_marks_completion() -> None:
    event = make_event(
        {
            "provider": "claude_code",
            "type": "system",
            "subtype": "turn_duration",
        },
        kind="system_message",
    )

    assert event_is_agent_completion(event) is True
    assert event_is_agent_activity(event) is True


def test_claude_metadata_after_completion_is_not_agent_activity() -> None:
    for payload_type in ("last-prompt", "ai-title", "permission-mode"):
        event = make_event(
            {
                "provider": "claude_code",
                "type": payload_type,
                "content": "metadata",
            },
            kind=payload_type,
        )

        assert event_is_agent_completion(event) is False
        assert event_is_agent_activity(event) is False


def test_cursor_assistant_message_marks_completion() -> None:
    event = make_event(
        {
            "provider": "cursor_cli",
            "role": "assistant",
            "text": "done",
        },
        kind="assistant_message",
    )

    assert event_is_agent_completion(event) is True
    assert event_is_agent_activity(event) is True


def test_antigravity_final_model_response_marks_completion() -> None:
    event = make_event(
        {
            "provider": "antigravity_cli",
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "content": "done",
        },
        kind="assistant_message",
    )

    assert event_is_agent_completion(event) is True
    assert event_is_agent_activity(event) is True


def test_antigravity_system_context_is_not_agent_activity() -> None:
    event = make_event(
        {
            "provider": "antigravity_cli",
            "source": "SYSTEM",
            "type": "CONVERSATION_HISTORY",
            "status": "DONE",
        },
        kind="context",
    )

    assert event_is_agent_completion(event) is False
    assert event_is_agent_activity(event) is False
