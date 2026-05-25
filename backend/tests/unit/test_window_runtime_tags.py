import pytest

from app.services.window_runtime_tags import agent_from_command


@pytest.mark.parametrize(
    ("command", "provider"),
    [
        ("codex", "codex"),
        ("env CLAUDE_CONFIG_DIR=/tmp/claude-home claude", "claude_code"),
        ("agent -p 'fix tests'", "cursor_cli"),
        ("command cursor --model gpt-5", "cursor_cli"),
        ("sudo codex exec 'fix tests'", "codex"),
        ("npm test && FOO=bar cursor", "cursor_cli"),
        ("npm test && agent resume", "cursor_cli"),
        ("env FOO=bar command claude --continue", "claude_code"),
    ],
)
def test_agent_from_command_detects_registered_agent_tools(command: str, provider: str) -> None:
    assert agent_from_command(command) == provider


@pytest.mark.parametrize(
    "command",
    [
        None,
        "",
        "pytest backend/tests",
        "echo claude",
        "npx cursor-agent",
    ],
)
def test_agent_from_command_ignores_non_agent_commands(command: str | None) -> None:
    assert agent_from_command(command) is None
