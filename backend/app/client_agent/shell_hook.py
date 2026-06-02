from __future__ import annotations

import base64
import gzip
import posixpath
import re
import shlex
import sys
import textwrap
from dataclasses import dataclass
from uuid import UUID

from app.agent_plugins import get_agent_plugin_registry
from app.client_agent.agent_commands import agent_command_with_permission_flag

_SAFE_SHELL_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
_SHELL_FUNCTION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
AGENT_NPM_GLOBAL_BIN = "~/.web-terminal-acp/npm-global/bin"


@dataclass(frozen=True)
class ManagedShellCommand:
    command: str
    command_capture_supported: bool
    hook_script: str | None = None


def build_managed_shell_command(
    *,
    shell: str,
    client_id: UUID | str,
    window_id: UUID | str,
    server_url: str,
    project_path: str | None = None,
) -> ManagedShellCommand:
    env = {
        "WEB_TERMINAL_CLIENT_ID": str(client_id),
        "WEB_TERMINAL_WINDOW_ID": str(window_id),
        "WEB_TERMINAL_SERVER_URL": server_url,
        "WEB_TERMINAL_COMMAND_HOOK": "1",
        "WEB_TERMINAL_PROJECT_PATH": project_path or "",
        **_agent_shell_environment(str(window_id)),
    }
    assignments = " ".join(
        [
            'PATH="$HOME/.web-terminal-acp/npm-global/bin:$PATH"',
            *(f"{key}={_shell_quote(value)}" for key, value in env.items()),
        ]
    )
    shell_name = posixpath.basename(shell)

    if shell_name == "bash":
        hook_script = _bash_hook_script()
        return ManagedShellCommand(
            command=f"{assignments} {_shell_quote(shell)} -lc {_shell_quote(_bash_launcher(shell, hook_script))}",
            command_capture_supported=True,
            hook_script=hook_script,
        )
    if shell_name == "zsh":
        hook_script = _zsh_hook_script()
        return ManagedShellCommand(
            command=f"{assignments} {_shell_quote(shell)} -fc {_shell_quote(_zsh_launcher(shell, hook_script))}",
            command_capture_supported=True,
            hook_script=hook_script,
        )

    return ManagedShellCommand(
        command=f"{assignments} /bin/sh -c {_shell_quote(_direct_launcher(shell))}",
        command_capture_supported=False,
    )


