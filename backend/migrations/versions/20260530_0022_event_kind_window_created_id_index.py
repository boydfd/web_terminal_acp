from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260530_0022"
down_revision: str | None = "20260530_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                "ix_events_client_window_kind_created_id "
                "ON events (client_id, virtual_window_id, kind, created_at, id)"
            )
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_events_client_window_kind_created")
        return

    op.create_index(
        "ix_events_client_window_kind_created_id",
        "events",
        ["client_id", "virtual_window_id", "kind", "created_at", "id"],
    )
    op.drop_index("ix_events_client_window_kind_created", table_name="events")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                "ix_events_client_window_kind_created "
                "ON events (client_id, virtual_window_id, kind, created_at)"
            )
            op.execute(
                "DROP INDEX CONCURRENTLY IF EXISTS "
                "ix_events_client_window_kind_created_id"
            )
        return

    op.create_index(
        "ix_events_client_window_kind_created",
        "events",
        ["client_id", "virtual_window_id", "kind", "created_at"],
    )
    op.drop_index("ix_events_client_window_kind_created_id", table_name="events")
