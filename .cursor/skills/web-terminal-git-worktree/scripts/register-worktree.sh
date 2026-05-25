#!/usr/bin/env bash
# Register the current linked git worktree with Web Terminal (OSC marker).
set -euo pipefail

if [[ -z "${WEB_TERMINAL_WINDOW_ID:-}" ]]; then
  echo "register-worktree: WEB_TERMINAL_WINDOW_ID is not set (not a Web Terminal shell?)" >&2
  exit 1
fi

if [[ ! -f .git ]]; then
  echo "register-worktree: must run inside a linked git worktree (.git must be a file)" >&2
  exit 1
fi

worktree_root="$(git rev-parse --show-toplevel)"
branch="$(git branch --show-current 2>/dev/null || true)"

payload="$(
  WEB_TERMINAL_WORKTREE_ROOT="$worktree_root" \
  WEB_TERMINAL_WORKTREE_BRANCH="$branch" \
  python3 - <<'PY'
import base64
import json
import os

payload: dict[str, str] = {
    "worktree_root": os.environ["WEB_TERMINAL_WORKTREE_ROOT"],
}
branch = os.environ.get("WEB_TERMINAL_WORKTREE_BRANCH", "").strip()
if branch:
    payload["branch"] = branch
print(base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode())
PY
)"

printf '\033]777;web-terminal-worktree;window_id=%s;payload=%s\007' \
  "$WEB_TERMINAL_WINDOW_ID" "$payload"

echo "Registered worktree: $worktree_root${branch:+ ($branch)}"
