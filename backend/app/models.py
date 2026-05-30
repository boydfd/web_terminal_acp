from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import Boolean, DateTime, Index
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Uuid, false, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.model_base import Base

LOCAL_CLIENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _enum_values(enum_class: type[Enum]) -> list[str]:
    return [member.value for member in enum_class]


class ClientStatus(Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"


class ClientRuntime(Enum):
    local = "local"
    remote = "remote"


class ClientRegistrationKeyStatus(Enum):
    active = "ACTIVE"
    used = "USED"


class WindowStatus(Enum):
    active = "ACTIVE"
    archived = "ARCHIVED"
    error = "ERROR"
    disconnected = "DISCONNECTED"


class EventSourceType(Enum):
    terminal = "terminal"
    claude_jsonl = "claude_jsonl"
    codex_trace = "codex_trace"
    summary = "summary"
    agent_tool_record = "agent_tool_record"


class SummaryJobStatus(Enum):
    pending = "PENDING"
    running = "RUNNING"
    succeeded = "SUCCEEDED"
    failed = "FAILED"


class FolderSplitJobStatus(Enum):
    pending = "PENDING"
    running = "RUNNING"
    succeeded = "SUCCEEDED"
    failed = "FAILED"


class ProjectSummaryStatus(Enum):
    pending = "PENDING"
    running = "RUNNING"
    succeeded = "SUCCEEDED"
    failed = "FAILED"


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (
        Index("ix_clients_status", "status"),
        Index("ix_clients_runtime", "runtime"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ClientStatus] = mapped_column(
        SAEnum(
            ClientStatus,
            name="clientstatus",
            values_callable=_enum_values,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default=ClientStatus.OFFLINE,
        server_default=ClientStatus.OFFLINE.value,
    )
    token_hash: Mapped[str] = mapped_column(String(71), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    install_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_update_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    runtime: Mapped[ClientRuntime] = mapped_column(
        SAEnum(
            ClientRuntime,
            name="clientruntime",
            values_callable=_enum_values,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default=ClientRuntime.remote,
        server_default=ClientRuntime.remote.value,
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    folders: Mapped[list[Folder]] = relationship("Folder", back_populates="client")
    windows: Mapped[list[VirtualWindow]] = relationship("VirtualWindow", back_populates="client")
    ai_sessions: Mapped[list[AiSession]] = relationship("AiSession", back_populates="client")
    events: Mapped[list[Event]] = relationship("Event", back_populates="client")
    folder_split_jobs: Mapped[list[FolderSplitJob]] = relationship(
        "FolderSplitJob", back_populates="client"
    )


class ClientRegistrationKey(Base):
    __tablename__ = "client_registration_keys"
    __table_args__ = (
        Index("ix_client_registration_keys_status", "status"),
        Index("ix_client_registration_keys_key_hash", "key_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key_hash: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
    status: Mapped[ClientRegistrationKeyStatus] = mapped_column(
        SAEnum(
            ClientRegistrationKeyStatus,
            name="clientregistrationkeystatus",
            values_callable=_enum_values,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default=ClientRegistrationKeyStatus.active,
        server_default=ClientRegistrationKeyStatus.active.value,
    )
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    used_client_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Folder(Base):
    __tablename__ = "folders"
    __table_args__ = (
        UniqueConstraint("client_id", "parent_id", "name", name="uq_folders_client_id_parent_id_name"),
        UniqueConstraint("client_id", "path", name="uq_folders_client_id_path"),
        Index("ix_folders_client_id", "client_id"),
        Index("ix_folders_parent_id", "parent_id"),
        Index("ix_folders_client_sort_name", "client_id", "sort_order", "name", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        default=lambda: LOCAL_CLIENT_ID,
        server_default=LOCAL_CLIENT_ID.hex,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("folders.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    client: Mapped[Client] = relationship("Client", back_populates="folders")
    parent: Mapped[Folder | None] = relationship(
        "Folder", back_populates="children", remote_side=[id]
    )
    children: Mapped[list[Folder]] = relationship("Folder", back_populates="parent")
    windows: Mapped[list[VirtualWindow]] = relationship("VirtualWindow", back_populates="folder")
    split_jobs: Mapped[list[FolderSplitJob]] = relationship(
        "FolderSplitJob", back_populates="folder"
    )


class VirtualWindow(Base):
    __tablename__ = "virtual_windows"
    __table_args__ = (
        Index("ix_virtual_windows_client_id", "client_id"),
        Index("ix_virtual_windows_folder_id", "folder_id"),
        Index("ix_virtual_windows_status", "status"),
        Index("ix_virtual_windows_client_folder_created", "client_id", "folder_id", "created_at", "title", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        default=lambda: LOCAL_CLIENT_ID,
        server_default=LOCAL_CLIENT_ID.hex,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("folders.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[WindowStatus] = mapped_column(
        SAEnum(
            WindowStatus,
            name="windowstatus",
            values_callable=_enum_values,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default=WindowStatus.active,
        server_default=WindowStatus.active.value,
    )
    tmux_session: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tmux_window_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remote_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_window_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cwd: Mapped[str | None] = mapped_column(Text, nullable=True)
    shell_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    title_manually_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    folder_manually_overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    title_tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminal_last_output_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_activity_latest_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_activity_latest_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    agent_activity_latest_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_activity_burst_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_activity_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client: Mapped[Client] = relationship("Client", back_populates="windows")
    folder: Mapped[Folder | None] = relationship("Folder", back_populates="windows")
    ai_sessions: Mapped[list[AiSession]] = relationship("AiSession", back_populates="virtual_window")
    events: Mapped[list[Event]] = relationship("Event", back_populates="virtual_window")
    summary_jobs: Mapped[list[SummaryJob]] = relationship(
        "SummaryJob", back_populates="virtual_window"
    )
    title_history: Mapped[list[WindowTitleHistory]] = relationship(
        "WindowTitleHistory", back_populates="virtual_window"
    )


class WindowTitleHistory(Base):
    __tablename__ = "window_title_history"
    __table_args__ = (
        Index("ix_window_title_history_client_window_created", "client_id", "virtual_window_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    virtual_window_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("virtual_windows.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    client: Mapped[Client] = relationship("Client")
    virtual_window: Mapped[VirtualWindow] = relationship(
        "VirtualWindow", back_populates="title_history"
    )


class AiSession(Base):
    __tablename__ = "ai_sessions"
    __table_args__ = (
        UniqueConstraint("client_id", "provider", "source_id", name="uq_ai_sessions_client_id_provider_source_id"),
        Index("ix_ai_sessions_client_id", "client_id"),
        Index("ix_ai_sessions_virtual_window_id", "virtual_window_id"),
        Index("ix_ai_sessions_client_window_updated", "client_id", "virtual_window_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        default=lambda: LOCAL_CLIENT_ID,
        server_default=LOCAL_CLIENT_ID.hex,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    virtual_window_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("virtual_windows.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    client: Mapped[Client] = relationship("Client", back_populates="ai_sessions")
    virtual_window: Mapped[VirtualWindow | None] = relationship(
        "VirtualWindow", back_populates="ai_sessions"
    )
    events: Mapped[list[Event]] = relationship("Event", back_populates="ai_session")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("client_id", "fingerprint", name="uq_events_client_id_fingerprint"),
        Index("ix_events_client_id", "client_id"),
        Index("ix_events_virtual_window_id", "virtual_window_id"),
        Index("ix_events_ai_session_id", "ai_session_id"),
        Index("ix_events_source_type_source_id", "source_type", "source_id"),
        Index("ix_events_agent_record_window", "client_id", "virtual_window_id", "created_at", "id"),
        Index("ix_events_client_window_kind_created_id", "client_id", "virtual_window_id", "kind", "created_at", "id"),
        Index("ix_events_client_window_source_created_id", "client_id", "virtual_window_id", "source_type", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        default=lambda: LOCAL_CLIENT_ID,
        server_default=LOCAL_CLIENT_ID.hex,
    )
    source_type: Mapped[EventSourceType] = mapped_column(
        SAEnum(
            EventSourceType,
            name="eventsourcetype",
            values_callable=_enum_values,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(128), nullable=False)
    virtual_window_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("virtual_windows.id", ondelete="SET NULL"), nullable=True
    )
    ai_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ai_sessions.id", ondelete="SET NULL"), nullable=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    client: Mapped[Client] = relationship("Client", back_populates="events")
    virtual_window: Mapped[VirtualWindow | None] = relationship(
        "VirtualWindow", back_populates="events"
    )
    ai_session: Mapped[AiSession | None] = relationship("AiSession", back_populates="events")


Index(
    "ix_events_source_unindexed_created",
    Event.source_type,
    Event.created_at,
    Event.id,
    postgresql_where=Event.indexed_at.is_(None),
    sqlite_where=Event.indexed_at.is_(None),
)

Index(
    "ix_events_agent_record_non_output_window",
    Event.client_id,
    Event.virtual_window_id,
    Event.created_at,
    Event.id,
    postgresql_where=Event.kind != "terminal_output",
    sqlite_where=Event.kind != "terminal_output",
)

Index(
    "ix_events_agent_activity_window_created",
    Event.client_id,
    Event.virtual_window_id,
    Event.created_at,
    Event.id,
    postgresql_where=Event.source_type.in_(
        [
            EventSourceType.agent_tool_record,
            EventSourceType.codex_trace,
            EventSourceType.claude_jsonl,
        ]
    ),
    sqlite_where=Event.source_type.in_(
        [
            EventSourceType.agent_tool_record,
            EventSourceType.codex_trace,
            EventSourceType.claude_jsonl,
        ]
    ),
)


class SummaryJob(Base):
    __tablename__ = "summary_jobs"
    __table_args__ = (
        Index("ix_summary_jobs_virtual_window_id", "virtual_window_id"),
        Index("ix_summary_jobs_status_run_after", "status", "run_after"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    virtual_window_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("virtual_windows.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[SummaryJobStatus] = mapped_column(
        SAEnum(
            SummaryJobStatus,
            name="summaryjobstatus",
            values_callable=_enum_values,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default=SummaryJobStatus.pending,
        server_default=SummaryJobStatus.pending.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trigger_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    allow_title_folder_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    input_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    virtual_window: Mapped[VirtualWindow] = relationship(
        "VirtualWindow", back_populates="summary_jobs"
    )


Index(
    "uq_summary_jobs_active_virtual_window_id",
    SummaryJob.virtual_window_id,
    unique=True,
    sqlite_where=SummaryJob.status == SummaryJobStatus.pending,
    postgresql_where=SummaryJob.status == SummaryJobStatus.pending,
)


class FolderSplitJob(Base):
    __tablename__ = "folder_split_jobs"
    __table_args__ = (
        Index("ix_folder_split_jobs_client_id", "client_id"),
        Index("ix_folder_split_jobs_folder_id", "folder_id"),
        Index("ix_folder_split_jobs_status_run_after", "status", "run_after"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    folder_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("folders.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[FolderSplitJobStatus] = mapped_column(
        SAEnum(
            FolderSplitJobStatus,
            name="foldersplitjobstatus",
            values_callable=_enum_values,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default=FolderSplitJobStatus.pending,
        server_default=FolderSplitJobStatus.pending.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    client: Mapped[Client] = relationship("Client", back_populates="folder_split_jobs")
    folder: Mapped[Folder] = relationship("Folder", back_populates="split_jobs")


class ProjectSummary(Base):
    __tablename__ = "project_summaries"
    __table_args__ = (
        UniqueConstraint("client_id", "project_path", name="uq_project_summaries_client_path"),
        Index("ix_project_summaries_client_id", "client_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_path: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[ProjectSummaryStatus] = mapped_column(
        SAEnum(
            ProjectSummaryStatus,
            name="projectsummarystatus",
            values_callable=_enum_values,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
        default=ProjectSummaryStatus.succeeded,
        server_default=ProjectSummaryStatus.succeeded.value,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UiSetting(Base):
    __tablename__ = "ui_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TerminalRecentUsage(Base):
    __tablename__ = "terminal_recent_usages"
    __table_args__ = (
        UniqueConstraint("client_id", "window_id", name="uq_terminal_recent_usages_client_window"),
        Index("ix_terminal_recent_usages_client_last_used", "client_id", "last_used_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    window_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("virtual_windows.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WindowGitBinding(Base):
    __tablename__ = "window_git_bindings"
    __table_args__ = (
        UniqueConstraint("virtual_window_id", name="uq_window_git_bindings_virtual_window_id"),
        Index("ix_window_git_bindings_client_id", "client_id"),
        Index("ix_window_git_bindings_window_client", "virtual_window_id", "client_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    virtual_window_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("virtual_windows.id", ondelete="CASCADE"), nullable=False
    )
    main_repo_root: Mapped[str] = mapped_column(Text, nullable=False)
    worktree_root: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovery_method: Mapped[str] = mapped_column(String(32), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GitWorktreeRun(Base):
    __tablename__ = "git_worktree_runs"
    __table_args__ = (
        UniqueConstraint(
            "virtual_window_id",
            "command_sequence",
            name="uq_git_worktree_runs_window_sequence",
        ),
        Index("ix_git_worktree_runs_client_window_started", "client_id", "virtual_window_id", "started_at"),
        Index("ix_git_worktree_runs_window_pending", "virtual_window_id", "pending_commit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    virtual_window_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("virtual_windows.id", ondelete="CASCADE"), nullable=False
    )
    command_sequence: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    main_repo_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    worktree_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovery_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    start_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    end_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    session_diff_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    pending_commit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index(
    "uq_folder_split_jobs_active_folder_id",
    FolderSplitJob.folder_id,
    unique=True,
    sqlite_where=FolderSplitJob.status.in_(
        [
            FolderSplitJobStatus.pending,
            FolderSplitJobStatus.running,
        ]
    ),
    postgresql_where=FolderSplitJob.status.in_(
        [
            FolderSplitJobStatus.pending,
            FolderSplitJobStatus.running,
        ]
    ),
)
