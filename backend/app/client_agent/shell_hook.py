from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from uuid import UUID

_SAFE_SHELL_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")


@dataclass(frozen=True)
class ManagedShellCommand:
    command: str
    command_capture_supported: bool


def build_managed_shell_command(
    *,
    shell: str,
    client_id: UUID | str,
    window_id: UUID | str,
    server_url: str,
    project_path: str | None = None,
) -> ManagedShellCommand:
    codex_home = f"~/.web-terminal-acp/codex-homes/{window_id}"
    claude_code_home = f"~/.web-terminal-acp/claude-code-homes/{window_id}"
    cursor_home = f"~/.web-terminal-acp/cursor-homes/{window_id}"
    storage_env = {
        "CLAUDE_CONFIG_DIR": claude_code_home,
        "CURSOR_AGENT_HOME": cursor_home,
        "CURSOR_CONFIG_DIR": cursor_home,
        "CURSOR_DATA_DIR": cursor_home,
    }
    env = {
        "WEB_TERMINAL_CLIENT_ID": str(client_id),
        "WEB_TERMINAL_WINDOW_ID": str(window_id),
        "WEB_TERMINAL_SERVER_URL": server_url,
        "WEB_TERMINAL_COMMAND_HOOK": "1",
        "WEB_TERMINAL_PROJECT_PATH": project_path or "",
        "WEB_TERMINAL_CODEX_HOME": codex_home,
        "WEB_TERMINAL_CLAUDE_CODE_HOME": claude_code_home,
        "WEB_TERMINAL_CURSOR_HOME": cursor_home,
        "WEB_TERMINAL_ORIGINAL_CURSOR_DIR": "~/.cursor",
        **storage_env,
    }
    assignments = " ".join(f"{key}={_shell_quote(value)}" for key, value in env.items())
    shell_name = posixpath.basename(shell)

    if shell_name == "bash":
        return ManagedShellCommand(
            command=f"{assignments} {_shell_quote(shell)} -lc {_shell_quote(_bash_launcher(shell))}",
            command_capture_supported=True,
        )
    if shell_name == "zsh":
        return ManagedShellCommand(
            command=f"{assignments} {_shell_quote(shell)} -fc {_shell_quote(_zsh_launcher(shell))}",
            command_capture_supported=True,
        )

    return ManagedShellCommand(
        command=f"{assignments} exec {_shell_quote(shell)}",
        command_capture_supported=False,
    )


def _bash_launcher(shell: str) -> str:
    quoted_shell = _shell_quote(shell)
    return f"""__web_terminal_rc=$(mktemp)
chmod 600 "$__web_terminal_rc"
cat > "$__web_terminal_rc" <<'WEB_TERMINAL_BASH_RC'
{_bash_hook_script()}
WEB_TERMINAL_BASH_RC
exec {quoted_shell} --rcfile "$__web_terminal_rc" -i
"""


def _zsh_launcher(shell: str) -> str:
    quoted_shell = _shell_quote(shell)
    return f"""__web_terminal_zdotdir=$(mktemp -d)
chmod 700 "$__web_terminal_zdotdir"
cat > "$__web_terminal_zdotdir/.zshrc" <<'WEB_TERMINAL_ZSH_RC'
{_zsh_hook_script()}
WEB_TERMINAL_ZSH_RC
export ZDOTDIR="$__web_terminal_zdotdir"
exec {quoted_shell} -i
"""


