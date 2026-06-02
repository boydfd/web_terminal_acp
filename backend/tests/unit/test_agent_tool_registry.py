from inspect import Parameter, signature
from typing import get_type_hints
from uuid import uuid4

import pytest
from app.agent_plugins import get_agent_plugin_registry
from app.agent_plugins.builtins import builtin_agent_plugins
from app.agent_plugins.registry import AgentPluginRegistry
from app.agent_plugins.types import (
    AgentCommandSpec,
    AgentManagedStorageSpec,
    AgentNativeConfigSpec,
    AgentPlugin,
)
from app.agent_tools import get_agent_tool_registry
from app.agent_tools.adapters.antigravity_cli import AntigravityCliAdapter
from app.agent_tools.adapters.claude_code import ClaudeCodeAdapter
from app.agent_tools.adapters.codex import CodexAdapter
from app.agent_tools.adapters.cursor_cli import CursorCliAdapter
import app.agent_tools.types as agent_tool_types
from app.agent_tools.types import (
    AgentChatProjection,
    AgentEventProjection,
    AgentToolAdapter,
    AgentToolStorage,
    AgentToolWatchEvent,
)
from app.models import Event, EventSourceType


def test_agent_tool_record_source_type_exists():
    assert EventSourceType.agent_tool_record.value == "agent_tool_record"


def test_default_registry_contains_initial_providers():
    registry = get_agent_tool_registry()

    assert registry.by_provider("codex").provider_id == "codex"
    assert registry.by_provider("claude_code").provider_id == "claude_code"
    assert registry.by_provider("cursor_cli").provider_id == "cursor_cli"
    assert registry.by_provider("antigravity_cli").provider_id == "antigravity_cli"


def test_agent_plugin_registry_describes_builtin_agent_clients():
    registry = get_agent_plugin_registry()

    assert registry.normalize_agent_id("claude_code") == "claude"
    assert registry.normalize_agent_id("agent") == "cursor"
    assert registry.by_agent_id("claude").provider_id == "claude_code"
    assert registry.by_provider("cursor").provider_id == "cursor_cli"

    descriptors = {descriptor.id: descriptor for descriptor in registry.descriptors()}
    assert descriptors["codex"].label == "Codex"
    assert descriptors["codex"].capabilities.agent_records is True
    assert descriptors["codex"].capabilities.runtime_tags is True
    assert descriptors["codex"].capabilities.work_presence is True
    codex_plugin = registry.by_agent_id("codex")
    assert codex_plugin.tool_adapter_module == "codex"
    assert codex_plugin.tool_adapter_class == "CodexAdapter"
    assert descriptors["claude"].default_command == "claude"
    assert descriptors["cursor"].command_names == ("agent", "cursor", "cursor-agent")
    assert descriptors["antigravity"].label == "Antigravity CLI"
    assert descriptors["antigravity"].provider_id == "antigravity_cli"
    assert descriptors["antigravity"].default_command == "agy-p"
    assert descriptors["antigravity"].command_names == ("agy-p", "agy")
    assert descriptors["antigravity"].capabilities.launch is True
    assert descriptors["antigravity"].capabilities.agent_records is True
    antigravity_plugin = registry.by_agent_id("antigravity")
    assert antigravity_plugin.tool_adapter_module == "antigravity_cli"
    assert antigravity_plugin.tool_adapter_class == "AntigravityCliAdapter"
    assert registry.normalize_agent_id("agy") == "antigravity"
    assert registry.provider_for_command_name("agy-p") == "antigravity_cli"
    assert registry.command_pattern().search("cursor-agent --resume session")
    assert get_agent_tool_registry().command_pattern().search("cursor-agent --resume session")


def _future_agent_plugin() -> AgentPlugin:
    return AgentPlugin(
        agent_client_id="future_agent",
        provider_id="future_provider",
        label="Future Agent",
        aliases=("future",),
        command=AgentCommandSpec(
            default_command="/opt/future/bin/future-agent",
            command_names=("/opt/future/bin/future-agent",),
            permission_flag="--safe-mode",
        ),
        storage=AgentManagedStorageSpec(
            user_root=".future-agent",
            managed_root=".web-terminal-acp/future-agent-homes",
            managed_home_alias=".future-agent",
            skills_directory="skills",
            config_item_names=("settings.json", "skills", "skills.disabled"),
            history_item_names=("history.jsonl",),
            env={"FUTURE_HOME": "{managed_root}"},
            shell_env_aliases={"FUTURE_HOME": "FUTURE_HOME"},
        ),
        native_config=AgentNativeConfigSpec(
            hooks_config_name="hooks.json",
            profile_agent_md_targets=("AGENT.md",),
            initial_agent_md_candidates=("AGENT.md",),
            plugin_strategy="directory",
        ),
    )


