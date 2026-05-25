from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

WindowTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
WindowText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
WindowTitleTag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
WindowStatusIn = Literal["ACTIVE", "ARCHIVED", "ERROR", "DISCONNECTED"]
ClientName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
ClientStatusOut = Literal["ONLINE", "OFFLINE", "ERROR"]
ClientRuntimeOut = Literal["local", "remote"]
BootstrapHost = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
BootstrapUsername = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
BootstrapPrivateKey = Annotated[str, StringConstraints(min_length=1)]
BootstrapServerUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]


class BootstrapClientIn(BaseModel):
    name: ClientName
    host: BootstrapHost
    port: int = Field(ge=1, le=65535)
    username: BootstrapUsername
    private_key: BootstrapPrivateKey
    passphrase: str | None = None
    server_url: BootstrapServerUrl


class BootstrapClientOut(BaseModel):
    client_id: UUID
    name: str
    status: ClientStatusOut
    reused: bool


class ClientUpdateOut(BaseModel):
    client_id: UUID
    job_id: str
    status: Literal["STARTED"]
    method: str


class WorkStatusOut(BaseModel):
    state: Literal["LONG_IDLE", "RECENT_ACTIVE", "WORKING"]
    label: str
    color: Literal["gray", "green", "orange"]
    last_activity_at: datetime | None
    last_working_activity_at: datetime | None


class TreeWindowOut(BaseModel):
    id: UUID
    title: str
    status: str
    created_at: datetime
    title_tags: list[str] | None = None


class GitWorktreeActivityOut(BaseModel):
    worktree_root: str
    main_repo_root: str
    branch: str | None = None
    pending_commit: bool = False


class WindowActivityOut(BaseModel):
    window_id: UUID
    work_status: WorkStatusOut
    runtime_tags: list[str] = Field(default_factory=list)
    last_agent_task_completed_at: datetime | None = None
    git_worktree: GitWorktreeActivityOut | None = None


class GitWorktreeRunOut(BaseModel):
    id: UUID
    virtual_window_id: UUID
    command_sequence: str
    agent_provider: str | None
    status: str
    worktree_root: str | None
    main_repo_root: str | None
    discovery_method: str | None
    session_diff_json: dict[str, Any] | None
    pending_commit: bool
    resolved_at: datetime | None
    started_at: datetime
    ended_at: datetime | None


class GitWorktreeRunListOut(BaseModel):
    supported: bool
    runs: list[GitWorktreeRunOut] = Field(default_factory=list)
    total: int = 0
    limit: int
    offset: int


class ClientWindowsActivityOut(BaseModel):
    windows: list[WindowActivityOut] = Field(default_factory=list)


class TreeFolderOut(BaseModel):
    id: UUID
    name: str
    path: str
    folders: list["TreeFolderOut"] = Field(default_factory=list)
    windows: list[TreeWindowOut] = Field(default_factory=list)


class ClientOut(BaseModel):
    id: UUID
    name: str
    status: ClientStatusOut
    hostname: str | None
    install_path: str | None
    version: str | None
    last_update_at: datetime | None
    runtime: ClientRuntimeOut
    last_seen_at: datetime | None
    connected_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ClientPatchIn(BaseModel):
    name: ClientName | None = None


class ClientUpdateCompleteIn(BaseModel):
    job_id: str | None = None


class FolderCreateIn(BaseModel):
    path: str


class FolderOut(BaseModel):
    id: UUID
    name: str
    path: str


class WindowCreateIn(BaseModel):
    cwd: WindowText | None = None
    shell_command: WindowText | None = None


class WindowPatchIn(BaseModel):
    folder_id: UUID | None = None
    title: WindowTitle | None = None
    status: WindowStatusIn | None = None
    summary: str | None = None
    title_tags: list[WindowTitleTag] | None = Field(default=None, max_length=20)


class SummaryJobOut(BaseModel):
    id: UUID
    status: str
    trigger_reason: str | None
    attempts: int
    last_error: str | None
    run_after: datetime | None
    updated_at: datetime
    allow_title_folder_override: bool


class WindowOut(BaseModel):
    id: UUID
    client_id: UUID
    title: str
    folder_id: UUID | None
    status: str
    tmux_session: str | None
    tmux_window_id: str | None
    remote_session_id: str | None
    remote_window_id: str | None
    cwd: str | None
    shell_command: str | None
    title_manually_overridden: bool
    folder_manually_overridden: bool
    command_capture_supported: bool
    summary: str | None
    title_tags: list[str] | None
    runtime_tags: list[str] = Field(default_factory=list)
    work_status: WorkStatusOut
    summary_job: SummaryJobOut | None
    created_at: datetime


class AgentSessionOut(BaseModel):
    id: UUID
    provider: str
    source_id: str
    source_path: str | None
    project_path: str | None
    virtual_window_id: UUID | None
    title: str | None
    tags: list[str] | None
    summary: str | None
    created_at: datetime
    updated_at: datetime


class AgentEventProjectionOut(BaseModel):
    tone: str
    label: str
    body: str
    body_format: Literal["markdown", "json"] = "markdown"
    subtype: str | None = None


class AgentEventOut(BaseModel):
    id: UUID
    ai_session_id: UUID | None
    source_type: str
    source_id: str
    kind: str
    payload_json: dict[str, Any]
    projection: AgentEventProjectionOut | None = None
    created_at: datetime


class AgentRecordOut(BaseModel):
    window_id: UUID
    sessions: list[AgentSessionOut]
    events: list[AgentEventOut]
    events_total: int
    events_limit: int
    events_offset: int
    events_has_more: bool


class AgentChatMessageOut(BaseModel):
    id: UUID
    ai_session_id: UUID | None
    source_type: str
    source_id: str
    role: Literal["user", "agent"]
    body: str
    body_format: Literal["markdown", "json"] = "markdown"
    created_at: datetime


class AgentChatRecordOut(BaseModel):
    window_id: UUID
    messages: list[AgentChatMessageOut]
    messages_total: int
    messages_limit: int
    messages_offset: int
    messages_has_more: bool


class SummaryJobRetryIn(BaseModel):
    allow_title_folder_override: bool = False


class IngestEventOut(BaseModel):
    id: UUID
    source_type: str
    source_id: str
    kind: str
    fingerprint: str


class SearchResultOut(BaseModel):
    id: str
    index: str
    score: float | None
    snippet: str
    source: dict[str, Any]


class SearchOut(BaseModel):
    query: str
    results: list[SearchResultOut]


class TerminalRecentOut(BaseModel):
    window_id: UUID
    title: str
    last_used_at: datetime


class TerminalRecentPageOut(BaseModel):
    items: list[TerminalRecentOut]
    page: int
    page_size: int
    total: int
    total_pages: int


class TerminalRecentTouchIn(BaseModel):
    window_id: UUID
    title: WindowTitle


class ProjectSummaryOut(BaseModel):
    project_path: str
    display_name: str | None
    status: str
    last_error: str | None
    updated_at: datetime


class ProjectSummarySummarizeIn(BaseModel):
    project_path: str
    output_language: str | None = None
