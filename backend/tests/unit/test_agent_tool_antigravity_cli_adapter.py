from uuid import uuid4

from app.agent_tools.adapters.antigravity_cli import AntigravityCliAdapter
from app.models import Event, EventSourceType


def make_event(payload: dict, kind: str = "assistant_message") -> Event:
    return Event(
        client_id=uuid4(),
        source_type=EventSourceType.agent_tool_record,
        source_id="antigravity-session-1",
        kind=kind,
        payload_json=payload,
        fingerprint=str(uuid4()),
    )


def test_antigravity_storage_uses_managed_per_window_home() -> None:
    storage = AntigravityCliAdapter().prepare_storage("window-1")

    assert storage.env == {
        "WEB_TERMINAL_ANTIGRAVITY_HOME": "~/.web-terminal-acp/antigravity-cli-homes/window-1",
        "WEB_TERMINAL_ANTIGRAVITY_COMMAND_HOME": "~/.web-terminal-acp/antigravity-cli-homes/.managed-home/window-1",
    }
    assert [str(path) for path in storage.directories] == [
        "~/.web-terminal-acp/antigravity-cli-homes/window-1",
        "~/.web-terminal-acp/antigravity-cli-homes/.managed-home/window-1",
    ]


def test_antigravity_normalize_uses_generic_source_type_and_session_id() -> None:
    event = AntigravityCliAdapter().normalize(
        {
            "session_id": "antigravity-session-1",
            "step_index": 2,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "content": "fix tests",
        },
        source_path="/tmp/brain/antigravity-session-1/.system_generated/logs/transcript.jsonl",
        cursor=128,
    )

    assert event.source_type == EventSourceType.agent_tool_record
    assert event.source_id == "antigravity-session-1"
    assert event.kind == "user_message"
    assert event.text == "fix tests"
    assert event.payload_json["provider"] == "antigravity_cli"
    assert event.fingerprint.startswith("agent_tool_record:antigravity_cli:")


def test_antigravity_projects_user_input_chat() -> None:
    event = make_event(
        {
            "provider": "antigravity_cli",
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": "<USER_REQUEST>\nfix tests\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\ntime\n</ADDITIONAL_METADATA>",
        },
        kind="user_message",
    )

    chat = AntigravityCliAdapter().project_chat(event)
    projection = AntigravityCliAdapter().project_event(event)

    assert chat is not None
    assert chat.role == "user"
    assert chat.body == "fix tests"
    assert projection.tone == "user-input"
    assert projection.label == "User input"
    assert projection.body == "fix tests"


def test_antigravity_projects_model_response_chat() -> None:
    event = make_event(
        {
            "provider": "antigravity_cli",
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "content": "done",
        },
        kind="assistant_message",
    )

    chat = AntigravityCliAdapter().project_chat(event)
    projection = AntigravityCliAdapter().project_event(event)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.body == "done"
    assert projection.tone == "agent"
    assert projection.label == "Agent response"


def test_antigravity_tool_calls_stay_out_of_chat() -> None:
    event = make_event(
        {
            "provider": "antigravity_cli",
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "content": "I will inspect files",
            "tool_calls": [{"name": "grep_search", "args": {"Query": "antigravity"}}],
        },
        kind="assistant_message",
    )

    adapter = AntigravityCliAdapter()
    chat = adapter.project_chat(event)
    projection = adapter.project_event(event)

    assert chat is None
    assert projection.tone == "tool-call"
    assert projection.label == "Tool call"
    assert projection.body.startswith("grep_search")


def test_antigravity_command_results_are_tool_responses() -> None:
    event = make_event(
        {
            "provider": "antigravity_cli",
            "source": "MODEL",
            "type": "RUN_COMMAND",
            "status": "DONE",
            "content": "pytest output",
        },
        kind="tool_result",
    )

    adapter = AntigravityCliAdapter()
    chat = adapter.project_chat(event)
    projection = adapter.project_event(event)

    assert chat is None
    assert projection.tone == "tool-result"
    assert projection.label == "Tool response"
    assert projection.body == "pytest output"


def test_antigravity_summary_and_index_text_use_chat_body() -> None:
    event = make_event(
        {
            "provider": "antigravity_cli",
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "content": "summarize this",
        },
        kind="user_message",
    )

    adapter = AntigravityCliAdapter()

    assert adapter.summary_text(event) == "summarize this"
    assert adapter.index_text(event) == "summarize this"


