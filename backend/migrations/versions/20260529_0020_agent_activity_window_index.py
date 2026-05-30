from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260529_0020"
down_revision: str | None = "20260529_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
                "ix_events_agent_activity_window_created "
                "ON events (client_id, virtual_window_id, created_at, id) "
                "WHERE source_type IN ('agent_tool_record', 'codex_trace', 'claude_jsonl')"
            )
        return

    op.create_index(
        "ix_events_agent_activity_window_created",
        "events",
        ["client_id", "virtual_window_id", "created_at", "id"],
        sqlite_where=sa.text(
            "source_type IN ('agent_tool_record', 'codex_trace', 'claude_jsonl')"
        ),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_events_agent_activity_window_created")
        return

    op.drop_index("ix_events_agent_activity_window_created", table_name="events")
