from __future__ import annotations

import importlib
import re
from collections.abc import Iterable

from app.models import EventSourceType

from app.agent_plugins import get_agent_plugin_registry
from app.agent_plugins.types import AgentPlugin
from .types import AgentToolAdapter


class AgentToolRegistry:
    def __init__(self, adapters: Iterable[AgentToolAdapter]):
        self._adapters = tuple(adapters)
        self._by_provider = {adapter.provider_id: adapter for adapter in self._adapters}

    def all(self) -> tuple[AgentToolAdapter, ...]:
        return self._adapters

    def by_provider(self, provider: str) -> AgentToolAdapter:
        provider = get_agent_plugin_registry().canonical_provider(provider)
        try:
            return self._by_provider[provider]
        except KeyError as exc:
            raise ValueError(f"unknown agent provider: {provider}") from exc

    def by_source_type(
        self, source_type: EventSourceType | str, provider: str | None = None
    ) -> AgentToolAdapter:
        if isinstance(source_type, str):
            source_type = EventSourceType(source_type)

        matches = [
            adapter
            for adapter in self._adapters
            if source_type in adapter.source_types or source_type in adapter.legacy_source_types
        ]

        if provider is not None:
            adapter = self.by_provider(provider)
            if adapter in matches:
                return adapter
            raise KeyError(provider)

        if len(matches) == 1:
            return matches[0]

        if not matches:
            raise KeyError(source_type)

        raise ValueError(f"source_type {source_type.value!r} requires provider")

    def agent_activity_source_types(self) -> set[EventSourceType]:
        source_types: set[EventSourceType] = set()
        for adapter in self._adapters:
            if adapter.ai_activity:
                source_types.update(adapter.source_types)
                source_types.update(adapter.legacy_source_types)
        return source_types

    def command_pattern(self) -> re.Pattern[str]:
        names = get_agent_plugin_registry().command_names()
        return re.compile(r"(?:^|\b)(" + "|".join(re.escape(name) for name in names) + r")(?:\b|$)")


def get_agent_tool_registry() -> AgentToolRegistry:
    return AgentToolRegistry(
        adapter
        for plugin in get_agent_plugin_registry().all()
        if (adapter := _adapter_from_plugin(plugin)) is not None
    )


def _adapter_from_plugin(plugin: AgentPlugin) -> AgentToolAdapter | None:
    module_name = plugin.tool_adapter_module
    class_name = plugin.tool_adapter_class
    if module_name is None and class_name is None:
        return None
    if not module_name or not class_name:
        raise ValueError(f"agent plugin {plugin.agent_client_id!r} has incomplete tool adapter metadata")
    if not module_name.isidentifier() or not class_name.isidentifier():
        raise ValueError(f"agent plugin {plugin.agent_client_id!r} has invalid tool adapter metadata")
    module = importlib.import_module(f"app.agent_tools.adapters.{module_name}")
    adapter = getattr(module, class_name)()
    if adapter.provider_id != plugin.provider_id:
        raise ValueError(
            f"agent plugin {plugin.agent_client_id!r} adapter provider "
            f"{adapter.provider_id!r} does not match {plugin.provider_id!r}"
        )
    return adapter
