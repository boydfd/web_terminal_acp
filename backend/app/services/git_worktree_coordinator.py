from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.git_worktree import (
    create_git_worktree_run,
    get_git_worktree_run,
    get_window_git_binding,
    upsert_window_git_binding,
    window_has_pending_commit,
)
from app.services.git_worktree_client import request_git_worktree_action
from app.services.git_worktree_ops import (
    compute_session_diff,
    parse_git_worktree_add_path,
    pending_commit_from_diff,
    pending_commit_from_live_snapshot,
)
from app.services.runtime.client_connections import ClientConnectionRegistry
from app.services.window_runtime_tags import agent_from_command

logger = logging.getLogger(__name__)


async def bind_worktree_for_window(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
    worktree_root: str,
    main_repo_root: str | None,
    branch: str | None,
    discovery_method: str,
    registry: ClientConnectionRegistry | None,
) -> bool:
    normalized_root = os.path.realpath(worktree_root)
    main_root = main_repo_root
    if not main_root:
        detect = await request_git_worktree_action(
            registry,
            client_id,
            action="detect",
            path=normalized_root,
        )
        if detect and detect.get("ok"):
            context = detect.get("context") or {}
            main_root = context.get("main_repo_root")
            branch = branch or context.get("branch")
        if not main_root:
            return False

    await upsert_window_git_binding(
        session,
        client_id=client_id,
        window_id=window_id,
        main_repo_root=os.path.realpath(main_root),
        worktree_root=normalized_root,
        branch=branch,
        discovery_method=discovery_method,
    )
    return True


async def process_worktree_registration(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
    marker: dict[str, Any],
    registry: ClientConnectionRegistry | None,
) -> None:
    worktree_root = marker.get("worktree_root")
    if not isinstance(worktree_root, str) or not worktree_root.strip():
        return
    branch = marker.get("branch") if isinstance(marker.get("branch"), str) else None
    main_repo_root = marker.get("main_repo_root") if isinstance(marker.get("main_repo_root"), str) else None
    await bind_worktree_for_window(
        session,
        client_id=client_id,
        window_id=window_id,
        worktree_root=worktree_root,
        main_repo_root=main_repo_root,
        branch=branch,
        discovery_method="osc",
        registry=registry,
    )
    await _complete_awaiting_runs_after_bind(session, client_id, window_id, registry)


async def process_terminal_commands_for_git(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
    commands: list[dict[str, Any]],
    registry: ClientConnectionRegistry | None,
) -> None:
    for command in commands:
        phase = command.get("phase")
        raw_command = command.get("command")
        cwd = command.get("cwd") if isinstance(command.get("cwd"), str) else None
        sequence = command.get("sequence")
        if sequence is None:
            continue
        sequence_str = str(sequence)

        if phase == "started" and isinstance(raw_command, str):
            agent = agent_from_command(raw_command)
            if agent is None:
                continue
            existing = await get_git_worktree_run(session, window_id, sequence_str)
            if existing is None:
                await create_git_worktree_run(
                    session,
                    client_id=client_id,
                    window_id=window_id,
                    command_sequence=sequence_str,
                    agent_provider=agent,
                )

            worktree_path = parse_git_worktree_add_path(raw_command, cwd)
            if worktree_path:
                await bind_worktree_for_window(
                    session,
                    client_id=client_id,
                    window_id=window_id,
                    worktree_root=worktree_path,
                    main_repo_root=None,
                    branch=None,
                    discovery_method="command",
                    registry=registry,
                )
                await _complete_awaiting_runs_after_bind(session, client_id, window_id, registry)
                continue

            if cwd:
                await _try_bind_from_path(
                    session,
                    client_id=client_id,
                    window_id=window_id,
                    path=cwd,
                    discovery_method="cwd",
                    registry=registry,
                )
            continue

        if phase == "finished" and isinstance(raw_command, str):
            agent = agent_from_command(raw_command)
            if agent is None:
                continue
            await _finish_agent_run(
                session,
                client_id=client_id,
                window_id=window_id,
                sequence_str=sequence_str,
                registry=registry,
            )


async def _try_bind_from_path(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
    path: str,
    discovery_method: str,
    registry: ClientConnectionRegistry | None,
) -> None:
    detect = await request_git_worktree_action(registry, client_id, action="detect", path=path)
    if not detect or not detect.get("ok"):
        return
    context = detect.get("context") or {}
    if not context.get("is_linked_worktree"):
        return
    worktree_root = context.get("worktree_root")
    if not isinstance(worktree_root, str):
        return
    await bind_worktree_for_window(
        session,
        client_id=client_id,
        window_id=window_id,
        worktree_root=worktree_root,
        main_repo_root=context.get("main_repo_root"),
        branch=context.get("branch") if isinstance(context.get("branch"), str) else None,
        discovery_method=discovery_method,
        registry=registry,
    )
    await _complete_awaiting_runs_after_bind(session, client_id, window_id, registry)


