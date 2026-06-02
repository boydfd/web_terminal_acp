from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from .builtins import builtin_agent_plugins
from .types import AgentClientDescriptor, AgentPlugin


def _normal_key(value: str) -> str:
    return value.strip().lower()


def _require_unique(
    index: dict[str, AgentPlugin],
    key: str,
    plugin: AgentPlugin,
    *,
    label: str,
) -> None:
    if key in index:
        owner = index[key]
        raise ValueError(
            f"duplicate agent plugin {label}: {key!r} "
            f"for {plugin.agent_client_id!r} conflicts with {owner.agent_client_id!r}"
        )
    index[key] = plugin


class AgentPluginRegistry:
    def __init__(self, plugins: Iterable[AgentPlugin]):
        self._plugins = tuple(plugins)
        self._by_agent_id: dict[str, AgentPlugin] = {}
        self._by_provider_id: dict[str, AgentPlugin] = {}
        self._by_command_name: dict[str, AgentPlugin] = {}
        for plugin in self._plugins:
            provider_id = _normal_key(plugin.provider_id)
            agent_client_id = _normal_key(plugin.agent_client_id)
            if not provider_id:
                raise ValueError(f"agent plugin {plugin.agent_client_id!r} has empty provider_id")
            if not agent_client_id:
                raise ValueError(f"agent plugin {plugin.provider_id!r} has empty agent_client_id")
            _require_unique(
                self._by_provider_id,
                provider_id,
                plugin,
                label="provider_id",
            )
            for agent_id in plugin.all_agent_ids:
                normalized = _normal_key(agent_id)
                if not normalized:
                    raise ValueError(f"agent plugin {plugin.agent_client_id!r} has empty alias")
                _require_unique(
                    self._by_agent_id,
                    normalized,
                    plugin,
                    label="agent_client_id/alias",
                )
            for command_name in plugin.command.command_names:
                normalized_command = _normal_key(Path(command_name).name)
                if not normalized_command:
                    raise ValueError(
                        f"agent plugin {plugin.agent_client_id!r} has empty command name"
                    )
                _require_unique(
                    self._by_command_name,
                    normalized_command,
                    plugin,
                    label="command_name",
                )

    def all(self) -> tuple[AgentPlugin, ...]:
        return self._plugins

    def descriptors(self) -> tuple[AgentClientDescriptor, ...]:
        return tuple(
            AgentClientDescriptor(
                id=plugin.agent_client_id,
                provider_id=plugin.provider_id,
                label=plugin.label,
                aliases=plugin.aliases,
                default_command=plugin.command.default_command,
                command_names=plugin.command.command_names,
                capabilities=plugin.capabilities,
            )
            for plugin in self._plugins
        )

    def normalize_agent_id(self, agent: str) -> str:
        normalized = _normal_key(agent)
        plugin = self._by_agent_id.get(normalized)
        if plugin is None:
            raise ValueError(f"unsupported agent: {agent}")
        return plugin.agent_client_id

    def canonical_provider(self, provider: str) -> str:
        normalized = _normal_key(provider)
        plugin = self._by_provider_id.get(normalized) or self._by_agent_id.get(normalized)
        if plugin is None:
            return provider
        return plugin.provider_id

    def by_agent_id(self, agent: str) -> AgentPlugin:
        return self._by_agent_id[self.normalize_agent_id(agent)]

    def by_provider(self, provider: str) -> AgentPlugin:
        canonical = self.canonical_provider(provider)
        try:
            return self._by_provider_id[_normal_key(canonical)]
        except KeyError as exc:
            raise ValueError(f"unknown agent provider: {provider}") from exc

    def command_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                self._by_command_name,
                key=len,
                reverse=True,
            )
        )

    def command_pattern(self) -> re.Pattern[str]:
        names = self.command_names()
        if not names:
            return re.compile(r"$^")
        return re.compile(r"(?:^|\b)(" + "|".join(re.escape(name) for name in names) + r")(?:\b|$)")

    def provider_for_command_name(self, command_name: str) -> str | None:
        plugin = self._by_command_name.get(_normal_key(Path(command_name).name))
        return plugin.provider_id if plugin is not None else None


_DEFAULT_REGISTRY: AgentPluginRegistry | None = None


def get_agent_plugin_registry() -> AgentPluginRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = AgentPluginRegistry(builtin_agent_plugins())
    return _DEFAULT_REGISTRY


def list_agent_client_descriptors() -> tuple[AgentClientDescriptor, ...]:
    return get_agent_plugin_registry().descriptors()
