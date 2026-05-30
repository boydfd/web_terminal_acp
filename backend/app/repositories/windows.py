from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import desc, func as sa_func
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AiSession,
    Event,
    Folder,
    GitWorktreeRun,
    SummaryJob,
    TerminalRecentUsage,
    VirtualWindow,
    WindowGitBinding,
    WindowTitleHistory,
    WindowStatus,
)
from app.repositories.folders import ensure_default_folder, prune_empty_folder_branch


class FolderNotFoundError(Exception):
    pass


_UNSET = object()


def fallback_terminal_title(now: datetime | None = None) -> str:
    timestamp = now or datetime.now()
    return f"Terminal-{timestamp:%H:%M}"


def _should_record_title_history(
    window: VirtualWindow,
    *,
    title: str | None | object,
    summary: str | None | object,
) -> bool:
    next_title = window.title if title is _UNSET or title is None else title
    next_summary = window.summary if summary is _UNSET else summary
    return next_title != window.title or next_summary != window.summary


async def record_window_title_history(
    session: AsyncSession,
    window: VirtualWindow,
    *,
    source: str,
) -> WindowTitleHistory:
    history = WindowTitleHistory(
        client_id=window.client_id,
        virtual_window_id=window.id,
        title=window.title,
        summary=window.summary,
        source=source,
        created_at=datetime.now(UTC),
    )
    session.add(history)
    await session.flush()
    return history


async def _window_title_history_exists(session: AsyncSession, window: VirtualWindow) -> bool:
    history_id = await session.scalar(
        select(WindowTitleHistory.id)
        .where(
            WindowTitleHistory.client_id == window.client_id,
            WindowTitleHistory.virtual_window_id == window.id,
        )
        .limit(1)
    )
    return history_id is not None


async def _record_baseline_title_history_if_missing(
    session: AsyncSession,
    window: VirtualWindow,
) -> None:
    if not await _window_title_history_exists(session, window):
        await record_window_title_history(session, window, source="baseline")


async def list_window_title_history(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    *,
    limit: int,
    offset: int,
) -> tuple[list[WindowTitleHistory], int]:
    filters = (
        WindowTitleHistory.client_id == client_id,
        WindowTitleHistory.virtual_window_id == window_id,
    )
    total = await session.scalar(select(sa_func.count()).select_from(WindowTitleHistory).where(*filters))
    items = list(
        await session.scalars(
            select(WindowTitleHistory)
            .where(*filters)
            .order_by(desc(WindowTitleHistory.created_at), desc(WindowTitleHistory.id))
            .offset(offset)
            .limit(limit)
        )
    )
    return items, int(total or 0)


async def create_window(
    session: AsyncSession,
    client_id: UUID,
    cwd: str | None,
    shell_command: str | None,
    window_id: UUID | None = None,
    tmux_session: str | None = None,
    tmux_window_id: str | None = None,
    remote_session_id: str | None = None,
    remote_window_id: str | None = None,
) -> VirtualWindow:
    folder = await ensure_default_folder(session, client_id)
    window = VirtualWindow(
        id=window_id,
        client_id=client_id,
        title=fallback_terminal_title(),
        folder_id=folder.id,
        status=WindowStatus.active,
        tmux_session=tmux_session,
        tmux_window_id=tmux_window_id,
        remote_session_id=remote_session_id,
        remote_window_id=remote_window_id,
        cwd=cwd,
        shell_command=shell_command,
    )
    session.add(window)
    await session.flush()
    await record_window_title_history(session, window, source="initial")
    return window


async def get_window(session: AsyncSession, window_id: UUID) -> VirtualWindow | None:
    return await session.get(VirtualWindow, window_id)


async def get_window_for_client(
    session: AsyncSession, client_id: UUID, window_id: UUID
) -> VirtualWindow | None:
    return await session.scalar(
        select(VirtualWindow).where(
            VirtualWindow.id == window_id,
            VirtualWindow.client_id == client_id,
        )
    )


async def get_window_for_local_tmux_target(
    session: AsyncSession,
    client_id: UUID,
    *,
    tmux_session: str,
    tmux_window_id: str,
) -> VirtualWindow | None:
    return await session.scalar(
        select(VirtualWindow).where(
            VirtualWindow.client_id == client_id,
            VirtualWindow.tmux_session == tmux_session,
            VirtualWindow.tmux_window_id == tmux_window_id,
        )
    )


