from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260522_0007"
down_revision: str | None = "20260522_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("clients", "version")
