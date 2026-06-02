from __future__ import annotations

import os
import re
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


def shell_loop_items(command: str, variable: str) -> set[str]:
    match = re.search(rf"for {re.escape(variable)} in ([^;]+); do", command)
    assert match is not None
    return set(match.group(1).split())


def test_bash_managed_shell_command_contains_command_capture_hook() -> None:
    managed = build_managed_shell_command(
        shell="/bin/bash",
        client_id=CLIENT_ID,
        window_id=WINDOW_ID,
        server_url=SERVER_URL,
        project_path="/workspace/project",
    )

    assert managed.command_capture_supported is True
    assert managed.hook_script is not None
    assert 'PATH="$HOME/.web-terminal-acp/npm-global/bin:$PATH"' in managed.command
    assert "WEB_TERMINAL_CLIENT_ID=12345678-1234-5678-1234-567812345678" in managed.command
    assert "WEB_TERMINAL_WINDOW_ID=87654321-4321-8765-4321-876543218765" in managed.command
    assert "WEB_TERMINAL_SERVER_URL=https://control.example.com" in managed.command
    assert "WEB_TERMINAL_COMMAND_HOOK=1" in managed.command
    assert "WEB_TERMINAL_PROJECT_PATH=/workspace/project" in managed.command
    assert "WEB_TERMINAL_CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CLAUDE_CODE_HOME='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CURSOR_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_ANTIGRAVITY_HOME='~/.web-terminal-acp/antigravity-cli-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_ANTIGRAVITY_COMMAND_HOME='~/.web-terminal-acp/antigravity-cli-homes/.managed-home/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_ORIGINAL_CODEX_HOME='~/.codex'" in managed.command
    assert "WEB_TERMINAL_ORIGINAL_CLAUDE_CODE_HOME='~/.claude'" in managed.command
    assert "WEB_TERMINAL_ORIGINAL_ANTIGRAVITY_CLI_HOME='~/.gemini/antigravity-cli'" in managed.command
    assert " CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" not in managed.command
    assert not managed.command.startswith("CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'")
    assert "CLAUDE_CONFIG_DIR='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "CURSOR_AGENT_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_BASH_RC_GZ" in managed.command
    assert len(managed.command) < 8000
    hook_script = managed.hook_script
    assert '__web_terminal_source_codex_home="${WEB_TERMINAL_ORIGINAL_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}"' in hook_script
    assert '__web_terminal_source_codex_home="$HOME/${__web_terminal_source_codex_home#"~/"}"' in hook_script
    assert "export CODEX_HOME=\"$WEB_TERMINAL_CODEX_HOME\"" in hook_script
    assert shell_loop_items(hook_script, "__web_terminal_codex_item") == {
        "auth.json",
        "config.toml",
        "AGENTS.md",
        "skills",
        "skills.disabled",
        "plugin_marketplaces.json",
        "hooks",
        "hooks.json",
        "hooks.disabled.json",
        "plugins",
        "plugins.disabled",
    }
    assert "for __web_terminal_codex_history_item in history.json history.jsonl" in hook_script
    assert "export CLAUDE_CONFIG_DIR=\"$WEB_TERMINAL_CLAUDE_CODE_HOME\"" in hook_script
    assert '__web_terminal_source_claude_home="${WEB_TERMINAL_ORIGINAL_CLAUDE_CODE_HOME:-$HOME/.claude}"' in hook_script
    assert '__web_terminal_source_claude_home="$HOME/${__web_terminal_source_claude_home#"~/"}"' in hook_script
    assert "__web_terminal_source_claude_json=\"${WEB_TERMINAL_ORIGINAL_CLAUDE_JSON:-$HOME/.claude.json}\"" in hook_script
    assert '__web_terminal_source_claude_json="$HOME/${__web_terminal_source_claude_json#"~/"}"' in hook_script
    assert "ln -s \"$__web_terminal_source_claude_json\" \"$WEB_TERMINAL_CLAUDE_CODE_HOME/.claude.json\"" in hook_script
    assert shell_loop_items(hook_script, "__web_terminal_claude_item") == {
        "settings.json",
        "settings.local.json",
        "commands",
        "skills",
        "skills.disabled",
        "api-key-helper.sh",
        "hooks",
        "hooks.json",
        "hooks.disabled.json",
        "plugins",
        "plugins.disabled",
    }
    assert "for __web_terminal_claude_history_item in history.json history.jsonl file-history" in hook_script
    assert "__web_terminal_load_claude_settings_env \"$__web_terminal_source_claude_home/settings.json\"" in hook_script
    assert "json.load(open(sys.argv[1], encoding=\"utf-8\")).get(\"env\", {})" in hook_script
    assert "__web_terminal_prepare_claude_code_home" in hook_script
    assert "export CURSOR_AGENT_HOME=\"$managed_cursor\"" in hook_script
    assert "export CURSOR_CONFIG_DIR=\"$managed_cursor\"" in hook_script
    assert "export CURSOR_DATA_DIR=\"$managed_cursor\"" in hook_script
    assert "__web_terminal_prepare_agent_homes" in hook_script
    assert "__web_terminal_prepare_agent_command_path" in hook_script
    assert "__web_terminal_prepend_path_once \"~/.local/bin\"" in hook_script
    assert "__web_terminal_prepend_path_once \"~/.npm-global/bin\"" in hook_script
    assert "__web_terminal_prepend_path_once \"~/.bun/bin\"" in hook_script
    assert '__web_terminal_prepend_path_once "~/.web-terminal-acp/npm-global/bin"' in hook_script
    assert "__web_terminal_load_user_shell_env" in hook_script
    assert "__web_terminal_load_zshrc_env" in hook_script
    assert "__web_terminal_missing_claude_env" in hook_script
    assert '[ -n "${ANTHROPIC_BASE_URL:-}" ] || [ -n "${CLAUDE_CODE_API_BASE_URL:-}" ] || return 0' in hook_script
    assert "if __web_terminal_missing_claude_env; then" in hook_script
    assert "zsh -ic" in hook_script
    assert "ANTHROPIC_*=*|CLAUDE_CODE_*=*" in hook_script
    assert "CLAUDE_CONFIG_DIR=*" not in hook_script
    assert "__web_terminal_prepare_cursor_home" in hook_script
    assert "__web_terminal_prepare_antigravity_home" in hook_script
    assert "alias agy-p=" in hook_script
    assert "__web_terminal_run_agent_command_with_permission agy-p --dangerously-skip-permissions" in hook_script
    assert "__web_terminal_install_agent_permission_wrappers" in hook_script
    assert "__web_terminal_run_agent_command_with_permission codex --dangerously-bypass-approvals-and-sandbox" in hook_script
    assert "__web_terminal_run_agent_command_with_permission claude --dangerously-skip-permissions" in hook_script
    assert "command agent" in hook_script
    assert "command cursor" in hook_script
    assert "command \"$__web_terminal_command_name\"" in hook_script
    assert "CURSOR_CONFIG_DIR=" in hook_script
    assert "CURSOR_DATA_DIR=" in hook_script
    assert "PROMPT_COMMAND" in hook_script
    assert "__web_terminal_start_bash_command" in hook_script
    assert " DEBUG" in hook_script
    assert "__web_terminal_last_history_id" in hook_script
    assert "__web_terminal_finish_bash_command" in hook_script
    assert "__web_terminal_should_capture_command" in hook_script
    assert "WEB_TERMINAL_AUTO_RESUME=1" in hook_script
    assert "WEB_TERMINAL_CAPTURED_CWD" in hook_script
    assert "web-terminal-command" in hook_script
    assert "Ptmux" in hook_script
    assert "phase" in hook_script
    assert "payload=" in hook_script
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
    assert managed.hook_script is not None
    assert 'PATH="$HOME/.web-terminal-acp/npm-global/bin:$PATH"' in managed.command
    assert "WEB_TERMINAL_COMMAND_HOOK=1" in managed.command
    assert "WEB_TERMINAL_PROJECT_PATH=/workspace/project" in managed.command
    assert "WEB_TERMINAL_CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CLAUDE_CODE_HOME='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_CURSOR_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_ANTIGRAVITY_HOME='~/.web-terminal-acp/antigravity-cli-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_ORIGINAL_CODEX_HOME='~/.codex'" in managed.command
    assert "WEB_TERMINAL_ORIGINAL_CLAUDE_CODE_HOME='~/.claude'" in managed.command
    assert " CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'" not in managed.command
    assert not managed.command.startswith("CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'")
    assert "CLAUDE_CONFIG_DIR='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "CURSOR_AGENT_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'" in managed.command
    assert "WEB_TERMINAL_ZSH_RC_GZ" in managed.command
    assert len(managed.command) < 8000
    hook_script = managed.hook_script
    assert '__web_terminal_source_codex_home="${WEB_TERMINAL_ORIGINAL_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}"' in hook_script
    assert "export CODEX_HOME=\"$WEB_TERMINAL_CODEX_HOME\"" in hook_script
    assert "__web_terminal_prepare_claude_code_home" in hook_script
    assert "__web_terminal_prepare_agent_homes" in hook_script
    assert "__web_terminal_prepare_agent_command_path" in hook_script
    assert "__web_terminal_prepare_cursor_home" in hook_script
    assert "__web_terminal_prepare_antigravity_home" in hook_script
    assert "__web_terminal_install_agent_permission_wrappers" in hook_script
    assert "__web_terminal_run_agent_command_with_permission codex --dangerously-bypass-approvals-and-sandbox" in hook_script
    assert "__web_terminal_run_agent_command_with_permission claude --dangerously-skip-permissions" in hook_script
    assert "CURSOR_CONFIG_DIR=" in hook_script
    assert "CURSOR_DATA_DIR=" in hook_script
    assert "preexec()" in hook_script
    assert "precmd()" in hook_script
    assert "__web_terminal_pending_command" in hook_script
    assert "__web_terminal_emit_command_marker started zsh" in hook_script
    assert "__web_terminal_emit_command_marker finished zsh" in hook_script
    assert "__web_terminal_should_capture_command" in hook_script
    assert "WEB_TERMINAL_AUTO_RESUME=1" in hook_script
    assert "WEB_TERMINAL_CAPTURED_CWD" in hook_script
    assert "web-terminal-command" in hook_script
    assert "Ptmux" in hook_script
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
        "WEB_TERMINAL_ORIGINAL_CLAUDE_JSON": "~/.claude.json",
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
    assert managed.command.startswith('PATH="$HOME/.web-terminal-acp/npm-global/bin:$PATH" ')
    assert "/bin/sh -c " in managed.command
    for assignment in (
        "WEB_TERMINAL_CLIENT_ID=12345678-1234-5678-1234-567812345678",
        "WEB_TERMINAL_WINDOW_ID=87654321-4321-8765-4321-876543218765",
        "WEB_TERMINAL_SERVER_URL=https://control.example.com",
        "WEB_TERMINAL_COMMAND_HOOK=1",
        "WEB_TERMINAL_PROJECT_PATH=/workspace/project",
        "WEB_TERMINAL_CODEX_HOME='~/.web-terminal-acp/codex-homes/87654321-4321-8765-4321-876543218765'",
        "WEB_TERMINAL_CLAUDE_CODE_HOME='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'",
        "WEB_TERMINAL_CURSOR_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'",
        "WEB_TERMINAL_ANTIGRAVITY_HOME='~/.web-terminal-acp/antigravity-cli-homes/87654321-4321-8765-4321-876543218765'",
        "WEB_TERMINAL_ANTIGRAVITY_COMMAND_HOME='~/.web-terminal-acp/antigravity-cli-homes/.managed-home/87654321-4321-8765-4321-876543218765'",
        "WEB_TERMINAL_ORIGINAL_CODEX_HOME='~/.codex'",
        "WEB_TERMINAL_ORIGINAL_CLAUDE_CODE_HOME='~/.claude'",
        "WEB_TERMINAL_ORIGINAL_CLAUDE_JSON='~/.claude.json'",
        "WEB_TERMINAL_ORIGINAL_CURSOR_DIR='~/.cursor'",
        "WEB_TERMINAL_ORIGINAL_ANTIGRAVITY_CLI_HOME='~/.gemini/antigravity-cli'",
        "WEB_TERMINAL_ORIGINAL_HOME='~'",
        "CLAUDE_CONFIG_DIR='~/.web-terminal-acp/claude-code-homes/87654321-4321-8765-4321-876543218765'",
        "CURSOR_AGENT_HOME='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'",
        "CURSOR_CONFIG_DIR='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'",
        "CURSOR_DATA_DIR='~/.web-terminal-acp/cursor-homes/87654321-4321-8765-4321-876543218765'",
    ):
        assert assignment in managed.command
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
    for command_name in ("codex", "claude", "agent", "agy-p"):
        executable = bin_dir / command_name
        executable.write_text(
            "#!/bin/sh\n"
            f"printf '{command_name}:%s:%s:%s\\n' \"$1\" \"$OPENAI_API_KEY\" \"$HOME\"\n",
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
            f"codex:--dangerously-bypass-approvals-and-sandbox:codex-key:{home}",
        ),
        ("claude --version", f"claude:--dangerously-skip-permissions:codex-key:{home}"),
        ("agent --version", f"agent:--version:codex-key:{home}"),
        (
            "agy-p --version",
            "agy-p:--dangerously-skip-permissions:codex-key:"
            f"{home}/.web-terminal-acp/antigravity-cli-homes/.managed-home/{WINDOW_ID}",
        ),
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
    (home / ".claude.json").write_text("{}", encoding="utf-8")
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
        ".gemini/antigravity-cli": [
            "settings.json",
            "keybindings.json",
            "hooks",
            "hooks.json",
            "hooks.disabled.json",
            "plugins",
            "plugins.disabled",
            "skills",
            "skills.disabled",
            "history.jsonl",
            "antigravity-oauth-token",
            "installation_id",
            "brain",
            "cache",
            "log",
            "scratch",
        ],
    }
    directory_names = {
        ".codex": {"hooks", "skills", "skills.disabled", "plugins", "plugins.disabled"},
        ".claude": {"commands", "hooks", "plugins", "plugins.disabled", "skills", "skills.disabled"},
        ".cursor": {"hooks", "plugins", "plugins.disabled", "skills-cursor", "skills-cursor.disabled"},
        ".gemini/antigravity-cli": {
            "hooks",
            "plugins",
            "plugins.disabled",
            "skills",
            "skills.disabled",
            "brain",
            "cache",
            "log",
            "scratch",
        },
    }
    for root_name, item_names in source_items.items():
        root = home / root_name
        root.mkdir(parents=True)
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
    (home / ".gemini" / "antigravity-cli" / "cache" / "onboarding.json").write_text('{"theme": "dark", "agreed": true}', encoding="utf-8")

    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "WEB_TERMINAL_CODEX_HOME": "~/.web-terminal-acp/codex-homes/window-1",
        "WEB_TERMINAL_CLAUDE_CODE_HOME": "~/.web-terminal-acp/claude-code-homes/window-1",
        "WEB_TERMINAL_ORIGINAL_CLAUDE_JSON": "~/.claude.json",
        "WEB_TERMINAL_CURSOR_HOME": "~/.web-terminal-acp/cursor-homes/window-1",
        "WEB_TERMINAL_ANTIGRAVITY_HOME": "~/.web-terminal-acp/antigravity-cli-homes/window-1",
        "WEB_TERMINAL_ANTIGRAVITY_COMMAND_HOME": "~/.web-terminal-acp/antigravity-cli-homes/.managed-home/window-1",
        "WEB_TERMINAL_ORIGINAL_ANTIGRAVITY_CLI_HOME": "~/.gemini/antigravity-cli",
        "WEB_TERMINAL_ORIGINAL_HOME": "~",
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
    antigravity_home = home / ".web-terminal-acp" / "antigravity-cli-homes" / "window-1"
    antigravity_command_home = (
        home / ".web-terminal-acp" / "antigravity-cli-homes" / ".managed-home" / "window-1"
    )

    assert (codex_home / "sessions").is_dir()
    assert (codex_home / "log").is_dir()
    assert (codex_home / "shell_snapshots").is_dir()
    assert (claude_home / "projects").is_dir()
    assert (cursor_home / "chats").is_dir()
    assert antigravity_home.is_dir()
    assert (antigravity_command_home / ".gemini" / "antigravity-cli").resolve() == antigravity_home
    assert not (cursor_home / "chats").is_symlink()
    assert not (cursor_home / "chats" / "marker").exists()

    for item_name in source_items[".codex"]:
        assert (codex_home / item_name).resolve() == home / ".codex" / item_name
    for item_name in source_items[".claude"]:
        assert (claude_home / item_name).resolve() == home / ".claude" / item_name
    assert (claude_home / ".claude.json").resolve() == home / ".claude.json"
    assert (claude_home / "file-history").resolve() == home / ".claude" / "file-history"
    for item_name in source_items[".cursor"]:
        assert (cursor_home / item_name).resolve() == home / ".cursor" / item_name
    for item_name in source_items[".gemini/antigravity-cli"]:
        target = antigravity_home / item_name
        if item_name in {"brain", "log", "scratch"}:
            assert not target.exists()
        elif item_name == "cache":
            assert target.is_dir()
            assert not (target / "marker").exists()
            assert (target / "onboarding.json").read_text(encoding="utf-8") == '{"theme": "dark", "agreed": true}'
        else:
            assert target.resolve() == home / ".gemini" / "antigravity-cli" / item_name


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
        "WEB_TERMINAL_ANTIGRAVITY_HOME": "~/.web-terminal-acp/antigravity-cli-homes/window-1",
        "WEB_TERMINAL_ANTIGRAVITY_COMMAND_HOME": "~/.web-terminal-acp/antigravity-cli-homes/.managed-home/window-1",
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


