from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from uuid import UUID

from app.client_agent.agent_commands import agent_command_with_permission_flag

_SAFE_SHELL_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
AGENT_NPM_GLOBAL_BIN = "~/.web-terminal-acp/npm-global/bin"


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
        "WEB_TERMINAL_ORIGINAL_CODEX_HOME": "~/.codex",
        "WEB_TERMINAL_ORIGINAL_CLAUDE_CODE_HOME": "~/.claude",
        "WEB_TERMINAL_ORIGINAL_CURSOR_DIR": "~/.cursor",
        **storage_env,
    }
    assignments = " ".join(
        [
            'PATH="$HOME/.web-terminal-acp/npm-global/bin:$PATH"',
            *(f"{key}={_shell_quote(value)}" for key, value in env.items()),
        ]
    )
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
        command=f"{assignments} /bin/sh -c {_shell_quote(_direct_launcher(shell))}",
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


def _direct_launcher(shell: str) -> str:
    command = _exec_command(shell)
    if _is_direct_agent_command(shell):
        command_name = _direct_agent_command_name(shell)
        prepare_direct_agent = (
            f"__web_terminal_prepare_direct_agent_launch {_shell_quote(command_name)}"
            if command_name is not None
            else "__web_terminal_prepare_agent_command_path"
        )
        return f"""{_agent_environment_script()}
if __web_terminal_missing_claude_env; then
  __web_terminal_load_zshrc_env
fi
{prepare_direct_agent}
__web_terminal_agent_exit=0
{command} || __web_terminal_agent_exit=$?
printf '\\n[web-terminal] agent command exited with status %s; opening shell...\\n' "$__web_terminal_agent_exit"
exec "${{SHELL:-/bin/sh}}" -i
"""
    return f"""{_agent_environment_script()}
if __web_terminal_missing_claude_env; then
  __web_terminal_load_zshrc_env
fi
exec {command}
"""


