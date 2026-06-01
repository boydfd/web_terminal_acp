from datetime import datetime, timezone
from uuid import uuid4

from app.models import Event, EventSourceType
from app.services.agent_activity_projection import (
    event_is_agent_activity,
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
