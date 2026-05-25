#!/usr/bin/env bash
# Create a Web Terminal worktree, cd into it, and register with the platform.
# Usage: init-worktree.sh [branch-suffix]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${WEB_TERMINAL_WINDOW_ID:-}" ]]; then
  echo "init-worktree: WEB_TERMINAL_WINDOW_ID is not set (not a Web Terminal shell?)" >&2
  exit 1
fi

if [[ -f .git ]]; then
  echo "init-worktree: already inside a linked worktree; registering only." >&2
  exec "$SCRIPT_DIR/register-worktree.sh"
fi

if [[ ! -d .git ]]; then
  echo "init-worktree: current directory is not a git repository" >&2
  exit 1
fi

main_root="$(git rev-parse --show-toplevel)"
cd "$main_root"

branch_suffix="${1:-${WEB_TERMINAL_WINDOW_ID:0:8}}"
branch="agent/${branch_suffix}"
wt_rel=".web-terminal-acp/worktrees/${WEB_TERMINAL_WINDOW_ID}"
wt_abs="${main_root}/${wt_rel}"

if [[ -d "$wt_abs" ]]; then
  echo "init-worktree: worktree path already exists: $wt_abs" >&2
  if [[ -f "$wt_abs/.git" ]]; then
    cd "$wt_abs"
    exec "$SCRIPT_DIR/register-worktree.sh"
  fi
  exit 1
fi

mkdir -p "$(dirname "$wt_rel")"
git worktree add "$wt_rel" -b "$branch"
cd "$wt_abs"
exec "$SCRIPT_DIR/register-worktree.sh"
