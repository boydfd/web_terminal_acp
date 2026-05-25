from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260524_0012"
down_revision: str | None = "20260524_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_events_client_window_kind_created",
        "events",
        ["client_id", "virtual_window_id", "kind", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_events_client_window_kind_created", table_name="events")
