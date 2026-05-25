from __future__ import annotations

from uuid import UUID

from app.client_agent.shell_hook import build_managed_shell_command

CLIENT_ID = UUID("12345678-1234-5678-1234-567812345678")
WINDOW_ID = UUID("87654321-4321-8765-4321-876543218765")
SERVER_URL = "https://control.example.com"


def test_bash_managed_shell_command_contains_command_capture_hook() -> None:
    managed = build_managed_shell_command(
        shell="/bin/bash",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        server_url=SERVER_URL,
        project_path="/workspace/project",
    )

    assert managed.command_capture_supported is True
    assert "WEB_TERMINAL_CLIENT_ID=12345678-1234-5678-1234-567812345678" in managed.command
    assert "WEB_TERMINAL_WINDOW_ID=87654321-4321-8765-4321-876543218765" in managed.command
    assert "WEB_TERMINAL_SERVER_URL=https://control.example.com" in managed.command
    assert "WEB_TERMINAL_COMMAND_HOOK=1" in managed.command
    assert "WEB_TERMINAL_PROJECT_PATH=/workspace/project" in managed.command
    assert "WEB_TERMINAL_CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CLAUDE_CODE_HOME='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CURSOR_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert " CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" not in managed.command
    assert not managed.command.startswith("CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'")
    assert "CLAUDE_CONFIG_DIR='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "CURSOR_AGENT_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert '__web_terminal_source_codex_home="${WEB_TERMINAL_ORIGINAL_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}"' in managed.command
    assert "export CODEX_HOME=\"$WEB_TERMINAL_CODEX_HOME\"" in managed.command
    assert "export CLAUDE_CONFIG_DIR=\"$WEB_TERMINAL_CLAUDE_CODE_HOME\"" in managed.command
    assert "export CURSOR_AGENT_HOME=\"$WEB_TERMINAL_CURSOR_HOME\"" in managed.command
    assert "__web_terminal_prepare_agent_homes" in managed.command
    assert "__web_terminal_prepare_cursor_home" in managed.command
    assert "CURSOR_CONFIG_DIR=" in managed.command
    assert "CURSOR_DATA_DIR=" in managed.command
    assert "PROMPT_COMMAND" in managed.command
    assert "__web_terminal_start_bash_command" in managed.command
    assert " DEBUG" in managed.command
    assert "__web_terminal_last_history_id" in managed.command
    assert "__web_terminal_finish_bash_command" in managed.command
    assert "WEB_TERMINAL_CAPTURED_CWD" in managed.command
    assert "web-terminal-command" in managed.command
    assert "phase" in managed.command
    assert "payload=" in managed.command
    assert "exec /bin/bash" in managed.command


def test_zsh_managed_shell_command_contains_preexec_command_capture_hook() -> None:
    managed = build_managed_shell_command(
        shell="/bin/zsh",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        server_url=SERVER_URL,
        project_path="/workspace/project",
    )

    assert managed.command_capture_supported is True
    assert "WEB_TERMINAL_COMMAND_HOOK=1" in managed.command
    assert "WEB_TERMINAL_PROJECT_PATH=/workspace/project" in managed.command
    assert "WEB_TERMINAL_CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CLAUDE_CODE_HOME='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CURSOR_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert " CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" not in managed.command
    assert not managed.command.startswith("CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'")
    assert "CLAUDE_CONFIG_DIR='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "CURSOR_AGENT_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert '__web_terminal_source_codex_home="${WEB_TERMINAL_ORIGINAL_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}"' in managed.command
    assert "export CODEX_HOME=\"$WEB_TERMINAL_CODEX_HOME\"" in managed.command
    assert "__web_terminal_prepare_agent_homes" in managed.command
    assert "__web_terminal_prepare_cursor_home" in managed.command
    assert "CURSOR_CONFIG_DIR=" in managed.command
    assert "CURSOR_DATA_DIR=" in managed.command
    assert "preexec()" in managed.command
    assert "precmd()" in managed.command
    assert "__web_terminal_pending_command" in managed.command
    assert "__web_terminal_emit_command_marker started zsh" in managed.command
    assert "__web_terminal_emit_command_marker finished zsh" in managed.command
    assert "WEB_TERMINAL_CAPTURED_CWD" in managed.command
    assert "web-terminal-command" in managed.command
    assert "exec /bin/zsh" in managed.command


def test_unsupported_shell_returns_fallback_command_and_unsupported_flag() -> None:
    managed = build_managed_shell_command(
        shell="/usr/bin/fish",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        server_url=SERVER_URL,
        project_path="/workspace/project",
    )

    assert managed.command_capture_supported is False
    assert managed.command == (
        "WEB_TERMINAL_CLIENT_ID=12345678-1234-5678-1234-567812345678 "
        "WEB_TERMINAL_WINDOW_ID=87654321-4321-8765-4321-876543218765 "
        "WEB_TERMINAL_SERVER_URL=https://control.example.com "
        "WEB_TERMINAL_COMMAND_HOOK=1 "
        "WEB_TERMINAL_PROJECT_PATH=/workspace/project "
        "WEB_TERMINAL_CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765' "
        "WEB_TERMINAL_CLAUDE_CODE_HOME='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765' "
        "WEB_TERMINAL_CURSOR_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765' "
        "WEB_TERMINAL_ORIGINAL_CURSOR_DIR='~/.cursor' "
        "CLAUDE_CONFIG_DIR='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765' "
        "CURSOR_AGENT_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765' "
        "CURSOR_CONFIG_DIR='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765' "
        "CURSOR_DATA_DIR='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765' "
        "exec /usr/bin/fish"
    )
    assert "web-terminal-command" not in managed.command
