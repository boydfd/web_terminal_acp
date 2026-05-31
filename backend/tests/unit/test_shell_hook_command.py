from __future__ import annotations

import os
import subprocess
from uuid import UUID

from app.client_agent.shell_hook import (
    _agent_environment_script,
    _common_hook_script,
    build_managed_shell_command,
)

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
    assert 'PATH="$HOME/.web-terminal-acp/npm-global/bin:$PATH"' in managed.command
    assert "WEB_TERMINAL_CLIENT_ID=12345678-1234-5678-1234-567812345678" in managed.command
    assert "WEB_TERMINAL_WINDOW_ID=87654321-4321-8765-4321-876543218765" in managed.command
    assert "WEB_TERMINAL_SERVER_URL=https://control.example.com" in managed.command
    assert "WEB_TERMINAL_COMMAND_HOOK=1" in managed.command
    assert "WEB_TERMINAL_PROJECT_PATH=/workspace/project" in managed.command
    assert "WEB_TERMINAL_CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CLAUDE_CODE_HOME='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CURSOR_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_ORIGINAL_CODEX_HOME='~/.codex'" in managed.command
    assert "WEB_TERMINAL_ORIGINAL_CLAUDE_CODE_HOME='~/.claude'" in managed.command
    assert " CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" not in managed.command
    assert not managed.command.startswith("CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'")
    assert "CLAUDE_CONFIG_DIR='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "CURSOR_AGENT_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert '__web_terminal_source_codex_home="${WEB_TERMINAL_ORIGINAL_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}"' in managed.command
    assert '__web_terminal_source_codex_home="$HOME/${__web_terminal_source_codex_home#"~/"}"' in managed.command
    assert "export CODEX_HOME=\"$WEB_TERMINAL_CODEX_HOME\"" in managed.command
    assert "for __web_terminal_codex_item in auth.json config.toml hooks hooks.json hooks.disabled.json AGENTS.md skills skills.disabled plugins plugins.disabled plugin_marketplaces.json" in managed.command
    assert "for __web_terminal_codex_history_item in history.json history.jsonl" in managed.command
    assert "export CLAUDE_CONFIG_DIR=\"$WEB_TERMINAL_CLAUDE_CODE_HOME\"" in managed.command
    assert '__web_terminal_source_claude_home="${WEB_TERMINAL_ORIGINAL_CLAUDE_CODE_HOME:-$HOME/.claude}"' in managed.command
    assert '__web_terminal_source_claude_home="$HOME/${__web_terminal_source_claude_home#"~/"}"' in managed.command
    assert "__web_terminal_source_claude_json=\"${WEB_TERMINAL_ORIGINAL_CLAUDE_JSON:-$HOME/.claude.json}\"" in managed.command
    assert "ln -s \"$__web_terminal_source_claude_json\" \"$WEB_TERMINAL_CLAUDE_CODE_HOME/.claude.json\"" in managed.command
    assert "for __web_terminal_claude_item in settings.json settings.local.json commands hooks hooks.disabled.json plugins plugins.disabled skills skills.disabled api-key-helper.sh" in managed.command
    assert "for __web_terminal_claude_history_item in history.json history.jsonl file-history" in managed.command
    assert "__web_terminal_load_claude_settings_env \"$__web_terminal_source_claude_home/settings.json\"" in managed.command
    assert "json.load(open(sys.argv[1], encoding=\"utf-8\")).get(\"env\", {})" in managed.command
    assert "__web_terminal_prepare_claude_code_home" in managed.command
    assert "export CURSOR_AGENT_HOME=\"$WEB_TERMINAL_CURSOR_HOME\"" in managed.command
    assert "__web_terminal_prepare_agent_homes" in managed.command
    assert "__web_terminal_prepare_agent_command_path" in managed.command
    assert "__web_terminal_prepend_path_once \"~/.local/bin\"" in managed.command
    assert "__web_terminal_prepend_path_once \"~/.npm-global/bin\"" in managed.command
    assert "__web_terminal_prepend_path_once \"~/.bun/bin\"" in managed.command
    assert '__web_terminal_prepend_path_once "~/.web-terminal-acp/npm-global/bin"' in managed.command
    assert "__web_terminal_load_user_shell_env" in managed.command
    assert "__web_terminal_load_zshrc_env" in managed.command
    assert "__web_terminal_missing_claude_env" in managed.command
    assert '[ -n "${ANTHROPIC_BASE_URL:-}" ] || [ -n "${CLAUDE_CODE_API_BASE_URL:-}" ] || return 0' in managed.command
    assert "if __web_terminal_missing_claude_env; then" in managed.command
    assert "zsh -ic" in managed.command
    assert "ANTHROPIC_*=*|CLAUDE_CODE_*=*" in managed.command
    assert "CLAUDE_CONFIG_DIR=*" not in managed.command
    assert "__web_terminal_prepare_cursor_home" in managed.command
    assert "__web_terminal_install_agent_permission_wrappers" in managed.command
    assert "command codex --dangerously-bypass-approvals-and-sandbox" in managed.command
    assert "command claude --dangerously-skip-permissions" in managed.command
    assert "command agent" in managed.command
    assert "command cursor" in managed.command
    assert "CURSOR_CONFIG_DIR=" in managed.command
    assert "CURSOR_DATA_DIR=" in managed.command
    assert "PROMPT_COMMAND" in managed.command
    assert "__web_terminal_start_bash_command" in managed.command
    assert " DEBUG" in managed.command
    assert "__web_terminal_last_history_id" in managed.command
    assert "__web_terminal_finish_bash_command" in managed.command
    assert "__web_terminal_should_capture_command" in managed.command
    assert "WEB_TERMINAL_AUTO_RESUME=1" in managed.command
    assert "WEB_TERMINAL_CAPTURED_CWD" in managed.command
    assert "web-terminal-command" in managed.command
    assert "Ptmux" in managed.command
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
    assert 'PATH="$HOME/.web-terminal-acp/npm-global/bin:$PATH"' in managed.command
    assert "WEB_TERMINAL_COMMAND_HOOK=1" in managed.command
    assert "WEB_TERMINAL_PROJECT_PATH=/workspace/project" in managed.command
    assert "WEB_TERMINAL_CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CLAUDE_CODE_HOME='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CURSOR_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_ORIGINAL_CODEX_HOME='~/.codex'" in managed.command
    assert "WEB_TERMINAL_ORIGINAL_CLAUDE_CODE_HOME='~/.claude'" in managed.command
    assert " CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" not in managed.command
    assert not managed.command.startswith("CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'")
    assert "CLAUDE_CONFIG_DIR='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "CURSOR_AGENT_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert '__web_terminal_source_codex_home="${WEB_TERMINAL_ORIGINAL_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}"' in managed.command
    assert "export CODEX_HOME=\"$WEB_TERMINAL_CODEX_HOME\"" in managed.command
    assert "__web_terminal_prepare_claude_code_home" in managed.command
    assert "__web_terminal_prepare_agent_homes" in managed.command
    assert "__web_terminal_prepare_agent_command_path" in managed.command
    assert "__web_terminal_prepare_cursor_home" in managed.command
    assert "__web_terminal_install_agent_permission_wrappers" in managed.command
    assert "command codex --dangerously-bypass-approvals-and-sandbox" in managed.command
    assert "command claude --dangerously-skip-permissions" in managed.command
    assert "CURSOR_CONFIG_DIR=" in managed.command
    assert "CURSOR_DATA_DIR=" in managed.command
    assert "preexec()" in managed.command
    assert "precmd()" in managed.command
    assert "__web_terminal_pending_command" in managed.command
    assert "__web_terminal_emit_command_marker started zsh" in managed.command
    assert "__web_terminal_emit_command_marker finished zsh" in managed.command
    assert "__web_terminal_should_capture_command" in managed.command
    assert "WEB_TERMINAL_AUTO_RESUME=1" in managed.command
    assert "WEB_TERMINAL_CAPTURED_CWD" in managed.command
    assert "web-terminal-command" in managed.command
    assert "Ptmux" in managed.command
    assert "exec /bin/zsh" in managed.command