async def patch_window(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    *,
    folder_id: UUID | None | object = _UNSET,
    title: str | None | object = _UNSET,
    status: str | None | object = _UNSET,
    summary: str | None | object = _UNSET,
    title_tags: list[str] | None | object = _UNSET,
    title_history_source: str = "manual",
) -> VirtualWindow | None:
    window = await get_window_for_client(session, client_id, window_id)
    if window is None:
        return None
    record_history = _should_record_title_history(
        window,
        title=title,
        summary=summary,
    )
    if record_history:
        await _record_baseline_title_history_if_missing(session, window)

    if folder_id is not _UNSET:
        if folder_id is None:
            window.folder_id = None
        else:
            folder = await session.scalar(
                select(Folder).where(
                    Folder.id == folder_id,
                    Folder.client_id == client_id,
                )
            )
            if folder is None:
                raise FolderNotFoundError("folder not found")
            window.folder_id = folder.id
        window.folder_manually_overridden = True

    if title is not _UNSET and title is not None:
        window.title = title
        window.title_manually_overridden = True
    if status is not _UNSET and status is not None:
        window.status = WindowStatus(status)
    if summary is not _UNSET:
        window.summary = summary
    if title_tags is not _UNSET:
        window.title_tags = title_tags

    await session.flush()
    if record_history:
        await record_window_title_history(session, window, source=title_history_source)
    return window


async def patch_runtime_window(
    session: AsyncSession,
    client_id: UUID,
    window_id: UUID,
    *,
    tmux_session: str | None | object = _UNSET,
    tmux_window_id: str | None | object = _UNSET,
    remote_session_id: str | None | object = _UNSET,
    remote_window_id: str | None | object = _UNSET,
    cwd: str | None | object = _UNSET,
    shell_command: str | None | object = _UNSET,
) -> VirtualWindow | None:
    window = await get_window_for_client(session, client_id, window_id)
    if window is None:
        return None

    if tmux_session is not _UNSET:
        window.tmux_session = tmux_session
    if tmux_window_id is not _UNSET:
        window.tmux_window_id = tmux_window_id
    if remote_session_id is not _UNSET:
        window.remote_session_id = remote_session_id
    if remote_window_id is not _UNSET:
        window.remote_window_id = remote_window_id
    if cwd is not _UNSET:
        window.cwd = cwd
    if shell_command is not _UNSET:
        window.shell_command = shell_command

    await session.flush()
    return window


async def delete_window(
    session: AsyncSession, client_id: UUID, window_id: UUID
) -> bool:
    window = await get_window_for_client(session, client_id, window_id)
    if window is None:
        return False
    folder_id = window.folder_id

    await session.execute(sa_delete(SummaryJob).where(SummaryJob.virtual_window_id == window_id))
    await session.execute(
        sa_delete(TerminalRecentUsage).where(TerminalRecentUsage.window_id == window_id)
    )
    await session.execute(
        sa_delete(WindowTitleHistory).where(WindowTitleHistory.virtual_window_id == window_id)
    )
    await session.execute(
        sa_delete(WindowGitBinding).where(WindowGitBinding.virtual_window_id == window_id)
    )
    await session.execute(
        sa_delete(GitWorktreeRun).where(GitWorktreeRun.virtual_window_id == window_id)
    )
    await session.execute(
        sa_update(AiSession)
        .where(AiSession.virtual_window_id == window_id)
        .values(virtual_window_id=None)
    )
    await session.execute(
        sa_update(Event)
        .where(Event.virtual_window_id == window_id)
        .values(virtual_window_id=None)
    )
    result = await session.execute(
        sa_delete(VirtualWindow).where(
            VirtualWindow.id == window_id,
            VirtualWindow.client_id == client_id,
        )
    )
    if result.rowcount != 1:
        return False
    if folder_id is not None:
        await prune_empty_folder_branch(session, client_id, folder_id)
    await session.flush()
    return True


async def list_active_windows(session: AsyncSession) -> list[VirtualWindow]:
    return list(
        await session.scalars(
            select(VirtualWindow)
            .where(VirtualWindow.status == WindowStatus.active)
            .order_by(VirtualWindow.created_at, VirtualWindow.title, VirtualWindow.id)
        )
    )