def _common_hook_script() -> str:
    return r'''__web_terminal_prepare_codex_home() {
  [ -n "$WEB_TERMINAL_CODEX_HOME" ] || return 0
  __web_terminal_source_codex_home="${WEB_TERMINAL_ORIGINAL_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}"
  case "$WEB_TERMINAL_CODEX_HOME" in
    "~/"*) WEB_TERMINAL_CODEX_HOME="$HOME/${WEB_TERMINAL_CODEX_HOME#"~/"}" ;;
  esac
  mkdir -p "$WEB_TERMINAL_CODEX_HOME/sessions" "$WEB_TERMINAL_CODEX_HOME/log" "$WEB_TERMINAL_CODEX_HOME/shell_snapshots" 2>/dev/null || return 0
  for __web_terminal_codex_item in auth.json config.toml AGENTS.md skills plugins plugin_marketplaces.json; do
    [ -e "$__web_terminal_source_codex_home/$__web_terminal_codex_item" ] || continue
    [ -e "$WEB_TERMINAL_CODEX_HOME/$__web_terminal_codex_item" ] && continue
    ln -s "$__web_terminal_source_codex_home/$__web_terminal_codex_item" "$WEB_TERMINAL_CODEX_HOME/$__web_terminal_codex_item" 2>/dev/null || true
  done
  export CODEX_HOME="$WEB_TERMINAL_CODEX_HOME"
}
__web_terminal_expand_home_path() {
  case "$1" in
    "~"*) printf '%s\n' "$HOME${1#\~}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}
__web_terminal_mkdir_env_path() {
  eval "__web_terminal_env_path=\${$1:-}"
  [ -n "$__web_terminal_env_path" ] || return 0
  __web_terminal_env_path=$(__web_terminal_expand_home_path "$__web_terminal_env_path")
  mkdir -p "$__web_terminal_env_path" 2>/dev/null || true
}
__web_terminal_prepare_agent_homes() {
  __web_terminal_prepare_codex_home
  case "$WEB_TERMINAL_CLAUDE_CODE_HOME" in
    "~/"*) WEB_TERMINAL_CLAUDE_CODE_HOME="$HOME/${WEB_TERMINAL_CLAUDE_CODE_HOME#"~/"}" ;;
  esac
  case "$WEB_TERMINAL_CURSOR_HOME" in
    "~/"*) WEB_TERMINAL_CURSOR_HOME="$HOME/${WEB_TERMINAL_CURSOR_HOME#"~/"}" ;;
  esac
  [ -n "$WEB_TERMINAL_CLAUDE_CODE_HOME" ] && mkdir -p "$WEB_TERMINAL_CLAUDE_CODE_HOME/projects" 2>/dev/null || true
  export CLAUDE_CONFIG_DIR="$WEB_TERMINAL_CLAUDE_CODE_HOME"
  export CURSOR_AGENT_HOME="$WEB_TERMINAL_CURSOR_HOME"
  export CURSOR_CONFIG_DIR="$WEB_TERMINAL_CURSOR_HOME"
  export CURSOR_DATA_DIR="$WEB_TERMINAL_CURSOR_HOME"
  __web_terminal_mkdir_env_path CLAUDE_CONFIG_DIR
  __web_terminal_mkdir_env_path CURSOR_AGENT_HOME
  __web_terminal_mkdir_env_path CURSOR_CONFIG_DIR
  __web_terminal_mkdir_env_path CURSOR_DATA_DIR
  export WEB_TERMINAL_CLAUDE_CODE_HOME WEB_TERMINAL_CURSOR_HOME
}
__web_terminal_prepare_cursor_home() {
  [ -n "$WEB_TERMINAL_CURSOR_HOME" ] || return 0
  local managed_cursor source_cursor item base
  case "$WEB_TERMINAL_CURSOR_HOME" in
    "~/"*) managed_cursor="$HOME/${WEB_TERMINAL_CURSOR_HOME#"~/"}" ;;
    *) managed_cursor="$WEB_TERMINAL_CURSOR_HOME" ;;
  esac
  case "${WEB_TERMINAL_ORIGINAL_CURSOR_DIR:-$HOME/.cursor}" in
    "~/"*) source_cursor="$HOME/${WEB_TERMINAL_ORIGINAL_CURSOR_DIR#"~/"}" ;;
    *) source_cursor="${WEB_TERMINAL_ORIGINAL_CURSOR_DIR:-$HOME/.cursor}" ;;
  esac
  mkdir -p "$managed_cursor/chats" 2>/dev/null || true
  for item in "$source_cursor"/*; do
    [ -e "$item" ] || continue
    base="${item##*/}"
    [ "$base" = "chats" ] && continue
    [ -e "$managed_cursor/$base" ] && continue
    ln -sf "$item" "$managed_cursor/$base" 2>/dev/null || true
  done
  export CURSOR_AGENT_HOME="$managed_cursor"
  export CURSOR_CONFIG_DIR="$managed_cursor"
  export CURSOR_DATA_DIR="$managed_cursor"
}
__web_terminal_prepare_agent_homes
__web_terminal_prepare_cursor_home
__web_terminal_sequence=0
__web_terminal_active_sequence=""
__web_terminal_active_command=""
__web_terminal_emit_command_marker() {
  __web_terminal_phase="$1"
  __web_terminal_shell="$2"
  __web_terminal_sequence_value="$3"
  __web_terminal_exit_status="$4"
  shift 4
  __web_terminal_command="$*"
  [ -n "$__web_terminal_command" ] || return 0
  __web_terminal_payload=$(
    WEB_TERMINAL_CAPTURED_PHASE="$__web_terminal_phase" \
    WEB_TERMINAL_CAPTURED_COMMAND="$__web_terminal_command" \
    WEB_TERMINAL_CAPTURED_SHELL="$__web_terminal_shell" \
    WEB_TERMINAL_CAPTURED_CWD="$PWD" \
    WEB_TERMINAL_CAPTURED_SEQUENCE="$__web_terminal_sequence_value" \
    WEB_TERMINAL_CAPTURED_EXIT_STATUS="$__web_terminal_exit_status" \
    python3 - <<'WEB_TERMINAL_PAYLOAD_PY'
import base64
import json
import os
from datetime import UTC, datetime

payload = {
    "phase": os.environ["WEB_TERMINAL_CAPTURED_PHASE"],
    "command": os.environ["WEB_TERMINAL_CAPTURED_COMMAND"],
    "shell": os.environ["WEB_TERMINAL_CAPTURED_SHELL"],
    "cwd": os.environ.get("WEB_TERMINAL_CAPTURED_CWD") or None,
    "captured_at": datetime.now(UTC).isoformat(),
    "sequence": int(os.environ["WEB_TERMINAL_CAPTURED_SEQUENCE"]),
}
exit_status = os.environ.get("WEB_TERMINAL_CAPTURED_EXIT_STATUS")
if exit_status:
    try:
        payload["exit_status"] = int(exit_status)
    except ValueError:
        payload["exit_status"] = exit_status
print(base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii"))
WEB_TERMINAL_PAYLOAD_PY
  ) || return 0
  [ -n "$__web_terminal_payload" ] || return 0
  printf '\033]777;web-terminal-command;window_id=%s;payload=%s\007' "$WEB_TERMINAL_WINDOW_ID" "$__web_terminal_payload"
}
'''


