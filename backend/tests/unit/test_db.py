import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.db import prefer_deferred_commit


class _FakeDialect:
    name = "postgresql"


class _FakeBind:
    dialect = _FakeDialect()


class _FakePostgresqlSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def get_bind(self) -> _FakeBind:
        return _FakeBind()

    async def execute(self, statement):  # noqa: ANN001
        self.statements.append(str(statement))


@pytest.mark.asyncio
async def test_prefer_deferred_commit_skips_non_postgresql() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def capture_statement(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    async with AsyncSession(engine) as session:
        await prefer_deferred_commit(session)

    await engine.dispose()

    assert statements == []


@pytest.mark.asyncio
async def test_prefer_deferred_commit_sets_postgresql_local_synchronous_commit() -> None:
    session = _FakePostgresqlSession()

    await prefer_deferred_commit(session)  # type: ignore[arg-type]

    assert session.statements == ["SET LOCAL synchronous_commit = OFF"]
