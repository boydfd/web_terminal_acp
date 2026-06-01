from __future__ import annotations

from datetime import UTC, datetime
from math import ceil
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, TerminalRecentUsage, VirtualWindow

MAX_TERMINAL_RECENTS = 1000
DEFAULT_TERMINAL_RECENTS_PAGE_SIZE = 20


async def touch_terminal_recent(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
    title: str,
) -> TerminalRecentUsage | None:
    window = await session.get(VirtualWindow, window_id)
    if window is None or window.client_id != client_id:
        return None

    now = datetime.now(UTC)
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        usage = await _upsert_terminal_recent_postgresql(
            session,
            client_id=client_id,
            window_id=window_id,
            title=title,
            last_used_at=now,
        )
    elif dialect_name == "sqlite":
        usage = await _upsert_terminal_recent_sqlite(
            session,
            client_id=client_id,
            window_id=window_id,
            title=title,
            last_used_at=now,
        )
    else:
        usage = await _upsert_terminal_recent_fallback(
            session,
            client_id=client_id,
            window_id=window_id,
            title=title,
            last_used_at=now,
        )

    await _trim_terminal_recents(session, client_id)
    return usage


async def _upsert_terminal_recent_postgresql(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
    title: str,
    last_used_at: datetime,
) -> TerminalRecentUsage:
    stmt = (
        postgresql_insert(TerminalRecentUsage)
        .values(
            id=uuid4(),
            client_id=client_id,
            window_id=window_id,
            title=title,
            last_used_at=last_used_at,
        )
        .on_conflict_do_update(
            constraint="uq_terminal_recent_usages_client_window",
            set_={"title": title, "last_used_at": last_used_at},
        )
        .returning(TerminalRecentUsage.id)
    )
    usage_id = (await session.execute(stmt)).scalar_one()
    usage = await session.get(TerminalRecentUsage, usage_id)
    if usage is None:
        raise RuntimeError("terminal recent upsert did not return a persisted row")
    return usage


async def _upsert_terminal_recent_sqlite(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
    title: str,
    last_used_at: datetime,
) -> TerminalRecentUsage:
    stmt = (
        sqlite_insert(TerminalRecentUsage)
        .values(
            id=uuid4(),
            client_id=client_id,
            window_id=window_id,
            title=title,
            last_used_at=last_used_at,
        )
        .on_conflict_do_update(
            index_elements=["client_id", "window_id"],
            set_={"title": title, "last_used_at": last_used_at},
        )
    )
    await session.execute(stmt)
    return await _get_terminal_recent(session, client_id=client_id, window_id=window_id)


async def _upsert_terminal_recent_fallback(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
    title: str,
    last_used_at: datetime,
) -> TerminalRecentUsage:
    existing = await session.scalar(
        select(TerminalRecentUsage).where(
            TerminalRecentUsage.client_id == client_id,
            TerminalRecentUsage.window_id == window_id,
        )
    )
    if existing is None:
        usage = TerminalRecentUsage(
            client_id=client_id,
            window_id=window_id,
            title=title,
            last_used_at=last_used_at,
        )
        session.add(usage)
    else:
        existing.title = title
        existing.last_used_at = last_used_at
        usage = existing

    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        usage = await _get_terminal_recent(session, client_id=client_id, window_id=window_id)
        usage.title = title
        usage.last_used_at = last_used_at
        await session.flush()
    return usage


async def _get_terminal_recent(
    session: AsyncSession,
    *,
    client_id: UUID,
    window_id: UUID,
) -> TerminalRecentUsage:
    usage = await session.scalar(
        select(TerminalRecentUsage).where(
            TerminalRecentUsage.client_id == client_id,
            TerminalRecentUsage.window_id == window_id,
        )
    )
    if usage is None:
        raise RuntimeError("terminal recent upsert did not return a persisted row")
    return usage


def _recent_title_search_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _terminal_recents_base_query(client_id: UUID, query: str | None):
    stmt = (
        select(TerminalRecentUsage, VirtualWindow.title)
        .outerjoin(VirtualWindow, TerminalRecentUsage.window_id == VirtualWindow.id)
        .where(TerminalRecentUsage.client_id == client_id)
    )
    if query:
        pattern = _recent_title_search_pattern(query)
        stmt = stmt.where(
            or_(
                TerminalRecentUsage.title.ilike(pattern, escape="\\"),
                VirtualWindow.title.ilike(pattern, escape="\\"),
            )
        )
    return stmt


