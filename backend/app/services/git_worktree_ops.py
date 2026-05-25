from __future__ import annotations

import re
from typing import Any

_GIT_WORKTREE_ADD_RE = re.compile(r"\bgit\s+worktree\s+add\b", re.IGNORECASE)


def parse_git_worktree_add_path(command: str, cwd: str | None) -> str | None:
    if not _GIT_WORKTREE_ADD_RE.search(command):
        return None

    tokens = command.split()
    path_tokens: list[str] = []
    skip_next = False
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        lower = token.lower()
        if lower in {"-f", "--force", "-b", "--detach", "-q", "--quiet"}:
            if lower in {"-b", "--detach"}:
                skip_next = True
            continue
        if lower in {"add", "git", "worktree"}:
            continue
        if token.startswith("-"):
            continue
        path_tokens.append(token)
        break

    if not path_tokens:
        return None

    raw_path = path_tokens[0]
    if raw_path.startswith("/"):
        return raw_path
    if cwd:
        return f"{cwd.rstrip('/')}/{raw_path}"
    return raw_path


def compute_session_diff(
    start_snapshot: dict[str, Any] | None,
    end_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    if not start_snapshot or not end_snapshot:
        return {"has_changes": False}

    start_head = start_snapshot.get("head_sha")
    end_head = end_snapshot.get("head_sha")
    head_moved = bool(start_head and end_head and start_head != end_head)

    start_status = start_snapshot.get("status_porcelain") or ""
    end_status = end_snapshot.get("status_porcelain") or ""
    dirty_at_end = bool(end_status.strip())
    status_changed = start_status != end_status

    has_changes = head_moved or dirty_at_end or status_changed
    return {
        "has_changes": has_changes,
        "head_moved": head_moved,
        "start_head": start_head,
        "end_head": end_head,
        "uncommitted_at_end": dirty_at_end,
        "start_status_porcelain": start_status,
        "end_status_porcelain": end_status,
        "end_diff_stat": end_snapshot.get("diff_stat") or "",
        "end_staged_diff_stat": end_snapshot.get("staged_diff_stat") or "",
    }


def pending_commit_from_diff(session_diff: dict[str, Any]) -> bool:
    return bool(session_diff.get("has_changes") and session_diff.get("uncommitted_at_end"))


def pending_commit_from_live_snapshot(snapshot: dict[str, Any]) -> bool:
    if not snapshot.get("is_linked_worktree"):
        return False
    status = snapshot.get("status_porcelain") or ""
    return bool(status.strip())
