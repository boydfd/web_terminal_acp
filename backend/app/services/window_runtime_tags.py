from __future__ import annotations

import re
import shlex

from app.agent_tools import get_agent_tool_registry
from app.models import AiSession, VirtualWindow

_COMMAND_SEGMENT_PATTERN = re.compile(r"&&|\|\||[;|]")
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_COMMAND_WRAPPERS = {"command", "env", "sudo"}


def agent_from_command(command: str | None) -> str | None:
    if not command:
        return None

    command_providers = {
        name: adapter.provider_id
        for adapter in get_agent_tool_registry().all()
        for name in adapter.command_names
    }
    for segment in _COMMAND_SEGMENT_PATTERN.split(command):
        provider = _agent_from_command_segment(segment, command_providers)
        if provider is not None:
            return provider
    return None


def _agent_from_command_segment(segment: str, command_providers: dict[str, str]) -> str | None:
    tokens = _command_tokens(segment)
    while tokens:
        token = tokens.pop(0)
        if _ENV_ASSIGNMENT_PATTERN.match(token):
            continue
        if token in _COMMAND_WRAPPERS:
            continue
        return command_providers.get(token)
    return None


def _command_tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment.strip())
    except ValueError:
        return segment.strip().split()


def runtime_tags_for_window(
    window: VirtualWindow,
    *,
    ai_session: AiSession | None = None,
    terminal_agent: str | None = None,
) -> list[str]:
    tags: list[str] = []
    provider = ai_session.provider if ai_session is not None else terminal_agent
    if provider is not None and _is_registered_provider(provider):
        tags.append(provider)

    path = ai_session.project_path if ai_session is not None and ai_session.project_path else window.cwd
    if path:
        tags.append(path)

    return _dedupe_tags(tags)


def _is_registered_provider(provider: str) -> bool:
    try:
        get_agent_tool_registry().by_provider(provider)
    except ValueError:
        return False
    return True


def _dedupe_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_tags: list[str] = []
    for tag in tags:
        normalized = tag.strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        unique_tags.append(normalized)
    return unique_tags
