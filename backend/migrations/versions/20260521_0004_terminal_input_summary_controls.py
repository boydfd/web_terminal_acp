from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260521_0004"
down_revision: str | None = "20260521_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "virtual_windows",
        sa.Column(
            "title_manually_overridden",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "virtual_windows",
        sa.Column(
            "folder_manually_overridden",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column("summary_jobs", sa.Column("trigger_reason", sa.String(length=128), nullable=True))
    op.add_column(
        "summary_jobs",
        sa.Column(
            "allow_title_folder_override",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "summary_jobs",
        sa.Column("input_generation", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("summary_jobs", "input_generation")
    op.drop_column("summary_jobs", "allow_title_folder_override")
    op.drop_column("summary_jobs", "trigger_reason")
    op.drop_column("virtual_windows", "folder_manually_overridden")
    op.drop_column("virtual_windows", "title_manually_overridden")
