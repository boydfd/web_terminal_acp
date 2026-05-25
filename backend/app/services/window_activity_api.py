from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VirtualWindow
from app.schemas import ClientWindowsActivityOut, GitWorktreeActivityOut, WindowActivityOut
from app.services.terminal_work_status import (
    load_tree_window_activity,
    long_idle_work_status,
    to_work_status_out,
)
from app.services.git_worktree_coordinator import load_git_worktree_activity_for_window
from app.services.runtime.client_connections import ClientConnectionRegistry
from app.services.window_runtime_tags import runtime_tags_for_window


async def load_client_windows_activity(
    session: AsyncSession,
    client_id: UUID,
    *,
    include_runtime_tags: bool = False,
    registry: ClientConnectionRegistry | None = None,
) -> ClientWindowsActivityOut:
    window_ids = list(
        await session.scalars(
            select(VirtualWindow.id).where(
                VirtualWindow.client_id == client_id,
                VirtualWindow.folder_id.is_not(None),
            )
        )
    )
    if not window_ids:
        return ClientWindowsActivityOut()

    activity = await load_tree_window_activity(
        session,
        client_id,
        window_ids,
        include_runtime_tags=include_runtime_tags,
    )
    windows = list(
        await session.scalars(
            select(VirtualWindow).where(VirtualWindow.id.in_(window_ids))
        )
    )
    windows_by_id = {window.id: window for window in windows}

    items: list[WindowActivityOut] = []
    for window_id in window_ids:
        window = windows_by_id.get(window_id)
        if window is None:
            continue
        work_status = activity.work_statuses.get(window_id, long_idle_work_status())
        if include_runtime_tags:
            runtime_tags = runtime_tags_for_window(
                window,
                ai_session=activity.latest_ai_sessions.get(window_id),
                terminal_agent=activity.latest_terminal_agents.get(window_id),
            )
        else:
            runtime_tags = runtime_tags_for_window(window)
        git_worktree_payload = await load_git_worktree_activity_for_window(
            session,
            client_id=client_id,
            window_id=window_id,
            registry=registry,
        )
        git_worktree = (
            GitWorktreeActivityOut(**git_worktree_payload) if git_worktree_payload is not None else None
        )
        items.append(
            WindowActivityOut(
                window_id=window_id,
                work_status=to_work_status_out(work_status),
                runtime_tags=runtime_tags,
                last_agent_task_completed_at=activity.last_agent_task_completed_at.get(
                    window_id
                ),
                git_worktree=git_worktree,
            )
        )
    return ClientWindowsActivityOut(windows=items)