def test_antigravity_projects_subagent_call_chat() -> None:
    event = make_event(
        {
            "provider": "antigravity_cli",
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "step_index": 2,
            "tool_calls": [
                {
                    "name": "invoke_subagent",
                    "args": {
                        "Subagents": '[{"Prompt": "Return exactly: 1", "Role": "Greeter Agent", "TypeName": "self"}]'
                    }
                }
            ],
            "subagent_tool_use_results": [
                {"tool_use_id": "step-2", "agent_id": "subagent-1"}
            ]
        },
        kind="assistant_message",
    )

    adapter = AntigravityCliAdapter()
    chat = adapter.project_chat(event)
    projection = adapter.project_event(event)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.agent_message_type == "subagent_call"
    assert chat.subagent_id == "subagent-1"
    assert chat.subagent_tool_use_id == "step-2"
    assert chat.target_session_source_id == "agent-subagent-1"
    assert chat.body == "Description: Greeter Agent\n\nType: self\n\nReturn exactly: 1"
    assert projection.tone == "subagent-call"
    assert projection.label == "Subagent call"


def test_antigravity_projects_subagent_creation_as_tool_response_not_chat() -> None:
    normalized = AntigravityCliAdapter().normalize(
        {
            "session_id": "antigravity-session-1",
            "source": "MODEL",
            "type": "INVOKE_SUBAGENT",
            "step_index": 3,
            "status": "DONE",
            "content": 'Created the following subagents:\n{\n  "conversationId": "subagent-1",\n  "workspaceUris": []\n}',
        },
        source_path="/tmp/brain/antigravity-session-1/.system_generated/logs/transcript.jsonl",
        cursor=128,
    )
    event = make_event(
        {
            "provider": "antigravity_cli",
            "source": "MODEL",
            "type": "INVOKE_SUBAGENT",
            "step_index": 3,
            "status": "DONE",
            "content": 'Created the following subagents:\n{\n  "conversationId": "subagent-1",\n  "workspaceUris": []\n}',
        },
        kind=normalized.kind,
    )

    adapter = AntigravityCliAdapter()
    chat = adapter.project_chat(event)
    projection = adapter.project_event(event)

    assert normalized.kind == "tool_result"
    assert chat is None
    assert projection.tone == "tool-result"
    assert projection.label == "Tool response"
    assert projection.body.startswith("Created the following subagents")


def test_antigravity_projects_parent_message_as_subagent_result_chat() -> None:
    event = make_event(
        {
            "provider": "antigravity_cli",
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "content": (
                "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n"
                "<SYSTEM_MESSAGE>\n"
                "[Message] timestamp=2026-06-02T05:52:12Z sender=subagent-1 "
                "priority=MESSAGE_PRIORITY_HIGH content=hi\n"
                "</SYSTEM_MESSAGE>"
            ),
            "toolUseResult": {"agentId": "subagent-1", "toolUseId": "step-2"},
        },
        kind="system_message",
    )

    adapter = AntigravityCliAdapter()
    chat = adapter.project_chat(event)
    projection = adapter.project_event(event)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.agent_message_type == "subagent_result"
    assert chat.subagent_id == "subagent-1"
    assert chat.subagent_tool_use_id == "step-2"
    assert chat.target_session_source_id == "agent-subagent-1"
    assert chat.body == "hi"
    assert projection.tone == "subagent-result"
    assert projection.label == "Subagent result"
    assert projection.body == "hi"


def test_antigravity_normalize_parent_message_as_chat_visible_assistant_message() -> None:
    event = AntigravityCliAdapter().normalize(
        {
            "session_id": "parent-session",
            "step_index": 11,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "content": (
                "<SYSTEM_MESSAGE>\n"
                "[Message] timestamp=2026-06-02T05:52:12Z sender=subagent-1 "
                "priority=MESSAGE_PRIORITY_HIGH content=hi\n"
                "</SYSTEM_MESSAGE>"
            ),
        },
        source_path="/tmp/brain/parent-session/.system_generated/logs/transcript.jsonl",
        cursor=256,
    )

    assert event.source_id == "parent-session"
    assert event.kind == "assistant_message"


def test_antigravity_projects_subagent_sidechain_prompt() -> None:
    event = make_event(
        {
            "provider": "antigravity_cli",
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "isSidechain": True,
            "agentId": "subagent-1",
            "content": "Return exactly: 1",
        },
        kind="user_message",
    )

    adapter = AntigravityCliAdapter()
    chat = adapter.project_chat(event)
    projection = adapter.project_event(event)

    assert chat is not None
    assert chat.role == "agent"
    assert chat.agent_message_type == "subagent_call"
    assert chat.subagent_id == "subagent-1"
    assert chat.body == "Return exactly: 1"
    assert projection.tone == "subagent-context"
    assert projection.label == "Subagent prompt"
