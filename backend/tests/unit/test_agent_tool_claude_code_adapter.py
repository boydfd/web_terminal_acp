from uuid import uuid4

from app.agent_tools.adapters.claude_code import ClaudeCodeAdapter
from app.models import Event, EventSourceType


def make_event(payload: dict, kind: str = "assistant_message") -> Event:
    return Event(
        client_id=uuid4(),
        source_type=EventSourceType.claude_jsonl,
        source_id="claude-session-1",
        kind=kind,
        payload_json=payload,
        fingerprint=str(uuid4()),
    )


def test_claude_code_normalize_managed_event_uses_generic_source_type() -> None:
    payload = {"type": "user", "sessionId": "session-1", "message": {"content": "hello"}}

    event = ClaudeCodeAdapter().normalize(payload, source_path="/tmp/session.jsonl", cursor=10)

    assert event.source_type == EventSourceType.agent_tool_record
    assert event.source_id == "session-1"
    assert event.kind == "user_message"
    assert event.text == "hello"
    assert event.payload_json["provider"] == "claude_code"
    assert event.fingerprint.startswith("agent_tool_record:claude_code:")


def test_claude_code_storage_uses_managed_per_window_home() -> None:
    storage = ClaudeCodeAdapter().prepare_storage("window-1")

    assert storage.env == {"CLAUDE_CONFIG_DIR": "~/.web-terminal-acp/claude-code-homes/window-1"}
    assert [str(path) for path in storage.directories] == ["~/.web-terminal-acp/claude-code-homes/window-1"]


def test_claude_code_projects_user_chat() -> None:
    event = make_event({"type": "user", "message": {"content": "fix bug"}}, kind="user_message")

    chat = ClaudeCodeAdapter().project_chat(event)

    assert chat is not None
    assert chat.role == "user"
    assert chat.body == "fix bug"


def test_claude_code_projects_assistant_chat_from_text_block() -> None:
    event = make_event(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
        kind="assistant_message",
    )

    chat = ClaudeCodeAdapter().project_chat(event)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.body == "done"


def test_claude_code_omits_tool_only_user_chat() -> None:
    event = make_event(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1", "content": "command output"}
                ],
            },
        },
        kind="user_message",
    )

    assert ClaudeCodeAdapter().project_chat(event) is None


def test_claude_code_omits_tool_only_assistant_chat() -> None:
    event = make_event(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pytest"}}
                ],
            },
        },
        kind="assistant_message",
    )

    assert ClaudeCodeAdapter().project_chat(event) is None


def test_claude_code_projects_mixed_chat_text_without_tool_blocks() -> None:
    event = make_event(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Running tests."},
                    {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "pytest"}},
                    "Tests passed.",
                    {"type": "tool_result", "tool_use_id": "tool-1", "content": "all green"},
                ],
            },
        },
        kind="assistant_message",
    )

    chat = ClaudeCodeAdapter().project_chat(event)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.body == "Running tests.\nTests passed."


def test_claude_code_projects_tool_use_detail() -> None:
    event = make_event(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}}
                ]
            },
        },
        kind="assistant_message",
    )

    projection = ClaudeCodeAdapter().project_event(event)

    assert projection.tone == "tool-call"
    assert projection.label == "Tool call"
    assert "Bash" in projection.body
    assert "pytest" in projection.body
