#!/usr/bin/env bash
# Remove this terminal's Web Terminal worktree (run from main checkout).
set -euo pipefail

if [[ -z "${WEB_TERMINAL_WINDOW_ID:-}" ]]; then
  echo "remove-worktree: WEB_TERMINAL_WINDOW_ID is not set" >&2
  exit 1
fi

if [[ -f .git ]]; then
  gitdir_line="$(head -1 .git)"
  main_git="${gitdir_line#gitdir: }"
  main_git="${main_git%/worktrees/*}"
  main_root="$(cd "$(dirname "$main_git")/.." && pwd)"
  cd "$main_root"
elif [[ -d .git ]]; then
  main_root="$(git rev-parse --show-toplevel)"
  cd "$main_root"
else
  echo "remove-worktree: not inside a git repository" >&2
  exit 1
fi

wt_rel=".web-terminal-acp/worktrees/${WEB_TERMINAL_WINDOW_ID}"
if [[ ! -d "$wt_rel" ]]; then
  echo "remove-worktree: no worktree at $wt_rel" >&2
  exit 0
fi

branch="$(git -C "$wt_rel" branch --show-current 2>/dev/null || true)"
git worktree remove --force "$wt_rel" 2>/dev/null || git worktree remove "$wt_rel"

if [[ -n "$branch" ]] && git show-ref --verify --quiet "refs/heads/$branch"; then
  git branch -d "$branch" 2>/dev/null || echo "remove-worktree: branch $branch not deleted (may be unmerged)" >&2
fi

echo "Removed worktree: $wt_rel"
