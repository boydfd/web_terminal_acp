from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260530_0024"
down_revision: str | None = "20260530_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_registration_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key_hash", sa.String(length=71), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "USED",
                name="clientregistrationkeystatus",
                create_constraint=True,
            ),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column("label", sa.String(length=255), nullable=True),
        sa.Column("used_client_id", sa.Uuid(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["used_client_id"], ["clients.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index(
        "ix_client_registration_keys_key_hash",
        "client_registration_keys",
        ["key_hash"],
        unique=False,
    )
    op.create_index(
        "ix_client_registration_keys_status",
        "client_registration_keys",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_client_registration_keys_status", table_name="client_registration_keys")
    op.drop_index("ix_client_registration_keys_key_hash", table_name="client_registration_keys")
    op.drop_table("client_registration_keys")