def test_agent_plugin_registry_accepts_non_builtin_plugin_contract():
    registry = AgentPluginRegistry((_future_agent_plugin(),))

    assert registry.normalize_agent_id("future") == "future_agent"
    assert registry.by_provider("future_agent").provider_id == "future_provider"
    assert registry.provider_for_command_name("/opt/future/bin/future-agent") == "future_provider"
    assert registry.command_pattern().search("future-agent run")

    descriptor = registry.descriptors()[0]
    assert descriptor.id == "future_agent"
    assert descriptor.provider_id == "future_provider"
    assert descriptor.default_command == "/opt/future/bin/future-agent"
    assert descriptor.command_names == ("/opt/future/bin/future-agent",)
    assert descriptor.capabilities.launch is True
    assert descriptor.capabilities.client_config is True
    assert descriptor.capabilities.agent_records is False
    assert descriptor.capabilities.runtime_tags is False
    assert descriptor.capabilities.work_presence is False


def test_builtin_plugins_declare_adapters_for_record_capable_clients():
    plugins = {plugin.agent_client_id: plugin for plugin in get_agent_plugin_registry().all()}
    registry = get_agent_tool_registry()

    for plugin in plugins.values():
        if not plugin.capabilities.agent_records:
            continue
        assert plugin.tool_adapter_module is not None
        assert plugin.tool_adapter_class is not None
        assert registry.by_provider(plugin.provider_id).provider_id == plugin.provider_id


def test_agent_plugin_registry_rejects_duplicate_provider_ids():
    codex, claude, *_ = builtin_agent_plugins()
    duplicate = type(claude)(**{**claude.__dict__, "provider_id": codex.provider_id})

    with pytest.raises(ValueError, match="duplicate agent plugin provider_id"):
        AgentPluginRegistry((codex, duplicate))


def test_agent_plugin_registry_rejects_duplicate_aliases_and_command_names():
    codex, claude, *_ = builtin_agent_plugins()
    duplicate_alias = type(claude)(**{**claude.__dict__, "aliases": ("codex",)})
    duplicate_command = type(claude)(
        **{**claude.__dict__, "command": type(claude.command)("claude", ("codex",))}
    )

    with pytest.raises(ValueError, match="duplicate agent plugin agent_client_id/alias"):
        AgentPluginRegistry((codex, duplicate_alias))
    with pytest.raises(ValueError, match="duplicate agent plugin command_name"):
        AgentPluginRegistry((codex, duplicate_command))


def test_registry_reports_unknown_provider_as_value_error():
    registry = get_agent_tool_registry()

    try:
        registry.by_provider("unknown")
    except ValueError as exc:
        assert str(exc) == "unknown agent provider: unknown"
    else:
        raise AssertionError("expected unknown provider to raise ValueError")


def test_registry_resolves_legacy_and_generic_source_types():
    registry = get_agent_tool_registry()

    assert registry.by_source_type(EventSourceType.codex_trace).provider_id == "codex"
    assert registry.by_source_type(EventSourceType.claude_jsonl).provider_id == "claude_code"
    assert (
        registry.by_source_type(EventSourceType.agent_tool_record, provider="cursor_cli").provider_id
        == "cursor_cli"
    )


def test_registry_reports_agent_activity_source_types():
    source_types = get_agent_tool_registry().agent_activity_source_types()

    assert EventSourceType.claude_jsonl in source_types
    assert EventSourceType.codex_trace in source_types
    assert EventSourceType.agent_tool_record in source_types


def test_adapter_protocol_uses_orm_events_for_projection_methods():
    namespace = vars(agent_tool_types)

    project_event_hints = get_type_hints(AgentToolAdapter.project_event, globalns=namespace)
    assert project_event_hints["event"] is Event
    assert project_event_hints["return"] is AgentEventProjection

    project_chat_hints = get_type_hints(AgentToolAdapter.project_chat, globalns=namespace)
    assert project_chat_hints["event"] is Event
    assert project_chat_hints["return"] == AgentChatProjection | None

    is_completion_hints = get_type_hints(AgentToolAdapter.is_completion, globalns=namespace)
    assert is_completion_hints["event"] is Event
    assert is_completion_hints["return"] is bool

    summary_text_hints = get_type_hints(AgentToolAdapter.summary_text, globalns=namespace)
    assert summary_text_hints["event"] is Event
    assert summary_text_hints["return"] is str

    index_text_hints = get_type_hints(AgentToolAdapter.index_text, globalns=namespace)
    assert index_text_hints["event"] is Event
    assert index_text_hints["return"] is str


def test_adapter_normalize_methods_accept_source_path_and_cursor_keywords():
    for adapter in get_agent_tool_registry().all():
        parameters = signature(adapter.normalize).parameters

        assert "source_path" in parameters
        assert parameters["source_path"].kind is Parameter.KEYWORD_ONLY
        assert "cursor" in parameters
        assert parameters["cursor"].kind is Parameter.KEYWORD_ONLY


