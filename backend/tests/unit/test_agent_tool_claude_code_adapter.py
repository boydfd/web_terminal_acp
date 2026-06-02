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


def test_claude_code_tool_result_user_event_is_not_chat() -> None:
    event = make_event(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "pytest output"}],
            },
        },
        kind="user_message",
    )

    adapter = ClaudeCodeAdapter()
    chat = adapter.project_chat(event)
    projection = adapter.project_event(event)

    assert chat is None
    assert projection.tone == "tool-result"
    assert projection.label == "Tool response"


def test_claude_code_projects_agent_tool_use_as_subagent_call_chat() -> None:
    event = make_event(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-subagent-1",
                        "name": "Agent",
                        "input": {
                            "description": "Return one",
                            "prompt": "Return exactly: 1",
                            "subagent_type": "claude",
                        },
                    }
                ],
            },
            "subagent_tool_use_results": [
                {"tool_use_id": "call-subagent-1", "agent_id": "subagent-1"},
            ],
        },
        kind="assistant_message",
    )

    adapter = ClaudeCodeAdapter()
    chat = adapter.project_chat(event)
    projection = adapter.project_event(event)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.agent_message_type == "subagent_call"
    assert chat.subagent_id == "subagent-1"
    assert chat.subagent_tool_use_id == "call-subagent-1"
    assert chat.target_session_source_id == "agent-subagent-1"
    assert chat.body == "Description: Return one\n\nType: claude\n\nReturn exactly: 1"
    assert projection.tone == "subagent-call"
    assert projection.label == "Subagent call"


def test_claude_code_projects_agent_tool_result_as_subagent_result_chat() -> None:
    event = make_event(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-subagent-1",
                        "content": [
                            {"type": "text", "text": "1"},
                            {"type": "text", "text": "agentId: subagent-1\n<usage>tokens</usage>"},
                        ],
                    }
                ],
            },
            "toolUseResult": {"agentId": "subagent-1", "toolUseId": "call-subagent-1"},
        },
        kind="user_message",
    )

    adapter = ClaudeCodeAdapter()
    chat = adapter.project_chat(event)
    projection = adapter.project_event(event)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.agent_message_type == "subagent_result"
    assert chat.subagent_id == "subagent-1"
    assert chat.subagent_tool_use_id == "call-subagent-1"
    assert chat.target_session_source_id == "agent-subagent-1"
    assert chat.body == "1"
    assert projection.tone == "subagent-result"
    assert projection.label == "Subagent result"


def test_claude_code_projects_subagent_prompt_as_main_agent_call_chat_and_uses_agent_source_id() -> None:
    payload = {
        "type": "user",
        "sessionId": "main-session-1",
        "agentId": "subagent-1",
        "isSidechain": True,
        "subagent": {"toolUseId": "call-subagent-1"},
        "message": {"role": "user", "content": "Return exactly: 1"},
    }
    event = make_event(payload, kind="user_message")

    adapter = ClaudeCodeAdapter()
    normalized = adapter.normalize(payload, source_path="/tmp/agent-subagent-1.jsonl", cursor=0)
    chat = adapter.project_chat(event)
    projection = adapter.project_event(event)

    assert normalized.source_id == "agent-subagent-1"
    assert chat is not None
    assert chat.role == "agent"
    assert chat.agent_message_type == "subagent_call"
    assert chat.subagent_id == "subagent-1"
    assert chat.subagent_tool_use_id == "call-subagent-1"
    assert chat.target_session_source_id is None
    assert chat.body == "Return exactly: 1"
    assert projection.tone == "subagent-context"
    assert projection.label == "Subagent prompt"


def test_claude_code_subagent_tool_use_and_sidechain_prompt_share_chat_dedupe_key() -> None:
    adapter = ClaudeCodeAdapter()
    tool_use = make_event(
        {
            "type": "assistant",
            "sessionId": "main-session-1",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call-subagent-1",
                        "name": "Agent",
                        "input": {"prompt": "Return exactly: 1"},
                    }
                ],
            },
            "subagent_tool_use_results": [
                {"tool_use_id": "call-subagent-1", "agent_id": "subagent-1"},
            ],
        },
        kind="assistant_message",
    )
    sidechain_prompt = make_event(
        {
            "type": "user",
            "sessionId": "main-session-1",
            "agentId": "subagent-1",
            "isSidechain": True,
            "subagent": {"toolUseId": "call-subagent-1"},
            "message": {"role": "user", "content": "Return exactly: 1"},
        },
        kind="user_message",
    )

    canonical = adapter.project_chat(tool_use)
    duplicate = adapter.project_chat(sidechain_prompt)

    assert canonical is not None
    assert duplicate is not None
    assert canonical.dedupe_key == duplicate.dedupe_key
    assert canonical.is_canonical is True
    assert duplicate.is_canonical is False
    assert duplicate.is_duplicate_candidate is True


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
