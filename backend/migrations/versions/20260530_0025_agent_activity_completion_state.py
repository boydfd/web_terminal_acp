from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260530_0025"
down_revision: str | None = "20260530_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "virtual_windows",
        sa.Column("agent_activity_latest_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        """
        UPDATE virtual_windows AS vw
        SET
            agent_activity_latest_at = latest.activity_at,
            agent_activity_latest_event_id = latest.id
        FROM (
            SELECT DISTINCT ON (client_id, virtual_window_id)
                id,
                client_id,
                virtual_window_id,
                created_at AS activity_at,
                created_at
            FROM events
            WHERE source_type IN ('agent_tool_record', 'codex_trace', 'claude_jsonl')
              AND virtual_window_id IS NOT NULL
            ORDER BY client_id, virtual_window_id, activity_at DESC, created_at DESC, id DESC
        ) AS latest
        WHERE vw.client_id = latest.client_id
          AND vw.id = latest.virtual_window_id
        """
    )


def downgrade() -> None:
    op.drop_column("virtual_windows", "agent_activity_latest_completed_at")