async def list_terminal_recents(
    session: AsyncSession,
    *,
    client_id: UUID,
    page: int,
    page_size: int,
    query: str | None = None,
) -> tuple[list[tuple[TerminalRecentUsage, str]], int]:
    normalized_query = query.strip() if query else None
    if normalized_query == "":
        normalized_query = None

    count_stmt = select(func.count(TerminalRecentUsage.id)).select_from(TerminalRecentUsage)
    count_stmt = count_stmt.outerjoin(
        VirtualWindow, TerminalRecentUsage.window_id == VirtualWindow.id
    ).where(TerminalRecentUsage.client_id == client_id)
    if normalized_query:
        pattern = _recent_title_search_pattern(normalized_query)
        count_stmt = count_stmt.where(
            or_(
                TerminalRecentUsage.title.ilike(pattern, escape="\\"),
                VirtualWindow.title.ilike(pattern, escape="\\"),
            )
        )
    total_count = int(await session.scalar(count_stmt) or 0)
    if total_count == 0:
        return [], 0

    offset = (page - 1) * page_size
    rows = await session.execute(
        _terminal_recents_base_query(client_id, normalized_query)
        .order_by(TerminalRecentUsage.last_used_at.desc(), TerminalRecentUsage.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    return [(usage, window_title or usage.title) for usage, window_title in rows.all()], total_count


def _global_terminal_recents_base_query(query: str | None):
    stmt = (
        select(TerminalRecentUsage, VirtualWindow.title, Client.name)
        .outerjoin(VirtualWindow, TerminalRecentUsage.window_id == VirtualWindow.id)
        .join(Client, TerminalRecentUsage.client_id == Client.id)
    )
    if query:
        pattern = _recent_title_search_pattern(query)
        stmt = stmt.where(
            or_(
                TerminalRecentUsage.title.ilike(pattern, escape="\\"),
                VirtualWindow.title.ilike(pattern, escape="\\"),
                Client.name.ilike(pattern, escape="\\"),
            )
        )
    return stmt


async def list_global_terminal_recents(
    session: AsyncSession,
    *,
    page: int,
    page_size: int,
    query: str | None = None,
) -> tuple[list[tuple[TerminalRecentUsage, str, str]], int]:
    normalized_query = query.strip() if query else None
    if normalized_query == "":
        normalized_query = None

    count_stmt = (
        select(func.count(TerminalRecentUsage.id))
        .select_from(TerminalRecentUsage)
        .outerjoin(VirtualWindow, TerminalRecentUsage.window_id == VirtualWindow.id)
        .join(Client, TerminalRecentUsage.client_id == Client.id)
    )
    if normalized_query:
        pattern = _recent_title_search_pattern(normalized_query)
        count_stmt = count_stmt.where(
            or_(
                TerminalRecentUsage.title.ilike(pattern, escape="\\"),
                VirtualWindow.title.ilike(pattern, escape="\\"),
                Client.name.ilike(pattern, escape="\\"),
            )
        )
    total_count = int(await session.scalar(count_stmt) or 0)
    if total_count == 0:
        return [], 0

    offset = (page - 1) * page_size
    rows = await session.execute(
        _global_terminal_recents_base_query(normalized_query)
        .order_by(TerminalRecentUsage.last_used_at.desc(), TerminalRecentUsage.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    return [
        (usage, window_title or usage.title, client_name)
        for usage, window_title, client_name in rows.all()
    ], total_count


def total_pages(total: int, page_size: int) -> int:
    if total <= 0:
        return 0
    return ceil(total / page_size)


async def _trim_terminal_recents(session: AsyncSession, client_id: UUID) -> None:
    keep_ids = await session.scalars(
        select(TerminalRecentUsage.id)
        .where(TerminalRecentUsage.client_id == client_id)
        .order_by(TerminalRecentUsage.last_used_at.desc(), TerminalRecentUsage.id.desc())
        .limit(MAX_TERMINAL_RECENTS)
    )
    keep_id_list = list(keep_ids)
    if not keep_id_list:
        return

    await session.execute(
        delete(TerminalRecentUsage).where(
            TerminalRecentUsage.client_id == client_id,
            TerminalRecentUsage.id.not_in(keep_id_list),
        )
    )
