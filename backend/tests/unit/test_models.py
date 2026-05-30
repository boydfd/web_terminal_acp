import importlib
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.model_base import Base
from app.models import (
    AiSession,
    Client,
    ClientRuntime,
    ClientStatus,
    Event,
    EventSourceType,
    Folder,
    LOCAL_CLIENT_ID,
    SummaryJob,
    SummaryJobStatus,
    VirtualWindow,
    WindowTitleHistory,
    WindowStatus,
)
from app.repositories.clients import hash_client_token


BACKEND_DIR = Path(__file__).resolve().parents[2]
initial_migration = importlib.import_module("migrations.versions.20260520_0001_initial")


def _uuid_hex() -> str:
    return uuid.uuid4().hex


def _insert_valid_window(connection, window_id: str) -> None:
    connection.exec_driver_sql(
        "INSERT INTO virtual_windows (id, title, status) VALUES (?, ?, ?)",
        (window_id, "Terminal-15:30", WindowStatus.active.value),
    )


def _insert_valid_ai_session(connection, provider: str) -> None:
    connection.exec_driver_sql(
        "INSERT INTO ai_sessions (id, provider, source_id) VALUES (?, ?, ?)",
        (_uuid_hex(), provider, f"{provider}-session"),
    )


def _assert_arbitrary_providers_accepted(engine) -> None:
    with engine.begin() as connection:
        _insert_valid_ai_session(connection, "claude")
        _insert_valid_ai_session(connection, "codex")
        _insert_valid_ai_session(connection, "cursor_cli")
        _insert_valid_ai_session(connection, "openai")

    with engine.connect() as connection:
        providers = connection.exec_driver_sql(
            "SELECT provider FROM ai_sessions ORDER BY provider"
        ).scalars()
        assert list(providers) == ["claude", "codex", "cursor_cli", "openai"]


def _assert_event_fingerprint_scoped_by_client(engine) -> None:
    first_client_id = _uuid_hex()
    second_client_id = _uuid_hex()
    fingerprint = "shared-fingerprint"

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO clients (id, name, status, token_hash, runtime) VALUES (?, ?, ?, ?, ?)",
            (first_client_id, "client-a", "ONLINE", f"sha256:{'1' * 64}", "remote"),
        )
        connection.exec_driver_sql(
            "INSERT INTO clients (id, name, status, token_hash, runtime) VALUES (?, ?, ?, ?, ?)",
            (second_client_id, "client-b", "ONLINE", f"sha256:{'2' * 64}", "remote"),
        )
        connection.exec_driver_sql(
            "INSERT INTO events "
            "(id, client_id, source_type, source_id, kind, payload_json, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_uuid_hex(), first_client_id, "terminal", "pane-1", "output", "{}", fingerprint),
        )
        connection.exec_driver_sql(
            "INSERT INTO events "
            "(id, client_id, source_type, source_id, kind, payload_json, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_uuid_hex(), second_client_id, "terminal", "pane-1", "output", "{}", fingerprint),
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO events "
                "(id, client_id, source_type, source_id, kind, payload_json, fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_uuid_hex(), first_client_id, "terminal", "pane-2", "output", "{}", fingerprint),
            )


def _assert_invalid_metadata_values_rejected(engine) -> None:
    window_id = _uuid_hex()
    with engine.begin() as connection:
        _insert_valid_window(connection, window_id)

    invalid_inserts = [
        (
            "INSERT INTO virtual_windows (id, title, status) VALUES (?, ?, ?)",
            (_uuid_hex(), "bad window", "BROKEN"),
        ),
        (
            "INSERT INTO events "
            "(id, source_type, source_id, kind, payload_json, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_uuid_hex(), "bad_source", "source-1", "output", "{}", _uuid_hex()),
        ),
        (
            "INSERT INTO summary_jobs (id, virtual_window_id, status) VALUES (?, ?, ?)",
            (_uuid_hex(), window_id, "BROKEN"),
        ),
    ]

    for statement, parameters in invalid_inserts:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.exec_driver_sql(statement, parameters)


