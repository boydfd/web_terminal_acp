from uuid import uuid4

from app.agent_tools.adapters.codex import CodexAdapter
from app.models import Event, EventSourceType


def make_event(payload: dict, kind: str = "response_item") -> Event:
    return Event(
        client_id=uuid4(),
        source_type=EventSourceType.codex_trace,
        source_id="codex-session-1",
        kind=kind,
        payload_json=payload,
        fingerprint=str(uuid4()),
    )


def test_codex_projects_user_response_item_chat() -> None:
    event = make_event(
        {
            "raw_type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        }
    )

    chat = CodexAdapter().project_chat(event)

    assert chat is not None
    assert chat.role == "user"
    assert chat.body == "hello"
    assert chat.is_canonical is True
    assert chat.is_duplicate_candidate is False


def test_codex_storage_keeps_managed_per_window_home() -> None:
    storage = CodexAdapter().prepare_storage("window-1")

    assert storage.env == {"CODEX_HOME": "~/.web-terminal-acp/codex-homes/window-1"}
    assert [str(path) for path in storage.directories] == ["~/.web-terminal-acp/codex-homes/window-1"]


def test_codex_projects_assistant_response_item_chat() -> None:
    event = make_event(
        {
            "raw_type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        }
    )

    chat = CodexAdapter().project_chat(event)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.body == "done"


def test_codex_projects_raw_watcher_assistant_response_item_chat() -> None:
    event = make_event(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        }
    )

    chat = CodexAdapter().project_chat(event)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.body == "done"

    projection = CodexAdapter().project_event(event)

    assert projection.tone == "agent"
    assert projection.label == "Agent response"
    assert projection.body == "done"
    assert projection.subtype == "response_item · message · assistant"


def test_codex_projects_system_and_developer_response_item_detail() -> None:
    adapter = CodexAdapter()
    system_event = make_event(
        {
            "raw_type": "response_item",
            "payload": {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "Base rules"}],
            },
        }
    )
    developer_event = make_event(
        {
            "raw_type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "Use terse replies"}],
            },
        }
    )

    system_projection = adapter.project_event(system_event)
    developer_projection = adapter.project_event(developer_event)

    assert system_projection.tone == "system"
    assert system_projection.label == "System message"
    assert system_projection.body == "Base rules"
    assert system_projection.body_format == "markdown"
    assert developer_projection.tone == "developer"
    assert developer_projection.label == "Developer instructions"
    assert developer_projection.body == "Use terse replies"
    assert developer_projection.body_format == "markdown"


def test_codex_projects_event_msg_as_duplicate_candidate() -> None:
    event = make_event(
        {"raw_type": "event_msg", "payload": {"type": "agent_message", "message": "done"}},
        kind="event_msg",
    )

    chat = CodexAdapter().project_chat(event)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.body == "done"
    assert chat.is_canonical is False
    assert chat.is_duplicate_candidate is True


def test_codex_projects_tool_call_detail() -> None:
    event = make_event(
        {
            "raw_type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "bash",
                "arguments": '{"cmd":"ls"}',
            },
        }
    )

    projection = CodexAdapter().project_event(event)

    assert projection.tone == "tool-call"
    assert projection.label == "Tool call"
    assert projection.body == 'bash\n\n```json\n{\n  "cmd": "ls"\n}\n```'


def test_codex_projects_raw_watcher_function_call_detail() -> None:
    event = make_event(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "bash",
                "arguments": '{"cmd":"ls"}',
            },
        }
    )

    projection = CodexAdapter().project_event(event)

    assert projection.tone == "tool-call"
    assert projection.label == "Tool call"
    assert projection.body == 'bash\n\n```json\n{\n  "cmd": "ls"\n}\n```'
    assert projection.subtype == "response_item · function_call"


def test_codex_projects_json_string_function_call_output_as_json() -> None:
    event = make_event(
        {
            "raw_type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": '{"status":"ok","items":[1,2]}',
            },
        }
    )

    projection = CodexAdapter().project_event(event)

    assert projection.tone == "tool-result"
    assert projection.label == "Tool response"
    assert projection.body_format == "json"
    assert projection.body == '```json\n{\n  "items": [\n    1,\n    2\n  ],\n  "status": "ok"\n}\n```'