def test_agent_environment_allows_client_without_agent_installs_under_zsh(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "WEB_TERMINAL_CODEX_HOME": "~/.web-terminal-acp/codex-homes/window-1",
        "WEB_TERMINAL_CLAUDE_CODE_HOME": "~/.web-terminal-acp/claude-code-homes/window-1",
        "WEB_TERMINAL_CURSOR_HOME": "~/.web-terminal-acp/cursor-homes/window-1",
        "WEB_TERMINAL_ANTIGRAVITY_HOME": "~/.web-terminal-acp/antigravity-cli-homes/window-1",
        "WEB_TERMINAL_ANTIGRAVITY_COMMAND_HOME": "~/.web-terminal-acp/antigravity-cli-homes/.managed-home/window-1",
        "CODEX_HOME": "",
    }
    result = subprocess.run(
        ["zsh", "-c", _agent_environment_script()],
        check=False,
        env=env,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert (home / ".web-terminal-acp" / "codex-homes" / "window-1" / "sessions").is_dir()
    assert (home / ".web-terminal-acp" / "claude-code-homes" / "window-1" / "projects").is_dir()
    assert (home / ".web-terminal-acp" / "cursor-homes" / "window-1" / "chats").is_dir()
    assert (home / ".web-terminal-acp" / "antigravity-cli-homes" / "window-1").is_dir()
