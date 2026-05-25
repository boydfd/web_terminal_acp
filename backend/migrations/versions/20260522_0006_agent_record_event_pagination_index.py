from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260522_0006"
down_revision: str | None = "20260522_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_events_agent_record_window",
        "events",
        ["client_id", "virtual_window_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_events_agent_record_window", table_name="events")
