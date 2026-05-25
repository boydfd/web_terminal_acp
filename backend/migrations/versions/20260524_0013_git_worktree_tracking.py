from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260524_0013"
down_revision: str | None = "20260524_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "window_git_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("virtual_window_id", sa.Uuid(), nullable=False),
        sa.Column("main_repo_root", sa.Text(), nullable=False),
        sa.Column("worktree_root", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("discovery_method", sa.String(length=32), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["virtual_window_id"], ["virtual_windows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("virtual_window_id", name="uq_window_git_bindings_virtual_window_id"),
    )
    op.create_index("ix_window_git_bindings_client_id", "window_git_bindings", ["client_id"])

    op.create_table(
        "git_worktree_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("virtual_window_id", sa.Uuid(), nullable=False),
        sa.Column("command_sequence", sa.String(length=64), nullable=False),
        sa.Column("agent_provider", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("main_repo_root", sa.Text(), nullable=True),
        sa.Column("worktree_root", sa.Text(), nullable=True),
        sa.Column("discovery_method", sa.String(length=32), nullable=True),
        sa.Column("start_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("end_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("session_diff_json", sa.JSON(), nullable=True),
        sa.Column("pending_commit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["virtual_window_id"], ["virtual_windows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "virtual_window_id",
            "command_sequence",
            name="uq_git_worktree_runs_window_sequence",
        ),
    )
    op.create_index(
        "ix_git_worktree_runs_client_window_started",
        "git_worktree_runs",
        ["client_id", "virtual_window_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_git_worktree_runs_client_window_started", table_name="git_worktree_runs")
    op.drop_table("git_worktree_runs")
    op.drop_index("ix_window_git_bindings_client_id", table_name="window_git_bindings")
    op.drop_table("window_git_bindings")