def _agent_shell_environment(window_id: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for plugin in get_agent_plugin_registry().all():
        for key, value in plugin.storage.shell_env_aliases.items():
            env[key] = value.format(window_id=window_id)
    for plugin in get_agent_plugin_registry().all():
        for key, value in plugin.storage.env.items():
            if key == "CODEX_HOME":
                continue
            env[key] = value.format(window_id=window_id)
    return env


def _shell_words(values: tuple[str, ...]) -> str:
    return " ".join(_shell_quote(value) for value in values)


def _env_path_prepare_lines() -> str:
    keys = sorted({key for plugin in get_agent_plugin_registry().all() for key in plugin.storage.env})
    return "\n".join(f"  __web_terminal_mkdir_env_path {_shell_quote(key)}" for key in keys)


def _agent_home_prepare_lines() -> str:
    lines = ["  __web_terminal_prepare_agent_command_path"]
    for plugin in get_agent_plugin_registry().all():
        function_name = plugin.storage.shell_prepare_function
        if function_name is None:
            continue
        if not _SHELL_FUNCTION_NAME.fullmatch(function_name):
            raise ValueError(f"invalid agent shell prepare function: {function_name}")
        lines.append(f"  {function_name}")
    lines.append(_env_path_prepare_lines())
    lines.append(
        "  export WEB_TERMINAL_CLAUDE_CODE_HOME WEB_TERMINAL_CURSOR_HOME WEB_TERMINAL_ANTIGRAVITY_HOME WEB_TERMINAL_ANTIGRAVITY_COMMAND_HOME"
    )
    return "\n".join(line for line in lines if line)


def _bash_launcher(shell: str, hook_script: str) -> str:
    quoted_shell = _shell_quote(shell)
    return f"""__web_terminal_rc=$(mktemp)
chmod 600 "$__web_terminal_rc"
{_decode_hook_to_path("$__web_terminal_rc", hook_script, "WEB_TERMINAL_BASH_RC_GZ")}
exec {quoted_shell} --rcfile "$__web_terminal_rc" -i
"""


def _zsh_launcher(shell: str, hook_script: str) -> str:
    quoted_shell = _shell_quote(shell)
    return f"""__web_terminal_zdotdir=$(mktemp -d)
chmod 700 "$__web_terminal_zdotdir"
{_decode_hook_to_path("$__web_terminal_zdotdir/.zshrc", hook_script, "WEB_TERMINAL_ZSH_RC_GZ")}
export ZDOTDIR="$__web_terminal_zdotdir"
exec {quoted_shell} -i
"""


def _decode_hook_to_path(path: str, hook_script: str, marker: str) -> str:
    payload = "\n".join(
        textwrap.wrap(base64.b64encode(gzip.compress(hook_script.encode("utf-8"))).decode("ascii"), 76)
    )
    return f"""{_shell_quote(sys.executable)} -c 'import base64,gzip,sys; sys.stdout.write(gzip.decompress(base64.b64decode(sys.stdin.read())).decode("utf-8"))' > "{path}" <<'{marker}'
{payload}
{marker}
__web_terminal_decode_status=$?
[ "$__web_terminal_decode_status" -eq 0 ] || exit "$__web_terminal_decode_status"
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
    codex_plugin = get_agent_plugin_registry().by_agent_id("codex")
    claude_plugin = get_agent_plugin_registry().by_agent_id("claude")
    script = r'''__web_terminal_prepare_codex_home() {
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
  case "$__web_terminal_source_claude_json" in
    "~/"*) __web_terminal_source_claude_json="$HOME/${__web_terminal_source_claude_json#"~/"}" ;;
  esac
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
  mkdir -p "$managed_cursor/chats" 2>/dev/null || true
  if [ -L "$managed_cursor/chats" ]; then
    rm -f "$managed_cursor/chats" 2>/dev/null || true
    mkdir -p "$managed_cursor/chats" 2>/dev/null || true
  fi
  find "$source_cursor" -mindepth 1 -maxdepth 1 2>/dev/null | while IFS= read -r item; do
    base="${item##*/}"
    case "$base" in
      chats) continue ;;
    esac
    [ -e "$managed_cursor/$base" ] && continue
    ln -sf "$item" "$managed_cursor/$base" 2>/dev/null || true
  done
  export CURSOR_AGENT_HOME="$managed_cursor"
  export CURSOR_CONFIG_DIR="$managed_cursor"
  export CURSOR_DATA_DIR="$managed_cursor"
}
__web_terminal_prepare_antigravity_home() {
  [ -n "$WEB_TERMINAL_ANTIGRAVITY_HOME" ] || return 0
  local managed_antigravity source_antigravity item base command_home original_home workspace_root
  case "$WEB_TERMINAL_ANTIGRAVITY_HOME" in
    "~/"*) managed_antigravity="$HOME/${WEB_TERMINAL_ANTIGRAVITY_HOME#"~/"}" ;;
    *) managed_antigravity="$WEB_TERMINAL_ANTIGRAVITY_HOME" ;;
  esac
  case "${WEB_TERMINAL_ORIGINAL_ANTIGRAVITY_CLI_HOME:-$HOME/.gemini/antigravity-cli}" in
    "~/"*) source_antigravity="$HOME/${WEB_TERMINAL_ORIGINAL_ANTIGRAVITY_CLI_HOME#"~/"}" ;;
    *) source_antigravity="${WEB_TERMINAL_ORIGINAL_ANTIGRAVITY_CLI_HOME:-$HOME/.gemini/antigravity-cli}" ;;
  esac
  mkdir -p "$managed_antigravity" 2>/dev/null || true
  find "$source_antigravity" -mindepth 1 -maxdepth 1 2>/dev/null | while IFS= read -r item; do
    base="${item##*/}"
    case "$base" in
      brain|cache|log|scratch) continue ;;
    esac
    [ -e "$managed_antigravity/$base" ] && continue
    ln -sf "$item" "$managed_antigravity/$base" 2>/dev/null || true
  done
  if [ -f "$source_antigravity/cache/onboarding.json" ] && [ ! -f "$managed_antigravity/cache/onboarding.json" ]; then
    mkdir -p "$managed_antigravity/cache" 2>/dev/null || true
    cp "$source_antigravity/cache/onboarding.json" "$managed_antigravity/cache/onboarding.json" 2>/dev/null || true
  fi
  command_home="${WEB_TERMINAL_ANTIGRAVITY_COMMAND_HOME:-}"
  if [ -n "$command_home" ]; then
    case "$command_home" in
      "~/"*) command_home="$HOME/${command_home#"~/"}" ;;
    esac
    mkdir -p "$command_home/.gemini" 2>/dev/null || true
    if [ ! -e "$command_home/.gemini/antigravity-cli" ] && [ ! -L "$command_home/.gemini/antigravity-cli" ]; then
      ln -s "$managed_antigravity" "$command_home/.gemini/antigravity-cli" 2>/dev/null || true
    fi
  fi
  original_home="${WEB_TERMINAL_ORIGINAL_HOME:-$HOME}"
  case "$original_home" in
    "~"*) original_home="$HOME${original_home#\~}" ;;
  esac
  workspace_root="${AGY_PROXY_WORKSPACE_LINK_ROOT:-$original_home/agy-workspaces}"
  case "$workspace_root" in
    "~"*) workspace_root="$original_home${workspace_root#\~}" ;;
  esac
  export WEB_TERMINAL_ANTIGRAVITY_HOME="$managed_antigravity"
  export WEB_TERMINAL_ANTIGRAVITY_COMMAND_HOME="${command_home:-$HOME}"
  export AGY_PROXY_WORKSPACE_LINK_ROOT="$workspace_root"
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
__web_terminal_run_agent_command() {
  __web_terminal_command_name="$1"
  shift
  case "$__web_terminal_command_name" in
    agy|agy-p)
      __web_terminal_prepare_antigravity_home
      HOME="$WEB_TERMINAL_ANTIGRAVITY_COMMAND_HOME" AGY_PROXY_WORKSPACE_LINK_ROOT="$AGY_PROXY_WORKSPACE_LINK_ROOT" command "$__web_terminal_command_name" "$@"
      ;;
    *)
      command "$__web_terminal_command_name" "$@"
      ;;
  esac
}
__web_terminal_run_agent_command_with_permission() {
  __web_terminal_command_name="$1"
  __web_terminal_permission_flag="$2"
  shift 2
  if [ -n "$__web_terminal_permission_flag" ] && ! __web_terminal_agent_arg_present "$__web_terminal_permission_flag" "$@"; then
    set -- "$__web_terminal_permission_flag" "$@"
  fi
  __web_terminal_run_agent_command "$__web_terminal_command_name" "$@"
}
''' + _agent_permission_wrapper_script() + r'''
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
    return (
        script.replace(
            "auth.json config.toml hooks hooks.json hooks.disabled.json AGENTS.md skills skills.disabled plugins plugins.disabled plugin_marketplaces.json",
            _shell_words(("auth.json", *codex_plugin.storage.config_item_names)),
        )
        .replace(
            "history.json history.jsonl",
            _shell_words(codex_plugin.storage.history_item_names),
            1,
        )
        .replace(
            "settings.json settings.local.json commands hooks hooks.disabled.json plugins plugins.disabled skills skills.disabled api-key-helper.sh",
            _shell_words(claude_plugin.storage.config_item_names),
        )
        .replace(
            "history.json history.jsonl file-history",
            _shell_words(claude_plugin.storage.history_item_names),
        )
        .replace(
            """  __web_terminal_prepare_agent_command_path
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
  export WEB_TERMINAL_CLAUDE_CODE_HOME WEB_TERMINAL_CURSOR_HOME""",
            _agent_home_prepare_lines(),
        )
        .replace("\n__web_terminal_prepare_cursor_home\n", "\n")
    )


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
    command = agent_command_with_permission_flag(value)
    if command is None:
        return _shell_quote(value)
    command_name = _direct_agent_command_name(command)
    return _direct_agent_command_with_env(command, command_name)


def _direct_agent_command_with_env(command: str, command_name: str | None) -> str:
    if command_name not in {"agy", "agy-p"}:
        return command
    return (
        "HOME=\"$WEB_TERMINAL_ANTIGRAVITY_COMMAND_HOME\" "
        "AGY_PROXY_WORKSPACE_LINK_ROOT=\"$AGY_PROXY_WORKSPACE_LINK_ROOT\" "
        f"{command}"
    )


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
        if basename in get_agent_plugin_registry().command_names():
            return basename
        return None
    return None


def _agent_permission_wrapper_script() -> str:
    lines = ["__web_terminal_install_agent_permission_wrappers() {"]
    for plugin in get_agent_plugin_registry().all():
        for command_name in plugin.command.command_names:
            lines.append(f"  unalias {command_name} 2>/dev/null || true")
            permission_flag = plugin.command.permission_flag
            if not _SHELL_FUNCTION_NAME.fullmatch(command_name):
                helper = (
                    f"__web_terminal_run_agent_command_with_permission {command_name} {_shell_quote(permission_flag)}"
                    if permission_flag
                    else f"__web_terminal_run_agent_command {command_name}"
                )
                lines.append(f"  alias {command_name}={_shell_quote(helper)}")
                continue
            lines.append(f"  {command_name}() {{")
            if permission_flag:
                quoted_flag = _shell_quote(permission_flag)
                lines.append(
                    f"    __web_terminal_run_agent_command_with_permission {command_name} {quoted_flag} \"$@\""
                )
            else:
                lines.append(f"    __web_terminal_run_agent_command {command_name} \"$@\"")
            lines.append("  }")
    lines.append("}")
    return "\n".join(lines)
