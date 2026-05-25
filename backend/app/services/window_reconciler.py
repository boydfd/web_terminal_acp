from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WindowStatus
from app.repositories.windows import list_active_windows
from app.services.tmux_manager import TmuxManager, TmuxTarget

logger = logging.getLogger(__name__)
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def mark_missing_tmux_windows_error(
    session_factory: SessionFactory,
    tmux_manager: TmuxManager,
) -> int:
    marked_count = 0
    async with session_factory() as session:
        for window in await list_active_windows(session):
            if window.tmux_session is None or window.tmux_window_id is None:
                window.status = WindowStatus.error
                marked_count += 1
                continue

            exists = await tmux_manager.has_window(
                TmuxTarget(session=window.tmux_session, window_id=window.tmux_window_id)
            )
            if not exists:
                window.status = WindowStatus.error
                marked_count += 1

        await session.commit()

    if marked_count > 0:
        logger.info("marked missing tmux windows as error", extra={"marked_count": marked_count})
    return marked_count