async def _complete_awaiting_runs_after_bind(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    registry: ClientConnectionRegistry | None,
) -> None:
    binding = await get_window_git_binding(session, window_id)
    if binding is None:
        return

    from sqlalchemy import select

    from app.models import GitWorktreeRun

    runs = list(
        await session.scalars(
            select(GitWorktreeRun).where(
                GitWorktreeRun.virtual_window_id == window_id,
                GitWorktreeRun.status == "awaiting_worktree",
            )
        )
    )
    for run in runs:
        await _start_run_snapshot(session, run, binding, client_id, registry)


async def _start_run_snapshot(
    session: AsyncSession,
    run: Any,
    binding: Any,
    client_id: UUID,
    registry: ClientConnectionRegistry | None,
) -> None:
    result = await request_git_worktree_action(
        registry,
        client_id,
        action="snapshot",
        worktree_root=binding.worktree_root,
    )
    if not result or not result.get("ok"):
        return
    snapshot = result.get("snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("is_linked_worktree"):
        return

    run.status = "bound"
    run.main_repo_root = binding.main_repo_root
    run.worktree_root = binding.worktree_root
    run.discovery_method = binding.discovery_method
    run.start_snapshot_json = snapshot
    await session.flush()


async def _finish_agent_run(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
    sequence_str: str,
    registry: ClientConnectionRegistry | None,
) -> None:
    run = await get_git_worktree_run(session, window_id, sequence_str)
    if run is None:
        return

    binding = await get_window_git_binding(session, window_id)
    if binding is None:
        run.status = "no_worktree"
        run.ended_at = datetime.now(UTC)
        await session.flush()
        return

    if run.status == "awaiting_worktree":
        await _start_run_snapshot(session, run, binding, client_id, registry)

    if not run.worktree_root:
        run.worktree_root = binding.worktree_root
        run.main_repo_root = binding.main_repo_root

    result = await request_git_worktree_action(
        registry,
        client_id,
        action="snapshot",
        worktree_root=run.worktree_root or binding.worktree_root,
    )
    snapshot = result.get("snapshot") if result and result.get("ok") else None
    if isinstance(snapshot, dict):
        run.end_snapshot_json = snapshot
        session_diff = compute_session_diff(run.start_snapshot_json, run.end_snapshot_json)
        run.session_diff_json = session_diff
        run.pending_commit = pending_commit_from_diff(session_diff)
        if not run.pending_commit:
            run.resolved_at = datetime.now(UTC)

    run.status = "completed"
    run.ended_at = datetime.now(UTC)
    await session.flush()


async def load_git_worktree_activity_for_window(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
    registry: ClientConnectionRegistry | None,
) -> dict[str, Any] | None:
    binding = await get_window_git_binding(session, window_id)
    if binding is None:
        return None

    if registry is not None:
        result = await request_git_worktree_action(
            registry,
            client_id,
            action="detect",
            path=binding.worktree_root,
        )
        if result and result.get("ok"):
            context = result.get("context") or {}
            if not context.get("is_linked_worktree"):
                return None
            binding.branch = context.get("branch") or binding.branch

        live = await request_git_worktree_action(
            registry,
            client_id,
            action="snapshot",
            worktree_root=binding.worktree_root,
        )
        if live and live.get("ok"):
            snapshot = live.get("snapshot") or {}
            pending = await window_has_pending_commit(session, window_id)
            if pending:
                still_pending = pending_commit_from_live_snapshot(snapshot)
                if not still_pending:
                    from sqlalchemy import update

                    from app.models import GitWorktreeRun

                    await session.execute(
                        update(GitWorktreeRun)
                        .where(
                            GitWorktreeRun.virtual_window_id == window_id,
                            GitWorktreeRun.pending_commit.is_(True),
                        )
                        .values(pending_commit=False, resolved_at=datetime.now(UTC))
                    )

    pending_commit = await window_has_pending_commit(session, window_id)
    return {
        "worktree_root": binding.worktree_root,
        "main_repo_root": binding.main_repo_root,
        "branch": binding.branch,
        "pending_commit": pending_commit,
    }