def test_folder_has_materialized_path():
    folder = Folder(name="生产排障", path="/2026-05/生产排障")
    assert folder.name == "生产排障"
    assert folder.path == "/2026-05/生产排障"


def test_client_status_and_runtime_values_are_stable():
    assert ClientStatus.ONLINE.value == "ONLINE"
    assert ClientStatus.OFFLINE.value == "OFFLINE"
    assert ClientStatus.ERROR.value == "ERROR"
    assert ClientRuntime.local.value == "local"
    assert ClientRuntime.remote.value == "remote"


def test_window_status_values_are_stable():
    assert WindowStatus.active.value == "ACTIVE"
    assert WindowStatus.archived.value == "ARCHIVED"
    assert WindowStatus.error.value == "ERROR"
    assert WindowStatus.disconnected.value == "DISCONNECTED"


def test_event_source_type_values_are_stable():
    assert EventSourceType.terminal.value == "terminal"
    assert EventSourceType.claude_jsonl.value == "claude_jsonl"
    assert EventSourceType.codex_trace.value == "codex_trace"
    assert EventSourceType.summary.value == "summary"
    assert EventSourceType.agent_tool_record.value == "agent_tool_record"


def test_virtual_window_references_folder_id():
    window = VirtualWindow(title="Terminal-15:30", folder_id=None)
    assert window.folder_id is None


def test_model_enum_columns_persist_values():
    client_status_values = Client.__table__.c.status.type.enums
    assert client_status_values == ["ONLINE", "OFFLINE", "ERROR"]

    client_runtime_values = Client.__table__.c.runtime.type.enums
    assert client_runtime_values == ["local", "remote"]

    window_values = VirtualWindow.__table__.c.status.type.enums
    assert window_values == ["ACTIVE", "ARCHIVED", "ERROR", "DISCONNECTED"]

    event_source_values = Event.__table__.c.source_type.type.enums
    assert event_source_values == [
        "terminal",
        "claude_jsonl",
        "codex_trace",
        "summary",
        "agent_tool_record",
    ]

    summary_job_status_values = SummaryJob.__table__.c.status.type.enums
    assert summary_job_status_values == ["PENDING", "RUNNING", "SUCCEEDED", "FAILED"]


