from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

WindowTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
WindowText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
WindowTitleTag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
WindowStatusIn = Literal["ACTIVE", "ARCHIVED", "ERROR", "DISCONNECTED"]
AgentKindIn = Literal["codex", "claude", "cursor"]
AgentConfigSectionKindIn = Literal["skills", "plugins", "hooks"]
ClientName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
ClientStatusOut = Literal["ONLINE", "OFFLINE", "ERROR"]
ClientRuntimeOut = Literal["local", "remote"]
BootstrapHost = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
BootstrapUsername = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
BootstrapPrivateKey = Annotated[str, StringConstraints(min_length=1)]
BootstrapServerUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]
LoginSecret = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
RegistrationKey = Annotated[str, StringConstraints(strip_whitespace=True, min_length=16, max_length=512)]


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


class LoginIn(BaseModel):
    secret: LoginSecret


class LoginOut(BaseModel):
    token: str
    enabled: bool = True


class ClientRegistrationKeyCreateIn(BaseModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] | None = None


class ClientRegistrationKeyOut(BaseModel):
    id: UUID
    key: str
    label: str | None = None
    created_at: datetime | None = None


class DirectClientRegisterIn(BaseModel):
    registration_key: RegistrationKey
    name: ClientName
    hostname: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] | None = None
    install_path: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)] | None = None
    server_url: BootstrapServerUrl


class DirectClientRegisterOut(BaseModel):
    client_id: UUID
    token: str
    name: str
    config: dict[str, Any]
    package: dict[str, Any]


class ClientUpdateOut(BaseModel):
    client_id: UUID
    job_id: str
    status: Literal["STARTED"]
    method: str


class WorkStatusOut(BaseModel):
    state: Literal["LONG_IDLE", "RECENT_ACTIVE", "WORKING", "FINISHED", "ABORTED"]
    label: str
    color: Literal["gray", "green", "orange", "red"]
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
    last_agent_task_status: Literal["FINISHED", "ABORTED"] | None = None
    last_agent_task_status_at: datetime | None = None
    git_worktree: GitWorktreeActivityOut | None = None


class GitWorktreeRunOut(BaseModel):
    id: UUID
    virtual_window_id: UUID
    command_sequence: str
    agent_provider: str | None
    status: str
    run_type: Literal["agent", "tracking"]
    worktree_root: str | None
    main_repo_root: str | None
    discovery_method: str | None
    start_snapshot_json: dict[str, Any] | None
    end_snapshot_json: dict[str, Any] | None
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


class TerminalNotificationOut(BaseModel):
    id: str
    client_id: UUID
    window_id: UUID
    window_title: str
    completed_at: datetime
    status: Literal["FINISHED", "ABORTED"]
    read: bool


class TerminalNotificationListOut(BaseModel):
    notifications: list[TerminalNotificationOut] = Field(default_factory=list)


class TerminalNotificationAckIn(BaseModel):
    window_id: UUID
    completed_at: datetime


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


class AgentConfigSelectionItemIn(BaseModel):
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
    enabled: bool


class AgentConfigSelectionSectionIn(BaseModel):
    id: AgentConfigSectionKindIn
    items: list[AgentConfigSelectionItemIn] = Field(default_factory=list, max_length=500)


class AgentConfigSelectionIn(BaseModel):
    agent: AgentKindIn
    sections: list[AgentConfigSelectionSectionIn] = Field(default_factory=list, max_length=3)


class AgentLaunchIn(BaseModel):
    agent: AgentKindIn
    command: WindowText | None = None
    config: AgentConfigSelectionIn | None = None
    template_id: str | None = None


class WindowCreateIn(BaseModel):
    cwd: WindowText | None = None
    shell_command: WindowText | None = None
    folder_path: WindowText | None = None
    agent_launch: AgentLaunchIn | None = None


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
    last_terminal_command_at: datetime | None
    last_agent_event_at: datetime | None
    last_active_at: datetime


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


class AgentConfigItemOut(BaseModel):
    id: str
    name: str
    enabled: bool
    path: str | None = None


class AgentConfigSectionOut(BaseModel):
    id: Literal["skills", "plugins", "hooks"]
    name: str
    items: list[AgentConfigItemOut] = Field(default_factory=list)


class AgentConfigOut(BaseModel):
    agent: AgentKindIn
    sections: list[AgentConfigSectionOut] = Field(default_factory=list)


class AgentConfigToggleIn(BaseModel):
    enabled: bool


class CommandHistoryItemOut(BaseModel):
    id: UUID
    command: str
    shell: str | None = None
    cwd: str | None = None
    sequence: int | str | None = None
    exit_status: int | str | None = None
    captured_at: datetime
    finished_at: datetime | None = None
    created_at: datetime


class CommandHistoryOut(BaseModel):
    window_id: UUID
    commands: list[CommandHistoryItemOut]
    commands_total: int
    commands_limit: int
    commands_offset: int
    commands_has_more: bool


class WindowTitleHistoryItemOut(BaseModel):
    id: UUID
    title: str
    summary: str | None
    source: str
    created_at: datetime


class WindowTitleHistoryOut(BaseModel):
    window_id: UUID
    items: list[WindowTitleHistoryItemOut]
    total: int
    limit: int
    offset: int
    has_more: bool


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


class CustomQuickKeyOut(BaseModel):
    id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    input: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    shortcut: dict[str, Any] | None = None


class CustomQuickKeysOut(BaseModel):
    quick_keys: list[CustomQuickKeyOut] = Field(default_factory=list, max_length=100)


class CustomQuickKeysPutIn(BaseModel):
    quick_keys: list[CustomQuickKeyOut] = Field(default_factory=list, max_length=100)


class ProjectSummaryOut(BaseModel):
    project_path: str
    display_name: str | None
    status: str
    last_error: str | None
    updated_at: datetime


class ProjectSummarySummarizeIn(BaseModel):
    project_path: str
    output_language: str | None = None
