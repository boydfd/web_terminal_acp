from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260524_0010"
down_revision: str | None = "20260523_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "terminal_recent_usages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("window_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["window_id"], ["virtual_windows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "window_id", name="uq_terminal_recent_usages_client_window"),
    )
    op.create_index(
        "ix_terminal_recent_usages_client_last_used",
        "terminal_recent_usages",
        ["client_id", "last_used_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_terminal_recent_usages_client_last_used", table_name="terminal_recent_usages")
    op.drop_table("terminal_recent_usages")
