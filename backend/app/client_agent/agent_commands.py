from __future__ import annotations

import posixpath
import re
import shlex

CODEX_BYPASS_APPROVALS_AND_SANDBOX_FLAG = "--dangerously-bypass-approvals-and-sandbox"
CLAUDE_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"

_AGENT_PERMISSION_FLAGS = {
    "codex": CODEX_BYPASS_APPROVALS_AND_SANDBOX_FLAG,
    "claude": CLAUDE_SKIP_PERMISSIONS_FLAG,
    "cursor": None,
    "agent": None,
}
_ENV_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_COMMAND_WRAPPERS = {"command", "env", "sudo"}


def agent_permission_flag(command_name: str | None) -> str | None:
    if not command_name:
        return None
    return _AGENT_PERMISSION_FLAGS.get(posixpath.basename(command_name))


def is_known_agent_command(command_name: str | None) -> bool:
    if not command_name:
        return False
    return posixpath.basename(command_name) in _AGENT_PERMISSION_FLAGS


def format_agent_command(command_name: str, *args: str) -> str:
    tokens = _tokens_with_agent_permission_flag([command_name, *args])
    return " ".join(shlex.quote(token) for token in tokens)


def agent_command_with_permission_flag(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None

    updated = _tokens_with_agent_permission_flag(tokens)
    if updated == tokens and _agent_command_index(tokens) is None:
        return None
    if updated == tokens and " " not in command.strip():
        return None
    return " ".join(shlex.quote(token) for token in updated)


def agent_command_for_interactive_shell(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or _agent_command_index(tokens) is None:
        return None
    return " ".join(shlex.quote(token) for token in _tokens_with_agent_permission_flag(tokens))


def _tokens_with_agent_permission_flag(tokens: list[str]) -> list[str]:
    command_index = _agent_command_index(tokens)
    if command_index is None:
        return tokens

    flag = agent_permission_flag(tokens[command_index])
    if flag is None or flag in tokens[command_index + 1 :]:
        return tokens

    return [*tokens[: command_index + 1], flag, *tokens[command_index + 1 :]]


def _agent_command_index(tokens: list[str]) -> int | None:
    for index, token in enumerate(tokens):
        if _ENV_ASSIGNMENT_PATTERN.match(token):
            continue
        if token in _COMMAND_WRAPPERS:
            continue
        if is_known_agent_command(token):
            return index
        return None
    return None
