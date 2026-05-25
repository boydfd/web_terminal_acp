from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260520_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCAL_CLIENT_ID = "00000000-0000-0000-0000-000000000001"
LOCAL_CLIENT_ID_SQLITE = "00000000000000000000000000000001"
LOCAL_CLIENT_UNUSABLE_TOKEN_HASH = (
    "sha256:9e3f0b2a4c1d8f6075b9e2c4a6d8f0137b5c9e1a2d4f6b8c0e3a5d7f9b1c4e6a"
)
CLIENT_STATUS_VALUES = ("ONLINE", "OFFLINE", "ERROR")
CLIENT_RUNTIME_VALUES = ("local", "remote")
WINDOW_STATUS_VALUES = ("ACTIVE", "ARCHIVED", "ERROR", "DISCONNECTED")
EVENT_SOURCE_TYPE_VALUES = ("terminal", "claude_jsonl", "codex_trace", "summary")
SUMMARY_JOB_STATUS_VALUES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED")
AI_SESSION_PROVIDER_VALUES = ("claude", "codex")


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _uuid_type(is_postgresql: bool) -> sa.types.TypeEngine[sa.UUID]:
    if is_postgresql:
        return postgresql.UUID(as_uuid=True)
    return sa.Uuid(as_uuid=True)


def _json_type(is_postgresql: bool) -> sa.types.TypeEngine[object]:
    if is_postgresql:
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _enum_type(
    values: tuple[str, ...], name: str, is_postgresql: bool
) -> sa.types.TypeEngine[object]:
    if is_postgresql:
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _now_default(is_postgresql: bool) -> sa.TextClause:
    if is_postgresql:
        return sa.text("now()")
    return sa.text("CURRENT_TIMESTAMP")


def _local_client_id(is_postgresql: bool) -> str:
    if is_postgresql:
        return LOCAL_CLIENT_ID
    return LOCAL_CLIENT_ID_SQLITE