def test_codex_adapter_normalizes_legacy_trace_as_agent_tool_record():
    event = CodexAdapter().normalize(
        {"trace_id": "trace-1", "span": {"name": "tool_call", "attributes": {"tool": "bash"}}},
        source_path=None,
        cursor=None,
    )

    assert event.source_type is EventSourceType.agent_tool_record
    assert event.payload_json["provider"] == "codex"
    assert event.fingerprint.startswith("agent_tool_record:codex:")
    assert not event.fingerprint.startswith("codex_trace:")


def test_claude_code_adapter_normalizes_legacy_jsonl_as_agent_tool_record():
    event = ClaudeCodeAdapter().normalize(
        {"type": "user", "message": {"content": "fix nginx 403"}, "sessionId": "session-1"},
        source_path="/tmp/claude.jsonl",
        cursor=12,
    )

    assert event.source_type is EventSourceType.agent_tool_record
    assert event.payload_json["provider"] == "claude_code"
    assert event.fingerprint.startswith("agent_tool_record:claude_code:")
    assert not event.fingerprint.startswith("claude_jsonl:")


def test_claude_code_adapter_keeps_provider_scoped_fingerprint_bounded():
    event = ClaudeCodeAdapter().normalize(
        {"type": "user", "message": {"content": "fix nginx 403"}, "sessionId": "session-1"},
        source_path="/tmp/" + "x" * 40,
        cursor=12,
    )

    assert event.fingerprint.startswith("agent_tool_record:claude_code:")
    assert len(event.fingerprint) <= 128


def test_cursor_cli_adapter_normalizes_generic_records_as_self_describing_agent_tool_records():
    event = CursorCliAdapter().normalize(
        {"session_id": "cursor-session-1", "kind": "assistant_message", "content": "hello"},
        source_path=None,
        cursor=12,
    )

    assert event.source_type is EventSourceType.agent_tool_record
    assert event.payload_json["provider"] == "cursor_cli"
    assert event.fingerprint.startswith("agent_tool_record:cursor_cli:")


def test_antigravity_cli_adapter_normalizes_generic_records_as_self_describing_agent_tool_records():
    event = AntigravityCliAdapter().normalize(
        {"session_id": "antigravity-session-1", "type": "PLANNER_RESPONSE", "content": "hello"},
        source_path=None,
        cursor=12,
    )

    assert event.source_type is EventSourceType.agent_tool_record
    assert event.payload_json["provider"] == "antigravity_cli"
    assert event.fingerprint.startswith("agent_tool_record:antigravity_cli:")


def test_cursor_cli_adapter_bounds_long_source_id_and_kind_deterministically():
    payload = {
        "session_id": "cursor-session-" + "x" * 900,
        "kind": "cursor-kind-" + "y" * 300,
        "content": "hello",
    }

    event_a = CursorCliAdapter().normalize(payload, source_path=None, cursor=12)
    event_b = CursorCliAdapter().normalize(payload, source_path=None, cursor=12)

    assert event_a.source_id == event_b.source_id
    assert event_a.source_id.startswith("cursor-session-")
    assert len(event_a.source_id) <= 512
    assert event_a.kind == event_b.kind
    assert event_a.kind.startswith("cursor-kind-")
    assert len(event_a.kind) <= 128


def test_adapter_projection_helpers_mark_json_fallback_format():
    event = Event(
        client_id=uuid4(),
        source_type=EventSourceType.agent_tool_record,
        source_id="session-1",
        kind="event",
        payload_json={"span": {"attributes": {"tool": "bash"}}},
        fingerprint="agent-tool-event-json-1",
    )

    projection = CodexAdapter().project_event(event)

    assert projection.body_format == "json"
    assert projection.body.startswith("```json")


def test_adapter_projection_helpers_accept_orm_event_payloads():
    event = Event(
        client_id=uuid4(),
        source_type=EventSourceType.agent_tool_record,
        source_id="session-1",
        kind="user_message",
        payload_json={"message": {"content": "hello from orm"}},
        fingerprint="agent-tool-event-1",
    )

    for adapter in get_agent_tool_registry().all():
        projection = adapter.project_event(event)

        assert projection.body == "hello from orm"
        assert adapter.project_chat(event) is None
        assert adapter.summary_text(event) == "hello from orm"
        assert adapter.index_text(event) == "hello from orm"


def test_agent_tool_watch_event_source_path_is_string_or_none():
    hints = get_type_hints(AgentToolWatchEvent)

    assert hints["source_path"] == str | None


def test_agent_tool_storage_contract_contains_env_and_directories():
    hints = get_type_hints(AgentToolStorage, globalns=vars(agent_tool_types))

    assert "env" in hints
    assert "directories" in hints