def test_metadata_schema_constraints_match_spec():
    folder_constraints = {
        tuple(constraint.columns.keys())
        for constraint in Folder.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("client_id", "path") in folder_constraints
    assert ("client_id", "parent_id", "name") in folder_constraints

    event_constraints = {
        tuple(constraint.columns.keys())
        for constraint in Event.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("client_id", "fingerprint") in event_constraints
    assert ("fingerprint",) not in event_constraints

    expected_indexes = {
        ("clients", ("status",)),
        ("clients", ("runtime",)),
        ("folders", ("client_id",)),
        ("folders", ("parent_id",)),
        ("virtual_windows", ("client_id",)),
        ("virtual_windows", ("folder_id",)),
        ("virtual_windows", ("status",)),
        ("ai_sessions", ("client_id",)),
        ("ai_sessions", ("virtual_window_id",)),
        ("events", ("client_id",)),
        ("events", ("virtual_window_id",)),
        ("events", ("ai_session_id",)),
        ("events", ("source_type", "source_id")),
        ("events", ("source_type", "created_at", "id")),
        ("events", ("client_id", "virtual_window_id", "source_type", "created_at", "id")),
        ("events", ("client_id", "virtual_window_id", "kind", "created_at", "id")),
        ("summary_jobs", ("virtual_window_id",)),
        ("summary_jobs", ("status", "run_after")),
        ("window_title_history", ("client_id", "virtual_window_id", "created_at", "id")),
    }
    actual_indexes = {
        (table.name, tuple(index.columns.keys()))
        for table in Base.metadata.tables.values()
        for index in table.indexes
    }
    assert expected_indexes <= actual_indexes
    agent_activity_index = next(
        index
        for index in Event.__table__.indexes
        if index.name == "ix_events_agent_activity_window_created"
    )
    assert tuple(agent_activity_index.columns.keys()) == (
        "client_id",
        "virtual_window_id",
        "created_at",
        "id",
    )
    assert agent_activity_index.dialect_options["postgresql"]["where"] is not None
    assert agent_activity_index.dialect_options["sqlite"]["where"] is not None

    assert Client.__table__.c.status.server_default is not None
    assert Client.__table__.c.runtime.server_default is not None
    assert Folder.__table__.c.client_id.nullable is False
    assert VirtualWindow.__table__.c.client_id.nullable is False
    assert VirtualWindow.__table__.c.remote_session_id.nullable is True
    assert VirtualWindow.__table__.c.remote_window_id.nullable is True
    assert VirtualWindow.__table__.c.title_manually_overridden.nullable is False
    assert VirtualWindow.__table__.c.folder_manually_overridden.nullable is False
    assert VirtualWindow.__table__.c.agent_activity_latest_at.nullable is True
    assert VirtualWindow.__table__.c.agent_activity_latest_event_id.nullable is True
    assert VirtualWindow.__table__.c.agent_activity_latest_completed_at.nullable is True
    assert VirtualWindow.__table__.c.agent_activity_burst_start_at.nullable is True
    assert VirtualWindow.__table__.c.agent_activity_generation.nullable is False
    assert AiSession.__table__.c.client_id.nullable is False
    assert Event.__table__.c.client_id.nullable is False
    assert Base.metadata.tables["ui_settings"].c.value_json.nullable is False

    assert Folder.__table__.c.sort_order.server_default is not None
    assert VirtualWindow.__table__.c.status.server_default is not None
    assert VirtualWindow.__table__.c.title_manually_overridden.server_default is not None
    assert VirtualWindow.__table__.c.folder_manually_overridden.server_default is not None
    assert VirtualWindow.__table__.c.agent_activity_generation.server_default is not None
    assert SummaryJob.__table__.c.status.server_default is not None
    assert SummaryJob.__table__.c.attempts.server_default is not None
    assert SummaryJob.__table__.c.allow_title_folder_override.nullable is False
    assert SummaryJob.__table__.c.allow_title_folder_override.server_default is not None
    assert SummaryJob.__table__.c.input_generation.nullable is False
    assert SummaryJob.__table__.c.input_generation.server_default is not None

    assert Event.__table__.c.payload_json.nullable is False
    assert Event.__table__.c.indexed_at.nullable is True
    assert Event.__table__.c.indexed_at.server_default is None

    assert SummaryJob.__table__.c.trigger_reason.nullable is True
    assert SummaryJob.__table__.c.run_after.nullable is True
    assert SummaryJob.__table__.c.run_after.server_default is None
    assert WindowTitleHistory.__table__.c.client_id.nullable is False
    assert WindowTitleHistory.__table__.c.virtual_window_id.nullable is False
    assert WindowTitleHistory.__table__.c.title.nullable is False
    assert WindowTitleHistory.__table__.c.summary.nullable is True
    assert WindowTitleHistory.__table__.c.source.nullable is False


def test_sqlite_create_all_persists_models_and_relationships():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        client = Client(
            id=LOCAL_CLIENT_ID,
            name="local",
            status=ClientStatus.ONLINE,
            token_hash=f"sha256:{'0' * 64}",
            runtime=ClientRuntime.local,
        )
        folder = Folder(name="生产排障", path="/2026-05/生产排障", client=client)
        window = VirtualWindow(
            title="Terminal-15:30",
            client=client,
            folder=folder,
            status=WindowStatus.archived,
            title_tags=["prod", "incident"],
        )
        ai_session = AiSession(
            provider="claude",
            source_id="session-1",
            client=client,
            virtual_window=window,
            tags=["triage"],
        )
        event = Event(
            source_type=EventSourceType.terminal,
            source_id="pane-1",
            kind="output",
            client=client,
            virtual_window=window,
            ai_session=ai_session,
            payload_json={"line": "ok", "nested": {"count": 1}},
            fingerprint="event-1",
        )
        job = SummaryJob(
            virtual_window=window,
            status=SummaryJobStatus.pending,
        )
        title_history = WindowTitleHistory(
            client=client,
            virtual_window=window,
            title="Terminal-15:30",
            summary="Initial summary.",
            source="summary",
        )
        session.add_all([folder, window, ai_session, event, job, title_history])
        session.commit()

        assert client.id == LOCAL_CLIENT_ID
        assert folder.id is not None
        assert folder.client_id == client.id
        assert window.id is not None
        assert window.client_id == client.id
        assert ai_session.id is not None
        assert ai_session.client_id == client.id
        assert event.id is not None
        assert event.client_id == client.id
        assert job.id is not None

    with Session(engine) as session:
        loaded_window = session.scalars(
            select(VirtualWindow).where(VirtualWindow.title == "Terminal-15:30")
        ).one()
        assert loaded_window.status is WindowStatus.archived
        assert loaded_window.title_tags == ["prod", "incident"]
        assert loaded_window.title_manually_overridden is False
        assert loaded_window.folder_manually_overridden is False
        assert loaded_window.client is not None
        assert loaded_window.client.name == "local"
        assert loaded_window.folder is not None
        assert loaded_window.folder.name == "生产排障"

        loaded_event = session.scalars(select(Event).where(Event.fingerprint == "event-1")).one()
        assert loaded_event.source_type is EventSourceType.terminal
        assert loaded_event.payload_json == {"line": "ok", "nested": {"count": 1}}
        assert loaded_event.ai_session is not None
        assert loaded_event.ai_session.provider == "claude"

        loaded_job = session.scalars(
            select(SummaryJob).where(SummaryJob.virtual_window_id == loaded_window.id)
        ).one()
        assert loaded_job.status is SummaryJobStatus.pending
        assert loaded_job.trigger_reason is None
        assert loaded_job.allow_title_folder_override is False
        assert loaded_job.input_generation == 0
        assert loaded_job.virtual_window.title == "Terminal-15:30"
        loaded_title_history = session.scalars(
            select(WindowTitleHistory).where(WindowTitleHistory.virtual_window_id == loaded_window.id)
        ).one()
        assert loaded_title_history.client_id == loaded_window.client_id
        assert loaded_title_history.title == "Terminal-15:30"
        assert loaded_title_history.summary == "Initial summary."
        assert loaded_title_history.source == "summary"


def test_sqlite_folder_sibling_unique_constraint_rejects_duplicate_names():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        parent = Folder(name="root", path="/root")
        session.add(parent)
        session.flush()

        session.add_all(
            [
                Folder(name="duplicate", path="/root/one", parent_id=parent.id),
                Folder(name="duplicate", path="/root/two", parent_id=parent.id),
            ]
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_sqlite_create_all_scopes_event_fingerprints_by_client():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    _assert_event_fingerprint_scoped_by_client(engine)


def test_sqlite_create_all_rejects_invalid_metadata_enum_values():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    _assert_invalid_metadata_values_rejected(engine)


def test_sqlite_create_all_accepts_arbitrary_ai_session_providers():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    _assert_arbitrary_providers_accepted(engine)


def test_initial_postgresql_local_client_seed_casts_uuid():
    statement = initial_migration._seed_local_client_statement(is_postgresql=True)

    sql = str(statement)
    assert f"'{LOCAL_CLIENT_ID}'::uuid" in sql
    assert "'ONLINE'::clientstatus" in sql
    assert "'local'::clientruntime" in sql
    assert statement.compile().params == {
        "token_hash": initial_migration.LOCAL_CLIENT_UNUSABLE_TOKEN_HASH
    }


def test_sqlite_alembic_migration_adds_terminal_summary_control_defaults(tmp_path, monkeypatch):
    database_path = tmp_path / "migrated-summary-controls.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()

    try:
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()

    window_id = _uuid_hex()
    job_id = _uuid_hex()
    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    with engine.begin() as connection:
        _insert_valid_window(connection, window_id)
        connection.exec_driver_sql(
            "INSERT INTO summary_jobs (id, virtual_window_id) VALUES (?, ?)",
            (job_id, window_id),
        )
        window_row = connection.exec_driver_sql(
            "SELECT title_manually_overridden, folder_manually_overridden "
            "FROM virtual_windows WHERE id = ?",
            (window_id,),
        ).mappings().one()
        job_row = connection.exec_driver_sql(
            "SELECT trigger_reason, allow_title_folder_override, input_generation "
            "FROM summary_jobs WHERE id = ?",
            (job_id,),
        ).mappings().one()

    assert window_row["title_manually_overridden"] == 0
    assert window_row["folder_manually_overridden"] == 0
    assert job_row["trigger_reason"] is None
    assert job_row["allow_title_folder_override"] == 0
    assert job_row["input_generation"] == 0


def test_sqlite_alembic_migration_seeds_local_client_parent_row(tmp_path, monkeypatch):
    database_path = tmp_path / "migrated-local-client.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()

    try:
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()

    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        row = connection.exec_driver_sql(
            "SELECT id, name, status, runtime, token_hash FROM clients WHERE id = ?",
            (LOCAL_CLIENT_ID.hex,),
        ).mappings().one()
        assert row["name"] == "local"
        assert row["status"] == ClientStatus.ONLINE.value
        assert row["runtime"] == ClientRuntime.local.value
        assert row["token_hash"].startswith("sha256:")
        assert row["token_hash"] != hash_client_token("local-client-token")

        connection.exec_driver_sql(
            "INSERT INTO folders (id, name, path) VALUES (?, ?, ?)",
            (_uuid_hex(), "default-client-folder", "/default-client-folder"),
        )

    with Session(engine) as session:
        assert session.get(Client, LOCAL_CLIENT_ID) is not None
        assert session.scalars(select(Client).where(Client.runtime == ClientRuntime.local)).all() == [
            session.get(Client, LOCAL_CLIENT_ID)
        ]


def test_sqlite_alembic_migrated_schema_creates_ui_settings(tmp_path, monkeypatch):
    database_path = tmp_path / "migrated-ui-settings.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()

    try:
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()

    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO ui_settings (key, value_json) VALUES (?, ?)",
            ("custom_quick_keys", '{"quick_keys": []}'),
        )
        row = connection.exec_driver_sql(
            "SELECT value_json FROM ui_settings WHERE key = ?",
            ("custom_quick_keys",),
        ).mappings().one()

    assert row["value_json"] == '{"quick_keys": []}'


def test_sqlite_alembic_migrated_schema_creates_window_title_history(tmp_path, monkeypatch):
    database_path = tmp_path / "migrated-title-history.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()

    try:
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()

    window_id = _uuid_hex()
    history_id = _uuid_hex()
    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    with engine.begin() as connection:
        _insert_valid_window(connection, window_id)
        connection.exec_driver_sql(
            "INSERT INTO window_title_history "
            "(id, client_id, virtual_window_id, title, summary, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                history_id,
                LOCAL_CLIENT_ID.hex,
                window_id,
                "Terminal-15:30",
                "Initial summary.",
                "summary",
            ),
        )
        row = connection.exec_driver_sql(
            "SELECT title, summary, source FROM window_title_history WHERE id = ?",
            (history_id,),
        ).mappings().one()

    assert row["title"] == "Terminal-15:30"
    assert row["summary"] == "Initial summary."
    assert row["source"] == "summary"


def test_sqlite_alembic_migrated_schema_scopes_event_fingerprints_by_client(tmp_path, monkeypatch):
    database_path = tmp_path / "migrated-event-fingerprint.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()

    try:
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()

    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    _assert_event_fingerprint_scoped_by_client(engine)


def test_sqlite_alembic_migrated_schema_rejects_invalid_metadata_enum_values(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "migrated.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()

    try:
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()

    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    _assert_invalid_metadata_values_rejected(engine)


def test_sqlite_alembic_migrated_schema_accepts_arbitrary_ai_session_providers(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "migrated.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()

    try:
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()

    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    _assert_arbitrary_providers_accepted(engine)


def test_sqlite_alembic_agent_tool_record_downgrade_removes_rows_and_reupgrades(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "agent-tool-record-downgrade.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))

    def run_alembic(action: str, revision: str) -> None:
        get_settings.cache_clear()
        try:
            getattr(command, action)(config, revision)
        finally:
            get_settings.cache_clear()

    run_alembic("upgrade", "head")

    legacy_session_id = _uuid_hex()
    non_legacy_session_id = _uuid_hex()
    engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO ai_sessions (id, provider, source_id) VALUES (?, ?, ?)",
            (legacy_session_id, "claude", "legacy-session"),
        )
        connection.exec_driver_sql(
            "INSERT INTO ai_sessions (id, provider, source_id) VALUES (?, ?, ?)",
            (non_legacy_session_id, "cursor_cli", "generic-session"),
        )
        connection.exec_driver_sql(
            "INSERT INTO events "
            "(id, source_type, source_id, kind, ai_session_id, payload_json, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _uuid_hex(),
                "agent_tool_record",
                "session-1",
                "message",
                legacy_session_id,
                "{}",
                "agent-event-1",
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO events "
            "(id, source_type, source_id, kind, ai_session_id, payload_json, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _uuid_hex(),
                "codex_trace",
                "legacy-linked",
                "message",
                legacy_session_id,
                "{}",
                "legacy-event-1",
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO events "
            "(id, source_type, source_id, kind, ai_session_id, payload_json, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _uuid_hex(),
                "codex_trace",
                "generic-linked",
                "message",
                non_legacy_session_id,
                "{}",
                "generic-linked-event-1",
            ),
        )
    engine.dispose()

    run_alembic("downgrade", "20260523_0008")

    downgraded_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    with downgraded_engine.connect() as connection:
        agent_rows = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM events WHERE source_type = 'agent_tool_record'"
        ).scalar_one()
        non_legacy_sessions = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM ai_sessions WHERE provider NOT IN ('claude', 'codex')"
        ).scalar_one()
        legacy_event_ai_session_id = connection.exec_driver_sql(
            "SELECT ai_session_id FROM events WHERE fingerprint = 'legacy-event-1'"
        ).scalar_one()
        generic_linked_event_ai_session_id = connection.exec_driver_sql(
            "SELECT ai_session_id FROM events WHERE fingerprint = 'generic-linked-event-1'"
        ).scalar_one()
    assert agent_rows == 0
    assert non_legacy_sessions == 0
    assert legacy_event_ai_session_id == legacy_session_id
    assert generic_linked_event_ai_session_id is None

    with pytest.raises(IntegrityError):
        with downgraded_engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO events "
                "(id, source_type, source_id, kind, payload_json, fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_uuid_hex(), "agent_tool_record", "session-2", "message", "{}", "agent-event-2"),
            )
    downgraded_engine.dispose()

    run_alembic("upgrade", "head")

    reupgraded_engine = create_engine(f"sqlite+pysqlite:///{database_path}")
    with reupgraded_engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO events "
            "(id, source_type, source_id, kind, payload_json, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_uuid_hex(), "agent_tool_record", "session-3", "message", "{}", "agent-event-3"),
        )
    reupgraded_engine.dispose()
