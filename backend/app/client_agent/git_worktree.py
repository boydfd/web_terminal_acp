from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

_GIT_TIMEOUT_SECONDS = 15.0


async def _run_git(cwd: str, *args: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=_GIT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, "", "git command timed out"
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


def _normalize_path(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def is_linked_worktree_path(path: str) -> bool:
    git_entry = Path(path) / ".git"
    return git_entry.is_file()


async def detect_git_context(path: str) -> dict[str, Any]:
    normalized = _normalize_path(path)
    if not os.path.isdir(normalized):
        return {"is_git": False, "is_linked_worktree": False, "path": normalized}

    code, inside, _stderr = await _run_git(normalized, "rev-parse", "--is-inside-work-tree")
    if code != 0 or inside.lower() != "true":
        return {"is_git": False, "is_linked_worktree": False, "path": normalized}

    is_linked = is_linked_worktree_path(normalized)
    _code, top_level, _stderr = await _run_git(normalized, "rev-parse", "--show-toplevel")
    worktree_root = _normalize_path(top_level) if top_level else normalized

    main_repo_root: str | None = None
    branch: str | None = None
    head_sha: str | None = None

    if is_linked:
        git_file = Path(worktree_root) / ".git"
        try:
            gitdir_line = git_file.read_text(encoding="utf-8").strip()
            if gitdir_line.startswith("gitdir: "):
                gitdir = _normalize_path(gitdir_line.removeprefix("gitdir: ").strip())
                main_repo_root = _normalize_path(str(Path(gitdir).parent.parent))
        except OSError:
            main_repo_root = None
    else:
        main_repo_root = worktree_root

    _code, branch_out, _stderr = await _run_git(worktree_root, "branch", "--show-current")
    if branch_out:
        branch = branch_out

    _code, head_out, _stderr = await _run_git(worktree_root, "rev-parse", "HEAD")
    if head_out:
        head_sha = head_out

    return {
        "is_git": True,
        "is_linked_worktree": is_linked,
        "path": normalized,
        "worktree_root": worktree_root,
        "main_repo_root": main_repo_root,
        "branch": branch,
        "head_sha": head_sha,
    }


async def list_worktrees(main_repo_root: str) -> list[str]:
    code, stdout, _stderr = await _run_git(main_repo_root, "worktree", "list", "--porcelain")
    if code != 0:
        return []

    paths: list[str] = []
    for line in stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(_normalize_path(line.removeprefix("worktree ").strip()))
    return paths


async def capture_worktree_snapshot(worktree_root: str) -> dict[str, Any]:
    context = await detect_git_context(worktree_root)
    if not context.get("is_linked_worktree"):
        return {"is_linked_worktree": False, "worktree_root": worktree_root}

    root = context["worktree_root"]
    _code, status_out, _stderr = await _run_git(root, "status", "--porcelain=v2")
    _code, diff_stat, _stderr = await _run_git(root, "diff", "--stat", "HEAD")
    _code, staged_stat, _stderr = await _run_git(root, "diff", "--cached", "--stat")

    return {
        "is_linked_worktree": True,
        "worktree_root": root,
        "main_repo_root": context.get("main_repo_root"),
        "branch": context.get("branch"),
        "head_sha": context.get("head_sha"),
        "status_porcelain": status_out,
        "diff_stat": diff_stat,
        "staged_diff_stat": staged_stat,
    }


async def handle_git_worktree_request(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    if action == "detect":
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            return {"ok": False, "error": "path is required"}
        context = await detect_git_context(path)
        return {"ok": True, "context": context}

    if action == "snapshot":
        worktree_root = payload.get("worktree_root")
        if not isinstance(worktree_root, str) or not worktree_root.strip():
            return {"ok": False, "error": "worktree_root is required"}
        snapshot = await capture_worktree_snapshot(worktree_root)
        return {"ok": True, "snapshot": snapshot}

    if action == "list_worktrees":
        main_repo_root = payload.get("main_repo_root")
        if not isinstance(main_repo_root, str) or not main_repo_root.strip():
            return {"ok": False, "error": "main_repo_root is required"}
        worktrees = await list_worktrees(main_repo_root)
        return {"ok": True, "worktrees": worktrees}

    return {"ok": False, "error": f"unknown action: {action}"}
