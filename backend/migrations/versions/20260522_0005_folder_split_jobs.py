from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260522_0005"
down_revision: str | None = "20260521_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOLDER_SPLIT_JOB_STATUS_VALUES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED")


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _uuid_type(is_postgresql: bool) -> sa.types.TypeEngine[sa.UUID]:
    if is_postgresql:
        return postgresql.UUID(as_uuid=True)
    return sa.Uuid(as_uuid=True)


def _status_enum(is_postgresql: bool) -> sa.types.TypeEngine[object]:
    if is_postgresql:
        return postgresql.ENUM(
            *FOLDER_SPLIT_JOB_STATUS_VALUES,
            name="foldersplitjobstatus",
            create_type=False,
        )
    return sa.Enum(
        *FOLDER_SPLIT_JOB_STATUS_VALUES,
        name="foldersplitjobstatus",
        native_enum=False,
        create_constraint=True,
    )


def _now_default(is_postgresql: bool) -> sa.TextClause:
    if is_postgresql:
        return sa.text("now()")
    return sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    is_postgresql = _is_postgresql()
    uuid_type = _uuid_type(is_postgresql)
    status_enum = _status_enum(is_postgresql)
    now_default = _now_default(is_postgresql)

    if is_postgresql:
        bind = op.get_bind()
        status_enum.create(bind, checkfirst=True)

    op.create_table(
        "folder_split_jobs",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("client_id", uuid_type, nullable=False),
        sa.Column("folder_id", uuid_type, nullable=False),
        sa.Column("status", status_enum, server_default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name="fk_folder_split_jobs_client_id_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["folders.id"],
            name="fk_folder_split_jobs_folder_id_folders",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_folder_split_jobs"),
    )
    op.create_index("ix_folder_split_jobs_client_id", "folder_split_jobs", ["client_id"])
    op.create_index("ix_folder_split_jobs_folder_id", "folder_split_jobs", ["folder_id"])
    op.create_index(
        "ix_folder_split_jobs_status_run_after",
        "folder_split_jobs",
        ["status", "run_after"],
    )
    op.create_index(
        "uq_folder_split_jobs_active_folder_id",
        "folder_split_jobs",
        ["folder_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
        sqlite_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )


def downgrade() -> None:
    is_postgresql = _is_postgresql()
    op.drop_index("uq_folder_split_jobs_active_folder_id", table_name="folder_split_jobs")
    op.drop_index("ix_folder_split_jobs_status_run_after", table_name="folder_split_jobs")
    op.drop_index("ix_folder_split_jobs_folder_id", table_name="folder_split_jobs")
    op.drop_index("ix_folder_split_jobs_client_id", table_name="folder_split_jobs")
    op.drop_table("folder_split_jobs")
    if is_postgresql:
        postgresql.ENUM(name="foldersplitjobstatus").drop(op.get_bind(), checkfirst=True)
