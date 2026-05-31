from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260531_0029"
down_revision: str | None = "20260531_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ensure_terminal_notification_states_table() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "terminal_notification_states" in inspector.get_table_names():
        return

    op.create_table(
        "terminal_notification_states",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("window_id", sa.Uuid(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["window_id"], ["virtual_windows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "window_id", name="uq_terminal_notification_states_client_window"),
    )
    op.create_index(
        "ix_terminal_notification_states_client_id",
        "terminal_notification_states",
        ["client_id"],
    )


def upgrade() -> None:
    _ensure_terminal_notification_states_table()

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
              AND NOT (
                  CASE
                      WHEN payload_json::text LIKE '%\\u0000%' THEN false
                      ELSE
                          coalesce(payload_json ->> 'provider', 'claude_code') IN ('claude', 'claude_code')
                          AND payload_json ->> 'type' = 'user'
                          AND (
                              payload_json ->> 'isMeta' = 'true'
                              OR ltrim(payload_json #>> '{message,content}') LIKE '<local-command-caveat>%'
                              OR ltrim(payload_json #>> '{message,content}') LIKE '<bash-input>%'
                              OR ltrim(payload_json #>> '{message,content}') LIKE '<bash-stdout>%'
                              OR ltrim(payload_json #>> '{message,content}') LIKE '<bash-stderr>%'
                          )
                  END
              )
            ORDER BY client_id, virtual_window_id, activity_at DESC, created_at DESC, id DESC
        ) AS latest
        WHERE vw.client_id = latest.client_id
          AND vw.id = latest.virtual_window_id
        """
    )
    op.execute(
        """
        UPDATE virtual_windows AS vw
        SET agent_activity_latest_completed_at = latest.completed_at
        FROM (
            SELECT
                client_id,
                virtual_window_id,
                max(created_at) AS completed_at
            FROM events
            WHERE source_type IN ('agent_tool_record', 'claude_jsonl')
              AND virtual_window_id IS NOT NULL
              AND CASE
                  WHEN payload_json::text LIKE '%\\u0000%' THEN false
                  ELSE
                      coalesce(payload_json ->> 'provider', 'claude_code') IN ('claude', 'claude_code')
                      AND payload_json ->> 'type' = 'system'
                      AND payload_json ->> 'subtype' = 'turn_duration'
              END
            GROUP BY client_id, virtual_window_id
        ) AS latest
        WHERE vw.client_id = latest.client_id
          AND vw.id = latest.virtual_window_id
          AND (
              vw.agent_activity_latest_completed_at IS NULL
              OR latest.completed_at >= vw.agent_activity_latest_completed_at
          )
        """
    )


def downgrade() -> None:
    pass
