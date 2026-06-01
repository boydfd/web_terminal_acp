from __future__ import annotations

import re
import shlex

from app.agent_tools import get_agent_tool_registry
from app.models import AiSession, VirtualWindow

_COMMAND_SEGMENT_PATTERN = re.compile(r"&&|\|\||[;|]")
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_COMMAND_WRAPPERS = {"command", "env", "sudo"}
_INLINE_PROMPT_FLAGS = {"-p", "--print", "--prompt", "--message"}
_FLAGS_WITH_VALUE = {
    "-c",
    "-m",
    "-p",
    "--config",
    "--model",
    "--print",
    "--profile",
    "--prompt",
    "--message",
}
_CODEX_NON_TASK_SUBCOMMANDS = {
    "auth",
    "completion",
    "debug",
    "help",
    "login",
    "logout",
    "mcp",
    "resume",
    "sandbox",
}


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


def agent_command_has_inline_task(command: str | None) -> bool:
    if not command:
        return False

    command_providers = {
        name: adapter.provider_id
        for adapter in get_agent_tool_registry().all()
        for name in adapter.command_names
    }
    for segment in _COMMAND_SEGMENT_PATTERN.split(command):
        parts = _agent_command_segment_parts(segment, command_providers)
        if parts is None:
            continue
        provider, args = parts
        return _agent_args_have_inline_task(provider, args)
    return False


def _agent_from_command_segment(segment: str, command_providers: dict[str, str]) -> str | None:
    parts = _agent_command_segment_parts(segment, command_providers)
    return parts[0] if parts is not None else None


def _agent_command_segment_parts(
    segment: str, command_providers: dict[str, str]
) -> tuple[str, list[str]] | None:
    tokens = _command_tokens(segment)
    while tokens:
        token = tokens.pop(0)
        if _ENV_ASSIGNMENT_PATTERN.match(token):
            continue
        if token in _COMMAND_WRAPPERS:
            continue
        provider = command_providers.get(token)
        return (provider, tokens) if provider is not None else None
    return None


def _command_tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment.strip())
    except ValueError:
        return segment.strip().split()


def _agent_args_have_inline_task(provider: str, args: list[str]) -> bool:
    prompt_flag = False

    def _mark_prompt_flag() -> None:
        nonlocal prompt_flag
        prompt_flag = True

    positionals = _agent_positionals(args, prompt_flag_callback=_mark_prompt_flag)
    if prompt_flag:
        return True
    if not positionals:
        return False
    if provider == "codex":
        first = positionals[0]
        if first == "exec":
            return len(positionals) > 1
        return first not in _CODEX_NON_TASK_SUBCOMMANDS
    return True


def _agent_positionals(args: list[str], *, prompt_flag_callback) -> list[str]:
    positionals: list[str] = []
    skip_next = False
    after_dashdash = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if after_dashdash:
            positionals.append(token)
            continue
        if token == "--":
            after_dashdash = True
            continue
        if token.startswith("--") and "=" in token:
            flag, value = token.split("=", 1)
            if flag in _INLINE_PROMPT_FLAGS and value:
                prompt_flag_callback()
            continue
        if token in _INLINE_PROMPT_FLAGS:
            prompt_flag_callback()
            skip_next = True
            continue
        if token in _FLAGS_WITH_VALUE:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        positionals.append(token)
    return positionals


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
