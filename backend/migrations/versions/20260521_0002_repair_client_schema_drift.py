from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260521_0002"
down_revision: str | None = "20260520_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCAL_CLIENT_ID = "00000000-0000-0000-0000-000000000001"
LOCAL_CLIENT_ID_SQLITE = "00000000000000000000000000000001"
LOCAL_CLIENT_UNUSABLE_TOKEN_HASH = (
    "sha256:9e3f0b2a4c1d8f6075b9e2c4a6d8f0137b5c9e1a2d4f6b8c0e3a5d7f9b1c4e6a"
)


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _uuid_type(is_postgresql: bool) -> sa.types.TypeEngine[sa.UUID]:
    if is_postgresql:
        return postgresql.UUID(as_uuid=True)
    return sa.Uuid(as_uuid=True)


def _local_client_id(is_postgresql: bool) -> str:
    if is_postgresql:
        return LOCAL_CLIENT_ID
    return LOCAL_CLIENT_ID_SQLITE


def _enum_type(values: tuple[str, ...], name: str, is_postgresql: bool) -> sa.types.TypeEngine[object]:
    if is_postgresql:
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _uniques(table: str) -> set[str]:
    return {constraint["name"] for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table)}


def _foreign_keys(table: str) -> set[str]:
    return {constraint["name"] for constraint in sa.inspect(op.get_bind()).get_foreign_keys(table)}


def _drop_unique_if_exists(table: str, name: str) -> None:
    if name in _uniques(table):
        op.drop_constraint(name, table, type_="unique")


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if name not in _indexes(table):
        op.create_index(name, table, columns)


def _create_unique_if_missing(name: str, table: str, columns: list[str]) -> None:
    if name not in _uniques(table):
        op.create_unique_constraint(name, table, columns)


def _create_fk_if_missing(name: str, source: str, referent: str, columns: list[str]) -> None:
    if name not in _foreign_keys(source):
        op.create_foreign_key(name, source, referent, columns, ["id"], ondelete="CASCADE")


def _add_client_id(table: str, uuid_type: sa.types.TypeEngine[sa.UUID]) -> None:
    if "client_id" in _columns(table):
        return
    op.add_column(
        table,
        sa.Column(
            "client_id",
            uuid_type,
            nullable=True,
            server_default=sa.text(f"'{LOCAL_CLIENT_ID}'::uuid")
            if _is_postgresql()
            else LOCAL_CLIENT_ID_SQLITE,
        ),
    )
    client_id = "CAST(:client_id AS uuid)" if _is_postgresql() else ":client_id"
    op.execute(
        sa.text(f"UPDATE {table} SET client_id = {client_id} WHERE client_id IS NULL").bindparams(
            client_id=_local_client_id(_is_postgresql())
        )
    )
    op.alter_column(table, "client_id", nullable=False)


def upgrade() -> None:
    is_postgresql = _is_postgresql()
    uuid_type = _uuid_type(is_postgresql)
    local_client_id = _local_client_id(is_postgresql)
    tables = _tables()

    if is_postgresql:
        bind = op.get_bind()
        _enum_type(("ONLINE", "OFFLINE", "ERROR"), "clientstatus", True).create(bind, checkfirst=True)
        _enum_type(("local", "remote"), "clientruntime", True).create(bind, checkfirst=True)

    if "clients" not in tables:
        op.create_table(
            "clients",
            sa.Column("id", uuid_type, nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("status", _enum_type(("ONLINE", "OFFLINE", "ERROR"), "clientstatus", is_postgresql), server_default="OFFLINE", nullable=False),
            sa.Column("token_hash", sa.String(length=71), nullable=False),
            sa.Column("hostname", sa.String(length=255), nullable=True),
            sa.Column("install_path", sa.Text(), nullable=True),
            sa.Column("runtime", _enum_type(("local", "remote"), "clientruntime", is_postgresql), server_default="remote", nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()") if is_postgresql else sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()") if is_postgresql else sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_clients"),
        )
    _create_index_if_missing("ix_clients_status", "clients", ["status"])
    _create_index_if_missing("ix_clients_runtime", "clients", ["runtime"])
    op.execute(
        sa.text(
            "INSERT INTO clients (id, name, status, token_hash, runtime) "
            "VALUES (CAST(:id AS uuid), 'local', 'ONLINE', :token_hash, 'local') "
            "ON CONFLICT (id) DO NOTHING"
        ).bindparams(id=local_client_id, token_hash=LOCAL_CLIENT_UNUSABLE_TOKEN_HASH)
        if is_postgresql
        else sa.text(
            "INSERT OR IGNORE INTO clients (id, name, status, token_hash, runtime) "
            "VALUES (:id, 'local', 'ONLINE', :token_hash, 'local')"
        ).bindparams(id=local_client_id, token_hash=LOCAL_CLIENT_UNUSABLE_TOKEN_HASH)
    )

    if "folders" in tables:
        _add_client_id("folders", uuid_type)
        _drop_unique_if_exists("folders", "folders_path_key")
        _drop_unique_if_exists("folders", "uq_folders_parent_id_name")
        _create_unique_if_missing("uq_folders_client_id_parent_id_name", "folders", ["client_id", "parent_id", "name"])
        _create_unique_if_missing("uq_folders_client_id_path", "folders", ["client_id", "path"])
        _create_index_if_missing("ix_folders_client_id", "folders", ["client_id"])
        _create_fk_if_missing("fk_folders_client_id_clients", "folders", "clients", ["client_id"])

    if "virtual_windows" in tables:
        _add_client_id("virtual_windows", uuid_type)
        cols = _columns("virtual_windows")
        if "remote_session_id" not in cols:
            op.add_column("virtual_windows", sa.Column("remote_session_id", sa.String(length=255), nullable=True))
        if "remote_window_id" not in cols:
            op.add_column("virtual_windows", sa.Column("remote_window_id", sa.String(length=255), nullable=True))
        _create_index_if_missing("ix_virtual_windows_client_id", "virtual_windows", ["client_id"])
        _create_fk_if_missing("fk_virtual_windows_client_id_clients", "virtual_windows", "clients", ["client_id"])

    if "ai_sessions" in tables:
        _add_client_id("ai_sessions", uuid_type)
        _drop_unique_if_exists("ai_sessions", "uq_ai_sessions_provider_source_id")
        _create_unique_if_missing("uq_ai_sessions_client_id_provider_source_id", "ai_sessions", ["client_id", "provider", "source_id"])
        _create_index_if_missing("ix_ai_sessions_client_id", "ai_sessions", ["client_id"])
        _create_fk_if_missing("fk_ai_sessions_client_id_clients", "ai_sessions", "clients", ["client_id"])

    if "events" in tables:
        _add_client_id("events", uuid_type)
        _drop_unique_if_exists("events", "events_fingerprint_key")
        _create_unique_if_missing("uq_events_client_id_fingerprint", "events", ["client_id", "fingerprint"])
        _create_index_if_missing("ix_events_client_id", "events", ["client_id"])
        _create_fk_if_missing("fk_events_client_id_clients", "events", "clients", ["client_id"])


def downgrade() -> None:
    pass