def _bash_hook_script() -> str:
    return _common_hook_script() + r'''
if [ -r "$HOME/.bashrc" ]; then
  . "$HOME/.bashrc"
fi
__web_terminal_last_history_id=""
__web_terminal_initial_history_line=$(HISTTIMEFORMAT= history 1 2>/dev/null || true)
__web_terminal_initial_history_line="${__web_terminal_initial_history_line#"${__web_terminal_initial_history_line%%[![:space:]]*}"}"
__web_terminal_last_history_id="${__web_terminal_initial_history_line%%[[:space:]]*}"
__web_terminal_in_hook=0
__web_terminal_start_bash_command() {
  [ "$__web_terminal_in_hook" = "1" ] && return 0
  case "$BASH_COMMAND" in
    __web_terminal_*|history\ *|trap\ *|PROMPT_COMMAND=*) return 0 ;;
  esac
  local __web_terminal_history_line __web_terminal_history_id __web_terminal_command
  __web_terminal_in_hook=1
  __web_terminal_history_line=$(HISTTIMEFORMAT= history 1 2>/dev/null)
  __web_terminal_in_hook=0
  [ -n "$__web_terminal_history_line" ] || return 0
  __web_terminal_history_line="${__web_terminal_history_line#"${__web_terminal_history_line%%[![:space:]]*}"}"
  __web_terminal_history_id="${__web_terminal_history_line%%[[:space:]]*}"
  [ -n "$__web_terminal_history_id" ] || return 0
  [ "$__web_terminal_history_id" != "$__web_terminal_last_history_id" ] || return 0
  __web_terminal_last_history_id="$__web_terminal_history_id"
  __web_terminal_command="${__web_terminal_history_line#"$__web_terminal_history_id"}"
  __web_terminal_command="${__web_terminal_command#"${__web_terminal_command%%[![:space:]]*}"}"
  [ -n "$__web_terminal_command" ] || return 0
  __web_terminal_sequence=$((__web_terminal_sequence + 1))
  __web_terminal_active_sequence="$__web_terminal_sequence"
  __web_terminal_active_command="$__web_terminal_command"
  __web_terminal_in_hook=1
  __web_terminal_emit_command_marker started bash "$__web_terminal_active_sequence" "" "$__web_terminal_command"
  __web_terminal_in_hook=0
}
__web_terminal_finish_bash_command() {
  local __web_terminal_status=$?
  [ -n "$__web_terminal_active_sequence" ] || return $__web_terminal_status
  __web_terminal_in_hook=1
  __web_terminal_emit_command_marker finished bash "$__web_terminal_active_sequence" "$__web_terminal_status" "$__web_terminal_active_command"
  __web_terminal_active_sequence=""
  __web_terminal_active_command=""
  __web_terminal_in_hook=0
  return $__web_terminal_status
}
trap '__web_terminal_start_bash_command' DEBUG
PROMPT_COMMAND="__web_terminal_finish_bash_command${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
'''


def _zsh_hook_script() -> str:
    return _common_hook_script() + r'''
if [ -r "$HOME/.zshrc" ]; then
  source "$HOME/.zshrc"
fi
if whence -w preexec >/dev/null 2>&1; then
  functions -c preexec __web_terminal_user_preexec
fi
if whence -w precmd >/dev/null 2>&1; then
  functions -c precmd __web_terminal_user_precmd
fi
__web_terminal_pending_command=""
preexec() {
  __web_terminal_pending_command="$1"
  __web_terminal_sequence=$((__web_terminal_sequence + 1))
  __web_terminal_active_sequence="$__web_terminal_sequence"
  __web_terminal_active_command="$1"
  __web_terminal_emit_command_marker started zsh "$__web_terminal_active_sequence" "" "$__web_terminal_active_command"
  if whence -w __web_terminal_user_preexec >/dev/null 2>&1; then
    __web_terminal_user_preexec "$@"
  fi
}
precmd() {
  __web_terminal_status=$?
  if [ -n "$__web_terminal_active_sequence" ]; then
    __web_terminal_emit_command_marker finished zsh "$__web_terminal_active_sequence" "$__web_terminal_status" "$__web_terminal_active_command"
    __web_terminal_active_sequence=""
    __web_terminal_active_command=""
    __web_terminal_pending_command=""
  fi
  if whence -w __web_terminal_user_precmd >/dev/null 2>&1; then
    __web_terminal_user_precmd "$@"
  fi
}
'''


def _shell_quote(value: str) -> str:
    if value and _SAFE_SHELL_VALUE.fullmatch(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"
