from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from app.models import Event, EventSourceType
from app.services.ingest.normalizers import NormalizedEvent


@dataclass(frozen=True)
class AgentEventProjection:
    tone: str
    label: str
    body: str
    body_format: Literal["markdown", "json"] = "markdown"
    subtype: str | None = None
    agent_message_type: str | None = None
    subagent_id: str | None = None
    subagent_tool_use_id: str | None = None
    target_session_source_id: str | None = None


@dataclass(frozen=True)
class AgentChatProjection:
    role: str
    body: str
    body_format: Literal["markdown", "json"] = "markdown"
    dedupe_key: str | None = None
    is_canonical: bool = True
    is_duplicate_candidate: bool = False
    agent_message_type: str | None = None
    subagent_id: str | None = None
    subagent_tool_use_id: str | None = None
    target_session_source_id: str | None = None


@dataclass(frozen=True)
class AgentToolStorage:
    env: Mapping[str, str]
    directories: Sequence[Path] = ()


@dataclass(frozen=True)
class AgentToolWatchEvent:
    provider: str
    payload: dict[str, Any]
    source_path: str | None
    cursor: str | int | None


class AgentToolAdapter(Protocol):
    provider_id: str
    source_types: Sequence[EventSourceType]
    legacy_source_types: Sequence[EventSourceType]
    command_names: Sequence[str]
    ai_activity: bool

    def prepare_storage(self, window_id: str) -> AgentToolStorage: ...

    def normalize(
        self, payload: dict[str, Any], *, source_path: str | None, cursor: str | int | None
    ) -> NormalizedEvent: ...

    def project_event(self, event: Event) -> AgentEventProjection: ...

    def project_chat(self, event: Event) -> AgentChatProjection | None: ...

    def is_completion(self, event: Event) -> bool: ...

    def summary_text(self, event: Event) -> str: ...

    def index_text(self, event: Event) -> str: ...