def test_common_command_capture_hook_emits_marker_without_python_311_datetime_utc(tmp_path) -> None:
    script = f"""
set -e
{_common_hook_script()}
__web_terminal_emit_command_marker started bash 7 "" pwd
"""
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "WEB_TERMINAL_WINDOW_ID": str(WINDOW_ID),
        "WEB_TERMINAL_CODEX_HOME": "~/.web-terminal-acp/codex-homes/window-1",
        "WEB_TERMINAL_CLAUDE_CODE_HOME": "~/.web-terminal-acp/claude-code-homes/window-1",
        "WEB_TERMINAL_CURSOR_HOME": "~/.web-terminal-acp/cursor-homes/window-1",
    }

    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        env=env,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "web-terminal-command" in result.stdout
    assert "payload=" in result.stdout
    assert "from datetime import UTC" not in script
    assert "timezone.utc" in script


def test_unsupported_shell_returns_fallback_command_and_unsupported_flag() -> None:
    managed = build_managed_shell_command(
        shell="/usr/bin/fish",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        server_url=SERVER_URL,
        project_path="/workspace/project",
    )

    assert managed.command_capture_supported is False
    assert managed.command.startswith(
        'PATH="$HOME/.web-terminal-acp/npm-global/bin:$PATH" '
        "WEB_TERMINAL_CLIENT_ID=12345678-1234-5678-1234-567812345678 "
        "WEB_TERMINAL_WINDOW_ID=87654321-4321-8765-4321-876543218765 "
        "WEB_TERMINAL_SERVER_URL=https://control.example.com "
        "WEB_TERMINAL_COMMAND_HOOK=1 "
        "WEB_TERMINAL_PROJECT_PATH=/workspace/project "
        "WEB_TERMINAL_CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765' "
        "WEB_TERMINAL_CLAUDE_CODE_HOME='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765' "
        "WEB_TERMINAL_CURSOR_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765' "
        "WEB_TERMINAL_ORIGINAL_CODEX_HOME='~/.codex' "
        "WEB_TERMINAL_ORIGINAL_CLAUDE_CODE_HOME='~/.claude' "
        "WEB_TERMINAL_ORIGINAL_CURSOR_DIR='~/.cursor' "
        "CLAUDE_CONFIG_DIR='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765' "
        "CURSOR_AGENT_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765' "
        "CURSOR_CONFIG_DIR='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765' "
        "CURSOR_DATA_DIR='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765' "
        "/bin/sh -c "
    )
    assert "__web_terminal_prepare_agent_homes" in managed.command
    assert "__web_terminal_load_zshrc_env" in managed.command
    assert "exec /usr/bin/fish" in managed.command
    assert "web-terminal-command" not in managed.command


