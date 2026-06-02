from __future__ import annotations

from app.client_agent.agent_commands import (
    agent_command_for_interactive_shell,
    agent_command_with_permission_flag,
    format_agent_command,
)


def test_format_agent_command_adds_codex_permission_flag_before_resume_args() -> None:
    assert (
        format_agent_command("codex", "resume", "codex-session")
        == "codex --dangerously-bypass-approvals-and-sandbox resume codex-session"
    )


def test_format_agent_command_adds_claude_permission_flag_before_resume_args() -> None:
    assert (
        format_agent_command("claude", "--resume", "claude-session")
        == "claude --dangerously-skip-permissions --resume claude-session"
    )


def test_agent_command_with_permission_flag_normalizes_direct_start_commands() -> None:
    assert (
        agent_command_with_permission_flag("codex exec 'fix tests'")
        == "codex --dangerously-bypass-approvals-and-sandbox exec 'fix tests'"
    )
    assert (
        agent_command_with_permission_flag("claude --resume claude-session")
        == "claude --dangerously-skip-permissions --resume claude-session"
    )


def test_agent_command_with_permission_flag_handles_wrappers_and_absolute_paths() -> None:
    assert (
        agent_command_with_permission_flag("env FOO=bar command /usr/local/bin/codex")
        == "env FOO=bar command /usr/local/bin/codex --dangerously-bypass-approvals-and-sandbox"
    )
    assert (
        agent_command_with_permission_flag("sudo claude --model sonnet")
        == "sudo claude --dangerously-skip-permissions --model sonnet"
    )


def test_agent_command_with_permission_flag_does_not_duplicate_existing_flags() -> None:
    assert (
        agent_command_with_permission_flag(
            "codex --dangerously-bypass-approvals-and-sandbox resume codex-session"
        )
        == "codex --dangerously-bypass-approvals-and-sandbox resume codex-session"
    )
    assert (
        agent_command_with_permission_flag("claude --dangerously-skip-permissions")
        == "claude --dangerously-skip-permissions"
    )


def test_agent_command_with_permission_flag_ignores_non_agent_commands() -> None:
    assert agent_command_with_permission_flag("/bin/bash") is None
    assert agent_command_with_permission_flag("echo codex") is None


def test_agent_command_with_permission_flag_preserves_cursor_agent_arguments() -> None:
    assert agent_command_with_permission_flag("agent") is None
    assert agent_command_with_permission_flag("cursor") is None
    assert (
        agent_command_with_permission_flag("agent --resume cursor-session")
        == "agent --resume cursor-session"
    )
    assert agent_command_with_permission_flag("cursor --reuse-window") == "cursor --reuse-window"


def test_antigravity_agent_command_uses_permission_flag() -> None:
    assert (
        format_agent_command("agy-p", "--version")
        == "agy-p --dangerously-skip-permissions --version"
    )
    assert (
        agent_command_with_permission_flag("agy --prompt 'fix tests'")
        == "agy --dangerously-skip-permissions --prompt 'fix tests'"
    )


def test_agent_command_for_interactive_shell_detects_direct_agent_commands() -> None:
    assert (
        agent_command_for_interactive_shell("codex")
        == "codex --dangerously-bypass-approvals-and-sandbox"
    )
    assert (
        agent_command_for_interactive_shell("claude --resume claude-session")
        == "claude --dangerously-skip-permissions --resume claude-session"
    )
    assert agent_command_for_interactive_shell("agent") == "agent"
    assert (
        agent_command_for_interactive_shell("agy-p --version")
        == "agy-p --dangerously-skip-permissions --version"
    )
    assert agent_command_for_interactive_shell("/bin/bash") is None