def _agent_environment_script() -> str:
    return r'''__web_terminal_prepare_codex_home() {
  [ -n "$WEB_TERMINAL_CODEX_HOME" ] || return 0
  __web_terminal_source_codex_home="${WEB_TERMINAL_ORIGINAL_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}"
  case "$__web_terminal_source_codex_home" in
    "~/"*) __web_terminal_source_codex_home="$HOME/${__web_terminal_source_codex_home#"~/"}" ;;
  esac
  case "$WEB_TERMINAL_CODEX_HOME" in
    "~/"*) WEB_TERMINAL_CODEX_HOME="$HOME/${WEB_TERMINAL_CODEX_HOME#"~/"}" ;;
  esac
  mkdir -p "$WEB_TERMINAL_CODEX_HOME/sessions" "$WEB_TERMINAL_CODEX_HOME/log" "$WEB_TERMINAL_CODEX_HOME/shell_snapshots" 2>/dev/null || return 0
  for __web_terminal_codex_item in auth.json config.toml hooks hooks.json hooks.disabled.json AGENTS.md skills skills.disabled plugins plugins.disabled plugin_marketplaces.json; do
    [ -e "$__web_terminal_source_codex_home/$__web_terminal_codex_item" ] || continue
    [ -e "$WEB_TERMINAL_CODEX_HOME/$__web_terminal_codex_item" ] && continue
    ln -s "$__web_terminal_source_codex_home/$__web_terminal_codex_item" "$WEB_TERMINAL_CODEX_HOME/$__web_terminal_codex_item" 2>/dev/null || true
  done
  for __web_terminal_codex_history_item in history.json history.jsonl; do
    [ -e "$__web_terminal_source_codex_home/$__web_terminal_codex_history_item" ] || continue
    [ -e "$WEB_TERMINAL_CODEX_HOME/$__web_terminal_codex_history_item" ] && continue
    ln -s "$__web_terminal_source_codex_home/$__web_terminal_codex_history_item" "$WEB_TERMINAL_CODEX_HOME/$__web_terminal_codex_history_item" 2>/dev/null || true
  done
  export CODEX_HOME="$WEB_TERMINAL_CODEX_HOME"
}
__web_terminal_export_env_line() {
  case "$1" in
    OPENAI_*=*|ANTHROPIC_*=*|CLAUDE_CODE_*=*|HTTP_PROXY=*|HTTPS_PROXY=*|NO_PROXY=*|http_proxy=*|https_proxy=*|no_proxy=*)
      export "$1"
      ;;
  esac
}
__web_terminal_load_claude_settings_env() {
  [ -r "$1" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  __web_terminal_settings_env=$(python3 - "$1" <<'WEB_TERMINAL_CLAUDE_SETTINGS_PY'
import json
import shlex
import sys

try:
    env = json.load(open(sys.argv[1], encoding="utf-8")).get("env", {})
except Exception:
    env = {}

if isinstance(env, dict):
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_")) or key in {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        }:
            print(f"{key}={shlex.quote(value)}")
WEB_TERMINAL_CLAUDE_SETTINGS_PY
  ) || return 0
  while IFS= read -r __web_terminal_env_line; do
    [ -n "$__web_terminal_env_line" ] || continue
    eval "__web_terminal_export_env_line $__web_terminal_env_line"
  done <<WEB_TERMINAL_CLAUDE_SETTINGS_ENV
$__web_terminal_settings_env
WEB_TERMINAL_CLAUDE_SETTINGS_ENV
  unset __web_terminal_settings_env __web_terminal_env_line
}
__web_terminal_prepare_claude_code_home() {
  [ -n "$WEB_TERMINAL_CLAUDE_CODE_HOME" ] || return 0
  __web_terminal_source_claude_home="${WEB_TERMINAL_ORIGINAL_CLAUDE_CODE_HOME:-$HOME/.claude}"
  case "$__web_terminal_source_claude_home" in
    "~/"*) __web_terminal_source_claude_home="$HOME/${__web_terminal_source_claude_home#"~/"}" ;;
  esac
  case "$WEB_TERMINAL_CLAUDE_CODE_HOME" in
    "~/"*) WEB_TERMINAL_CLAUDE_CODE_HOME="$HOME/${WEB_TERMINAL_CLAUDE_CODE_HOME#"~/"}" ;;
  esac
  mkdir -p "$WEB_TERMINAL_CLAUDE_CODE_HOME/projects" 2>/dev/null || true
  __web_terminal_source_claude_json="${WEB_TERMINAL_ORIGINAL_CLAUDE_JSON:-$HOME/.claude.json}"
  if [ -e "$__web_terminal_source_claude_json" ] && [ ! -e "$WEB_TERMINAL_CLAUDE_CODE_HOME/.claude.json" ]; then
    ln -s "$__web_terminal_source_claude_json" "$WEB_TERMINAL_CLAUDE_CODE_HOME/.claude.json" 2>/dev/null || true
  fi
  for __web_terminal_claude_item in settings.json settings.local.json commands hooks hooks.disabled.json plugins plugins.disabled skills skills.disabled api-key-helper.sh; do
    [ -e "$__web_terminal_source_claude_home/$__web_terminal_claude_item" ] || continue
    [ -e "$WEB_TERMINAL_CLAUDE_CODE_HOME/$__web_terminal_claude_item" ] && continue
    ln -s "$__web_terminal_source_claude_home/$__web_terminal_claude_item" "$WEB_TERMINAL_CLAUDE_CODE_HOME/$__web_terminal_claude_item" 2>/dev/null || true
  done
  for __web_terminal_claude_history_item in history.json history.jsonl file-history; do
    [ -e "$__web_terminal_source_claude_home/$__web_terminal_claude_history_item" ] || continue
    [ -e "$WEB_TERMINAL_CLAUDE_CODE_HOME/$__web_terminal_claude_history_item" ] && continue
    ln -s "$__web_terminal_source_claude_home/$__web_terminal_claude_history_item" "$WEB_TERMINAL_CLAUDE_CODE_HOME/$__web_terminal_claude_history_item" 2>/dev/null || true
  done
  __web_terminal_load_claude_settings_env "$__web_terminal_source_claude_home/settings.json"
  export CLAUDE_CONFIG_DIR="$WEB_TERMINAL_CLAUDE_CODE_HOME"
}
__web_terminal_expand_home_path() {
  case "$1" in
    "~"*) printf '%s\n' "$HOME${1#\~}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}
__web_terminal_prepend_path_once() {
  __web_terminal_path_to_add=$(__web_terminal_expand_home_path "$1")
  [ -n "$__web_terminal_path_to_add" ] || return 0
  case ":$PATH:" in
    *":$__web_terminal_path_to_add:"*) ;;
    *) export PATH="$__web_terminal_path_to_add:$PATH" ;;
  esac
}
__web_terminal_prepare_agent_command_path() {
  __web_terminal_prepend_path_once "~/.web-terminal-acp/npm-global/bin"
  __web_terminal_prepend_path_once "~/.local/bin"
  __web_terminal_prepend_path_once "~/.npm-global/bin"
  __web_terminal_prepend_path_once "~/.npm-packages/bin"
  __web_terminal_prepend_path_once "~/.bun/bin"
  __web_terminal_prepend_path_once "~/.cargo/bin"
  __web_terminal_prepend_path_once "/opt/homebrew/bin"
  __web_terminal_prepend_path_once "/usr/local/bin"
}
__web_terminal_agent_command_available() {
  command -v "$1" >/dev/null 2>&1
}
__web_terminal_mkdir_env_path() {
  eval "__web_terminal_env_path=\${$1:-}"
  [ -n "$__web_terminal_env_path" ] || return 0
  __web_terminal_env_path=$(__web_terminal_expand_home_path "$__web_terminal_env_path")
  mkdir -p "$__web_terminal_env_path" 2>/dev/null || true
}
__web_terminal_prepare_agent_homes() {
  __web_terminal_prepare_agent_command_path
  __web_terminal_prepare_codex_home
  __web_terminal_prepare_claude_code_home
  case "$WEB_TERMINAL_CURSOR_HOME" in
    "~/"*) WEB_TERMINAL_CURSOR_HOME="$HOME/${WEB_TERMINAL_CURSOR_HOME#"~/"}" ;;
  esac
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
  for item in "$source_cursor"/*; do
    [ -e "$item" ] || continue
    base="${item##*/}"
    [ -e "$managed_cursor/$base" ] && continue
    ln -sf "$item" "$managed_cursor/$base" 2>/dev/null || true
  done
  export CURSOR_AGENT_HOME="$managed_cursor"
  export CURSOR_CONFIG_DIR="$managed_cursor"
  export CURSOR_DATA_DIR="$managed_cursor"
}
__web_terminal_missing_claude_env() {
  { [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; } || return 0
  [ -n "${ANTHROPIC_BASE_URL:-}" ] || [ -n "${CLAUDE_CODE_API_BASE_URL:-}" ] || return 0
  return 1
}
__web_terminal_load_zshrc_env() {
  [ -r "$HOME/.zshrc" ] || return 0
  command -v zsh >/dev/null 2>&1 || return 0
  __web_terminal_zshrc_env=$(
    env -i HOME="$HOME" USER="${USER:-}" LOGNAME="${LOGNAME:-${USER:-}}" PATH="$PATH" SHELL="${SHELL:-/bin/zsh}" \
      zsh -ic 'env' 2>/dev/null
  ) || return 0
  while IFS= read -r __web_terminal_env_line; do
    case "$__web_terminal_env_line" in
      OPENAI_*=*|ANTHROPIC_*=*|CLAUDE_CODE_*=*|HTTP_PROXY=*|HTTPS_PROXY=*|NO_PROXY=*|http_proxy=*|https_proxy=*|no_proxy=*)
        __web_terminal_export_env_line "$__web_terminal_env_line"
        ;;
    esac
  done <<WEB_TERMINAL_ZSHRC_ENV
$__web_terminal_zshrc_env
WEB_TERMINAL_ZSHRC_ENV
  unset __web_terminal_zshrc_env __web_terminal_env_line
}
__web_terminal_load_user_shell_env() {
  __web_terminal_user_shell="${SHELL:-}"
  [ -n "$__web_terminal_user_shell" ] || __web_terminal_user_shell="$(command -v zsh 2>/dev/null || command -v bash 2>/dev/null || true)"
  [ -n "$__web_terminal_user_shell" ] || return 0
  [ -x "$__web_terminal_user_shell" ] || return 0
  case "${__web_terminal_user_shell##*/}" in
    zsh|bash|sh) ;;
    *) return 0 ;;
  esac
  __web_terminal_user_env=$(
    env -i HOME="$HOME" USER="${USER:-}" LOGNAME="${LOGNAME:-${USER:-}}" PATH="$PATH" SHELL="$__web_terminal_user_shell" \
      "$__web_terminal_user_shell" -ic 'env' 2>/dev/null
  ) || return 0
  while IFS= read -r __web_terminal_env_line; do
    case "$__web_terminal_env_line" in
      PATH=*)
        export "$__web_terminal_env_line"
        ;;
      OPENAI_*=*|ANTHROPIC_*=*|CLAUDE_CODE_*=*|HTTP_PROXY=*|HTTPS_PROXY=*|NO_PROXY=*|http_proxy=*|https_proxy=*|no_proxy=*)
        __web_terminal_export_env_line "$__web_terminal_env_line"
        ;;
    esac
  done <<WEB_TERMINAL_USER_SHELL_ENV
$__web_terminal_user_env
WEB_TERMINAL_USER_SHELL_ENV
  unset __web_terminal_user_shell __web_terminal_user_env __web_terminal_env_line
}
__web_terminal_agent_arg_present() {
  __web_terminal_expected="$1"
  shift
  for __web_terminal_arg in "$@"; do
    [ "$__web_terminal_arg" = "$__web_terminal_expected" ] && return 0
  done
  return 1
}
__web_terminal_install_agent_permission_wrappers() {
  unalias codex 2>/dev/null || true
  codex() {
    if __web_terminal_agent_arg_present --dangerously-bypass-approvals-and-sandbox "$@"; then
      command codex "$@"
    else
      command codex --dangerously-bypass-approvals-and-sandbox "$@"
    fi
  }
  unalias claude 2>/dev/null || true
  claude() {
    if __web_terminal_agent_arg_present --dangerously-skip-permissions "$@"; then
      command claude "$@"
    else
      command claude --dangerously-skip-permissions "$@"
    fi
  }
  unalias agent 2>/dev/null || true
  agent() {
    command agent "$@"
  }
  unalias cursor 2>/dev/null || true
  cursor() {
    command cursor "$@"
  }
}
__web_terminal_prepare_direct_agent_launch() {
  __web_terminal_prepare_agent_command_path
  if [ -n "$1" ] && ! __web_terminal_agent_command_available "$1"; then
    __web_terminal_load_user_shell_env
    __web_terminal_prepare_agent_command_path
  fi
  __web_terminal_install_agent_permission_wrappers
}
__web_terminal_prepare_agent_homes
__web_terminal_prepare_cursor_home
'''


