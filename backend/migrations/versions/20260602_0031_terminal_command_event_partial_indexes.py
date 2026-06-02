from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260602_0031"
down_revision: str | None = "20260531_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                "ix_events_terminal_input_window_created "
                "ON events (client_id, virtual_window_id, created_at, id) "
                "WHERE kind = 'terminal_input_command'"
            )
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                "ix_events_terminal_finished_window_created "
                "ON events (client_id, virtual_window_id, created_at, id) "
                "WHERE kind = 'terminal_command_finished'"
            )
        return

    op.create_index(
        "ix_events_terminal_input_window_created",
        "events",
        ["client_id", "virtual_window_id", "created_at", "id"],
        sqlite_where=sa.text("kind = 'terminal_input_command'"),
    )
    op.create_index(
        "ix_events_terminal_finished_window_created",
        "events",
        ["client_id", "virtual_window_id", "created_at", "id"],
        sqlite_where=sa.text("kind = 'terminal_command_finished'"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "DROP INDEX CONCURRENTLY IF EXISTS "
                "ix_events_terminal_finished_window_created"
            )
            op.execute(
                "DROP INDEX CONCURRENTLY IF EXISTS "
                "ix_events_terminal_input_window_created"
            )
        return

    op.drop_index("ix_events_terminal_finished_window_created", table_name="events")
    op.drop_index("ix_events_terminal_input_window_created", table_name="events")
