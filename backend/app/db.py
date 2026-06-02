from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.model_base import Base

__all__ = [
    "Base",
    "SessionLocal",
    "engine",
    "get_session",
    "prefer_deferred_commit",
]

engine = create_async_engine(get_settings().database_url, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def prefer_deferred_commit(session: AsyncSession) -> None:
    """Relax commit fsync for replayable event/projection writes on PostgreSQL."""
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(text("SET LOCAL synchronous_commit = OFF"))
