from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AiSession


async def get_or_create_ai_session(
    session: AsyncSession,
    *,
    client_id: UUID,
    provider: str,
    source_id: str,
    source_path: str | None = None,
    project_path: str | None = None,
    virtual_window_id: UUID | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
) -> AiSession:
    ai_session = await _select_ai_session(session, client_id, provider, source_id)
    if ai_session is not None:
        _update_ai_session(
            ai_session,
            source_path=source_path,
            project_path=project_path,
            virtual_window_id=virtual_window_id,
            title=title,
            tags=tags,
        )
        await session.flush()
        return ai_session

    ai_session = AiSession(
        client_id=client_id,
        provider=provider,
        source_id=source_id,
        source_path=source_path,
        project_path=project_path,
        virtual_window_id=virtual_window_id,
        title=title,
        tags=tags,
    )
    try:
        async with session.begin_nested():
            session.add(ai_session)
            await session.flush()
    except IntegrityError as exc:
        existing_session = await _select_ai_session(session, client_id, provider, source_id)
        if existing_session is not None:
            _update_ai_session(
                existing_session,
                source_path=source_path,
                project_path=project_path,
                virtual_window_id=virtual_window_id,
                title=title,
                tags=tags,
            )
            await session.flush()
            return existing_session
        raise exc

    return ai_session


async def _select_ai_session(
    session: AsyncSession,
    client_id: UUID,
    provider: str,
    source_id: str,
) -> AiSession | None:
    return await session.scalar(
        select(AiSession).where(
            AiSession.client_id == client_id,
            AiSession.provider == provider,
            AiSession.source_id == source_id,
        )
    )


def _update_ai_session(
    ai_session: AiSession,
    *,
    source_path: str | None,
    project_path: str | None,
    virtual_window_id: UUID | None,
    title: str | None,
    tags: list[str] | None,
) -> None:
    if source_path is not None:
        ai_session.source_path = source_path
    if project_path is not None:
        ai_session.project_path = project_path
    if virtual_window_id is not None:
        ai_session.virtual_window_id = virtual_window_id
    if title is not None:
        ai_session.title = title
    if tags is not None:
        ai_session.tags = tags
