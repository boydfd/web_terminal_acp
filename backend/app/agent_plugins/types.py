from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

AgentConfigSectionKind = Literal["skills", "plugins", "hooks"]


@dataclass(frozen=True)
class AgentManagedStorageSpec:
    user_root: str
    managed_root: str
    managed_home_alias: str
    skills_directory: str
    config_item_names: tuple[str, ...]
    history_item_names: tuple[str, ...]
    env: Mapping[str, str]
    shell_env_aliases: Mapping[str, str]
    shell_prepare_function: str | None = None


@dataclass(frozen=True)
class AgentNativeConfigSpec:
    hooks_config_name: str
    profile_agent_md_targets: tuple[str, ...]
    initial_agent_md_candidates: tuple[str, ...]
    plugin_strategy: Literal["codex_toml", "claude_settings", "directory"]
    hook_strategy: Literal["json", "claude_settings"] = "json"


@dataclass(frozen=True)
class AgentCommandSpec:
    default_command: str
    command_names: tuple[str, ...]
    permission_flag: str | None = None
    non_task_subcommands: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentClientCapabilities:
    launch: bool = True
    client_config: bool = True
    window_config: bool = True
    profile_config: bool = True
    agent_records: bool = False
    runtime_tags: bool = False
    work_presence: bool = False


@dataclass(frozen=True)
class AgentPlugin:
    agent_client_id: str
    provider_id: str
    label: str
    aliases: tuple[str, ...]
    command: AgentCommandSpec
    storage: AgentManagedStorageSpec
    native_config: AgentNativeConfigSpec
    capabilities: AgentClientCapabilities = AgentClientCapabilities()
    watch_collector_name: str | None = None
    tool_adapter_module: str | None = None
    tool_adapter_class: str | None = None

    @property
    def all_agent_ids(self) -> tuple[str, ...]:
        return (self.agent_client_id, *self.aliases)


@dataclass(frozen=True)
class AgentClientDescriptor:
    id: str
    provider_id: str
    label: str
    aliases: tuple[str, ...]
    default_command: str
    command_names: tuple[str, ...]
    capabilities: AgentClientCapabilities