def test_direct_codex_shell_command_adds_permission_flag() -> None:
    managed = build_managed_shell_command(
        shell="codex resume codex-session",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        server_url=SERVER_URL,
        project_path="/workspace/project",
    )

    assert managed.command_capture_supported is False
    assert "codex --dangerously-bypass-approvals-and-sandbox resume codex-session || __web_terminal_agent_exit=$?" in managed.command
    assert "agent command exited with status" in managed.command
    assert "__web_terminal_prepare_direct_agent_launch codex" in managed.command
    assert "__web_terminal_load_user_shell_env" in managed.command
    assert "__web_terminal_load_zshrc_env" in managed.command


def test_direct_claude_shell_command_adds_permission_flag() -> None:
    managed = build_managed_shell_command(
        shell="claude --resume claude-session",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        server_url=SERVER_URL,
        project_path="/workspace/project",
    )

    assert managed.command_capture_supported is False
    assert "claude --dangerously-skip-permissions --resume claude-session || __web_terminal_agent_exit=$?" in managed.command
    assert "agent command exited with status" in managed.command
    assert "__web_terminal_prepare_direct_agent_launch claude" in managed.command
    assert "__web_terminal_load_zshrc_env" in managed.command


def test_direct_cursor_agent_shell_command_preserves_arguments() -> None:
    managed = build_managed_shell_command(
        shell="agent --resume cursor-session",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        server_url=SERVER_URL,
        project_path="/workspace/project",
    )

    assert managed.command_capture_supported is False
    assert "agent --resume cursor-session || __web_terminal_agent_exit=$?" in managed.command
    assert "agent command exited with status" in managed.command
    assert "__web_terminal_prepare_direct_agent_launch agent" in managed.command


