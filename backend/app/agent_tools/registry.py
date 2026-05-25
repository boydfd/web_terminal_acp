from __future__ import annotations

import re
from collections.abc import Iterable

from app.models import EventSourceType

from .adapters.claude_code import ClaudeCodeAdapter
from .adapters.codex import CodexAdapter
from .adapters.cursor_cli import CursorCliAdapter
from .types import AgentToolAdapter


class AgentToolRegistry:
    def __init__(self, adapters: Iterable[AgentToolAdapter]):
        self._adapters = tuple(adapters)
        self._by_provider = {adapter.provider_id: adapter for adapter in self._adapters}

    def all(self) -> tuple[AgentToolAdapter, ...]:
        return self._adapters

    def by_provider(self, provider: str) -> AgentToolAdapter:
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
        names = sorted(
            {name for adapter in self._adapters for name in adapter.command_names},
            key=len,
            reverse=True,
        )
        return re.compile(r"(?:^|\b)(" + "|".join(re.escape(name) for name in names) + r")(?:\b|$)")


def get_agent_tool_registry() -> AgentToolRegistry:
    return AgentToolRegistry((CodexAdapter(), ClaudeCodeAdapter(), CursorCliAdapter()))
