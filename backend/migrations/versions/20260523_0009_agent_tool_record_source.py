from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260523_0009"
down_revision: str | None = "20260523_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AI_SESSION_PROVIDER_VALUES = ("claude", "codex")
EVENT_SOURCE_TYPE_VALUES = ("terminal", "claude_jsonl", "codex_trace", "summary")
EVENT_SOURCE_TYPE_VALUES_WITH_AGENT_TOOL_RECORD = (
    "terminal",
    "claude_jsonl",
    "codex_trace",
    "summary",
    "agent_tool_record",
)
LEGACY_PROVIDER_SQL = "('claude', 'codex')"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _sqlite_event_source_type(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name="eventsourcetype", native_enum=False, create_constraint=True)


def upgrade() -> None:
    if _is_postgresql():
        op.execute(sa.text("ALTER TYPE eventsourcetype ADD VALUE IF NOT EXISTS 'agent_tool_record'"))
        op.drop_constraint("ck_ai_sessions_provider", "ai_sessions", type_="check")
        return

    with op.batch_alter_table("events") as batch_op:
        batch_op.alter_column(
            "source_type",
            existing_type=_sqlite_event_source_type(EVENT_SOURCE_TYPE_VALUES),
            type_=_sqlite_event_source_type(EVENT_SOURCE_TYPE_VALUES_WITH_AGENT_TOOL_RECORD),
            existing_nullable=False,
        )

    with op.batch_alter_table("ai_sessions") as batch_op:
        batch_op.drop_constraint("ck_ai_sessions_provider", type_="check")


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE events SET ai_session_id = NULL "
            "WHERE ai_session_id IN "
            "(SELECT id FROM ai_sessions WHERE provider NOT IN " + LEGACY_PROVIDER_SQL + ")"
        )
    )
    op.execute(sa.text("DELETE FROM ai_sessions WHERE provider NOT IN " + LEGACY_PROVIDER_SQL))
    op.execute(sa.text("DELETE FROM events WHERE source_type = 'agent_tool_record'"))

    if _is_postgresql():
        op.create_check_constraint(
            "ck_ai_sessions_provider",
            "ai_sessions",
            f"provider IN {AI_SESSION_PROVIDER_VALUES}",
        )
        return


    with op.batch_alter_table("events") as batch_op:
        batch_op.alter_column(
            "source_type",
            existing_type=_sqlite_event_source_type(EVENT_SOURCE_TYPE_VALUES_WITH_AGENT_TOOL_RECORD),
            type_=_sqlite_event_source_type(EVENT_SOURCE_TYPE_VALUES),
            existing_nullable=False,
        )

    with op.batch_alter_table("ai_sessions") as batch_op:
        batch_op.create_check_constraint(
            "ck_ai_sessions_provider",
            f"provider IN {AI_SESSION_PROVIDER_VALUES}",
        )