def upgrade() -> None:
    is_postgresql = _is_postgresql()
    uuid_type = _uuid_type(is_postgresql)
    now_default = _now_default(is_postgresql)
    local_client_id = _local_client_id(is_postgresql)
    clientstatus = _enum_type(CLIENT_STATUS_VALUES, "clientstatus", is_postgresql)
    clientruntime = _enum_type(CLIENT_RUNTIME_VALUES, "clientruntime", is_postgresql)
    windowstatus = _enum_type(WINDOW_STATUS_VALUES, "windowstatus", is_postgresql)
    eventsourcetype = _enum_type(EVENT_SOURCE_TYPE_VALUES, "eventsourcetype", is_postgresql)
    summaryjobstatus = _enum_type(SUMMARY_JOB_STATUS_VALUES, "summaryjobstatus", is_postgresql)

    if is_postgresql:
        bind = op.get_bind()
        clientstatus.create(bind, checkfirst=True)
        clientruntime.create(bind, checkfirst=True)
        windowstatus.create(bind, checkfirst=True)
        eventsourcetype.create(bind, checkfirst=True)
        summaryjobstatus.create(bind, checkfirst=True)

    op.create_table(
        "clients",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", clientstatus, server_default="OFFLINE", nullable=False),
        sa.Column("token_hash", sa.String(length=71), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("install_path", sa.Text(), nullable=True),
        sa.Column("runtime", clientruntime, server_default="remote", nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_clients"),
    )
    op.create_index("ix_clients_status", "clients", ["status"])
    op.create_index("ix_clients_runtime", "clients", ["runtime"])
    op.execute(
        sa.text(
            "INSERT INTO clients (id, name, status, token_hash, runtime) "
            "VALUES (:id, :name, :status, :token_hash, :runtime)"
        ).bindparams(
            id=local_client_id,
            name="local",
            status="ONLINE",
            token_hash=LOCAL_CLIENT_UNUSABLE_TOKEN_HASH,
            runtime="local",
        )
    )

    op.create_table(
        "folders",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("client_id", uuid_type, server_default=local_client_id, nullable=False),
        sa.Column("parent_id", uuid_type, nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name="fk_folders_client_id_clients", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["folders.id"], name="fk_folders_parent_id_folders", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_folders"),
        sa.UniqueConstraint("client_id", "parent_id", "name", name="uq_folders_client_id_parent_id_name"),
        sa.UniqueConstraint("client_id", "path", name="uq_folders_client_id_path"),
    )
    op.create_index("ix_folders_client_id", "folders", ["client_id"])
    op.create_index("ix_folders_parent_id", "folders", ["parent_id"])

    op.create_table(
        "virtual_windows",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("client_id", uuid_type, server_default=local_client_id, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("folder_id", uuid_type, nullable=True),
        sa.Column("status", windowstatus, server_default="ACTIVE", nullable=False),
        sa.Column("tmux_session", sa.String(length=255), nullable=True),
        sa.Column("tmux_window_id", sa.String(length=64), nullable=True),
        sa.Column("remote_session_id", sa.String(length=255), nullable=True),
        sa.Column("remote_window_id", sa.String(length=255), nullable=True),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("shell_command", sa.Text(), nullable=True),
        sa.Column("title_tags", _json_type(is_postgresql), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name="fk_virtual_windows_client_id_clients", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["folder_id"], ["folders.id"], name="fk_virtual_windows_folder_id_folders", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_virtual_windows"),
    )
    op.create_index("ix_virtual_windows_client_id", "virtual_windows", ["client_id"])
    op.create_index("ix_virtual_windows_folder_id", "virtual_windows", ["folder_id"])
    op.create_index("ix_virtual_windows_status", "virtual_windows", ["status"])

    op.create_table(
        "ai_sessions",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("client_id", uuid_type, server_default=local_client_id, nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=512), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("project_path", sa.Text(), nullable=True),
        sa.Column("virtual_window_id", uuid_type, nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("tags", _json_type(is_postgresql), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name="fk_ai_sessions_client_id_clients", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["virtual_window_id"],
            ["virtual_windows.id"],
            name="fk_ai_sessions_virtual_window_id_virtual_windows",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            f"provider IN {AI_SESSION_PROVIDER_VALUES}", name="ck_ai_sessions_provider"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_sessions"),
        sa.UniqueConstraint(
            "client_id", "provider", "source_id", name="uq_ai_sessions_client_id_provider_source_id"
        ),
    )
    op.create_index("ix_ai_sessions_client_id", "ai_sessions", ["client_id"])
    op.create_index("ix_ai_sessions_virtual_window_id", "ai_sessions", ["virtual_window_id"])

    op.create_table(
        "events",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("client_id", uuid_type, server_default=local_client_id, nullable=False),
        sa.Column("source_type", eventsourcetype, nullable=False),
        sa.Column("source_id", sa.String(length=512), nullable=False),
        sa.Column("kind", sa.String(length=128), nullable=False),
        sa.Column("virtual_window_id", uuid_type, nullable=True),
        sa.Column("ai_session_id", uuid_type, nullable=True),
        sa.Column("payload_json", _json_type(is_postgresql), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.ForeignKeyConstraint(
            ["ai_session_id"],
            ["ai_sessions.id"],
            name="fk_events_ai_session_id_ai_sessions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name="fk_events_client_id_clients", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["virtual_window_id"],
            ["virtual_windows.id"],
            name="fk_events_virtual_window_id_virtual_windows",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("client_id", "fingerprint", name="uq_events_client_id_fingerprint"),
    )
    op.create_index("ix_events_client_id", "events", ["client_id"])
    op.create_index("ix_events_virtual_window_id", "events", ["virtual_window_id"])
    op.create_index("ix_events_ai_session_id", "events", ["ai_session_id"])
    op.create_index("ix_events_source_type_source_id", "events", ["source_type", "source_id"])

    op.create_table(
        "summary_jobs",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("virtual_window_id", uuid_type, nullable=False),
        sa.Column("status", summaryjobstatus, server_default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now_default, nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["virtual_window_id"],
            ["virtual_windows.id"],
            name="fk_summary_jobs_virtual_window_id_virtual_windows",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_summary_jobs"),
    )
    active_summary_job_filter = sa.text("status IN ('PENDING', 'RUNNING')")
    op.create_index("ix_summary_jobs_virtual_window_id", "summary_jobs", ["virtual_window_id"])
    op.create_index("ix_summary_jobs_status_run_after", "summary_jobs", ["status", "run_after"])
    op.create_index(
        "uq_summary_jobs_active_virtual_window_id",
        "summary_jobs",
        ["virtual_window_id"],
        unique=True,
        postgresql_where=active_summary_job_filter,
        sqlite_where=active_summary_job_filter,
    )


def downgrade() -> None:
    is_postgresql = _is_postgresql()

    op.drop_table("summary_jobs")
    op.drop_table("events")
    op.drop_table("ai_sessions")
    op.drop_table("virtual_windows")
    op.drop_table("folders")
    op.drop_table("clients")

    if is_postgresql:
        bind = op.get_bind()
        _enum_type(SUMMARY_JOB_STATUS_VALUES, "summaryjobstatus", True).drop(bind, checkfirst=True)
        _enum_type(EVENT_SOURCE_TYPE_VALUES, "eventsourcetype", True).drop(bind, checkfirst=True)
        _enum_type(WINDOW_STATUS_VALUES, "windowstatus", True).drop(bind, checkfirst=True)
        _enum_type(CLIENT_RUNTIME_VALUES, "clientruntime", True).drop(bind, checkfirst=True)
        _enum_type(CLIENT_STATUS_VALUES, "clientstatus", True).drop(bind, checkfirst=True)