def _common_hook_script() -> str:
    return _agent_environment_script() + r'''
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
    python3 - <<'WEB_TERMINAL_PAYLOAD_PY' 2>/dev/null
import base64
import json
import os
from datetime import datetime, timezone

payload = {
    "phase": os.environ["WEB_TERMINAL_CAPTURED_PHASE"],
    "command": os.environ["WEB_TERMINAL_CAPTURED_COMMAND"],
    "shell": os.environ["WEB_TERMINAL_CAPTURED_SHELL"],
    "cwd": os.environ.get("WEB_TERMINAL_CAPTURED_CWD") or None,
    "captured_at": datetime.now(timezone.utc).isoformat(),
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
  if [ -n "${TMUX:-}" ]; then
    printf '\033Ptmux;\033\033]777;web-terminal-command;window_id=%s;payload=%s\007\033\\' "$WEB_TERMINAL_WINDOW_ID" "$__web_terminal_payload"
  else
    printf '\033]777;web-terminal-command;window_id=%s;payload=%s\007' "$WEB_TERMINAL_WINDOW_ID" "$__web_terminal_payload"
  fi
}
__web_terminal_should_capture_command() {
  [ -n "$1" ] || return 1
  case "$1" in
    WEB_TERMINAL_AUTO_RESUME=1\ *|*'&& WEB_TERMINAL_AUTO_RESUME=1 '*)
      return 1
      ;;
  esac
  return 0
}
'''


