import pytest

from app.services.window_runtime_tags import agent_command_has_inline_task, agent_from_command


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


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("codex", False),
        ("claude", False),
        ("agent", False),
        ("codex resume abc", False),
        ("codex exec 'fix tests'", True),
        ("codex -m gpt-5 exec 'fix tests'", True),
        ("codex --prompt 'fix tests'", True),
        ("claude -p 'fix tests'", True),
        ("agent 'fix tests'", True),
        ("command cursor --model gpt-5 'fix tests'", True),
    ],
)
def test_agent_command_has_inline_task_distinguishes_empty_launch(
    command: str, expected: bool
) -> None:
    assert agent_command_has_inline_task(command) is expected