def test_direct_agent_commands_find_user_local_executables(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for command_name in ("codex", "claude", "agent"):
        executable = bin_dir / command_name
        executable.write_text(
            "#!/bin/sh\n"
            f"printf '{command_name}:%s:%s\\n' \"$1\" \"$OPENAI_API_KEY\"\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)

    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "OPENAI_API_KEY": "codex-key",
    }
    command_cases = [
        (
            "codex --version",
            "codex:--dangerously-bypass-approvals-and-sandbox:codex-key",
        ),
        ("claude --version", "claude:--dangerously-skip-permissions:codex-key"),
        ("agent --version", "agent:--version:codex-key"),
    ]

    for shell, expected in command_cases:
        managed = build_managed_shell_command(
            shell=shell,
            client_id=CLIENT_ID,
            window_id=WINDOW_ID,
            server_url=SERVER_URL,
        )
        result = subprocess.run(
            ["/bin/sh", "-c", managed.command],
            check=False,
            env=env,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert expected in result.stdout
        assert "agent command exited with status 0" in result.stdout


def test_agent_environment_prepares_per_window_agent_config_links(tmp_path) -> None:
    home = tmp_path
    source_items = {
        ".codex": [
            "auth.json",
            "config.toml",
            "hooks",
            "hooks.json",
            "hooks.disabled.json",
            "AGENTS.md",
            "skills",
            "skills.disabled",
            "plugins",
            "plugins.disabled",
            "plugin_marketplaces.json",
            "history.jsonl",
        ],
        ".claude": [
            "settings.json",
            "settings.local.json",
            "commands",
            "hooks",
            "hooks.disabled.json",
            "plugins",
            "plugins.disabled",
            "skills",
            "skills.disabled",
            "api-key-helper.sh",
            "history.jsonl",
        ],
        ".cursor": [
            "agent-cli-state.json",
            "cli-config.json",
            "hooks",
            "hooks.json",
            "hooks.disabled.json",
            "plugins",
            "plugins.disabled",
            "skills-cursor",
            "skills-cursor.disabled",
            "history.jsonl",
        ],
    }
    directory_names = {
        ".codex": {"hooks", "skills", "skills.disabled", "plugins", "plugins.disabled"},
        ".claude": {"commands", "hooks", "plugins", "plugins.disabled", "skills", "skills.disabled"},
        ".cursor": {"hooks", "plugins", "plugins.disabled", "skills-cursor", "skills-cursor.disabled"},
    }
    for root_name, item_names in source_items.items():
        root = home / root_name
        root.mkdir()
        for item_name in item_names:
            path = root / item_name
            if item_name in directory_names[root_name]:
                path.mkdir()
                (path / "marker").write_text(item_name, encoding="utf-8")
            else:
                path.write_text("{}", encoding="utf-8")
    (home / ".claude" / "file-history").mkdir()
    (home / ".claude" / "file-history" / "marker").write_text("file-history", encoding="utf-8")
    (home / ".cursor" / "chats").mkdir()
    (home / ".cursor" / "chats" / "marker").write_text("chats", encoding="utf-8")

    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "WEB_TERMINAL_CODEX_HOME": "~/.web-terminal-acp/codex-homes/window-1",
        "WEB_TERMINAL_CLAUDE_CODE_HOME": "~/.web-terminal-acp/claude-code-homes/window-1",
        "WEB_TERMINAL_CURSOR_HOME": "~/.web-terminal-acp/cursor-homes/window-1",
        "CODEX_HOME": "",
    }
    result = subprocess.run(
        ["bash", "-c", _agent_environment_script()],
        check=False,
        env=env,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    codex_home = home / ".web-terminal-acp" / "codex-homes" / "window-1"
    claude_home = home / ".web-terminal-acp" / "claude-code-homes" / "window-1"
    cursor_home = home / ".web-terminal-acp" / "cursor-homes" / "window-1"

    assert (codex_home / "sessions").is_dir()
    assert (codex_home / "log").is_dir()
    assert (codex_home / "shell_snapshots").is_dir()
    assert (claude_home / "projects").is_dir()
    assert (cursor_home / "chats").is_dir()
    assert not (cursor_home / "chats").is_symlink()
    assert not (cursor_home / "chats" / "marker").exists()

    for item_name in source_items[".codex"]:
        assert (codex_home / item_name).resolve() == home / ".codex" / item_name
    for item_name in source_items[".claude"]:
        assert (claude_home / item_name).resolve() == home / ".claude" / item_name
    assert (claude_home / "file-history").resolve() == home / ".claude" / "file-history"
    for item_name in source_items[".cursor"]:
        assert (cursor_home / item_name).resolve() == home / ".cursor" / item_name


def test_agent_environment_replaces_legacy_cursor_chats_symlink(tmp_path) -> None:
    home = tmp_path / "home"
    source_chats = home / ".cursor" / "chats"
    source_chats.mkdir(parents=True)
    cursor_home = home / ".web-terminal-acp" / "cursor-homes" / "window-1"
    cursor_home.mkdir(parents=True)
    (cursor_home / "chats").symlink_to(source_chats)

    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "WEB_TERMINAL_CODEX_HOME": "~/.web-terminal-acp/codex-homes/window-1",
        "WEB_TERMINAL_CLAUDE_CODE_HOME": "~/.web-terminal-acp/claude-code-homes/window-1",
        "WEB_TERMINAL_CURSOR_HOME": "~/.web-terminal-acp/cursor-homes/window-1",
        "CODEX_HOME": "",
    }
    result = subprocess.run(
        ["bash", "-c", _agent_environment_script()],
        check=False,
        env=env,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (cursor_home / "chats").is_dir()
    assert not (cursor_home / "chats").is_symlink()