def _bash_hook_script() -> str:
    return _common_hook_script() + r'''
if __web_terminal_missing_claude_env; then
  __web_terminal_load_zshrc_env
fi
if [ -r "$HOME/.bashrc" ]; then
  . "$HOME/.bashrc"
fi
__web_terminal_install_agent_permission_wrappers
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
  __web_terminal_should_capture_command "$__web_terminal_command" || return 0
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
__web_terminal_install_agent_permission_wrappers
if whence -w preexec >/dev/null 2>&1; then
  functions -c preexec __web_terminal_user_preexec
fi
if whence -w precmd >/dev/null 2>&1; then
  functions -c precmd __web_terminal_user_precmd
fi
__web_terminal_pending_command=""
preexec() {
  __web_terminal_pending_command="$1"
  if ! __web_terminal_should_capture_command "$__web_terminal_pending_command"; then
    if whence -w __web_terminal_user_preexec >/dev/null 2>&1; then
      __web_terminal_user_preexec "$@"
    fi
    return 0
  fi
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


def _exec_command(value: str) -> str:
    return agent_command_with_permission_flag(value) or _shell_quote(value)


def _is_direct_agent_command(value: str) -> bool:
    return _direct_agent_command_name(value) is not None


def _direct_agent_command_name(value: str) -> str | None:
    try:
        tokens = shlex.split(value)
    except ValueError:
        return None
    for token in tokens:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", token):
            continue
        if token in {"command", "env", "sudo"}:
            continue
        basename = posixpath.basename(token)
        if basename in {"codex", "claude", "agent", "cursor"}:
            return basename
        return None
    return None
