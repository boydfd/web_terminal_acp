export type ClientRuntime = "local" | "remote";

export type ClientStatus = "ONLINE" | "OFFLINE" | "ERROR";

export type Client = {
  id: string;
  name: string;
  status: ClientStatus;
  hostname: string | null;
  install_path: string | null;
  version: string | null;
  last_update_at: string | null;
  runtime: ClientRuntime;
  last_seen_at: string | null;
  connected_at: string | null;
  created_at: string;
  updated_at: string;
};

export type BootstrapClientInput = {
  name: string;
  host: string;
  port: number;
  username: string;
  private_key: string;
  passphrase: string | null;
  server_url: string;
};

export type BootstrapClientResult = {
  client_id: string;
  name: string;
  status: ClientStatus;
  reused: boolean;
};

export type ClientUpdateResult = {
  client_id: string;
  job_id: string;
  status: "STARTED";
  method: string;
};

export type WorkStatus = {
  state: "LONG_IDLE" | "RECENT_ACTIVE" | "WORKING";
  label: string;
  color: "gray" | "green" | "orange";
  last_activity_at?: string | null;
  last_working_activity_at?: string | null;
};

export type TreeWindowCore = {
  id: string;
  title: string;
  status: string;
  title_tags?: string[] | null;
  created_at: string;
};

export type GitWorktreeActivity = {
  worktree_root: string;
  main_repo_root: string;
  branch?: string | null;
  pending_commit: boolean;
};

export type WindowActivity = {
  work_status: WorkStatus;
  runtime_tags: string[];
  last_agent_task_completed_at?: string | null;
  git_worktree?: GitWorktreeActivity | null;
};

export type GitSessionDiff = {
  has_changes?: boolean;
  head_moved?: boolean;
  start_head?: string | null;
  end_head?: string | null;
  uncommitted_at_end?: boolean;
  start_status_porcelain?: string;
  end_status_porcelain?: string;
  end_diff_stat?: string;
  end_staged_diff_stat?: string;
};

export type GitWorktreeRun = {
  id: string;
  virtual_window_id: string;
  command_sequence: string;
  agent_provider: string | null;
  status: string;
  worktree_root: string | null;
  main_repo_root: string | null;
  discovery_method: string | null;
  session_diff_json: GitSessionDiff | null;
  pending_commit: boolean;
  resolved_at: string | null;
  started_at: string;
  ended_at: string | null;
};

export type GitWorktreeRunList = {
  supported: boolean;
  runs: GitWorktreeRun[];
  total: number;
  limit: number;
  offset: number;
};

export type TreeWindow = TreeWindowCore & WindowActivity;

export type ClientWindowsActivity = {
  windows: Array<WindowActivity & { window_id: string }>;
};

export type TreeFolderCore = {
  id: string;
  name: string;
  path: string;
  folders: TreeFolderCore[];
  windows: TreeWindowCore[];
};

export type TreeFolder = {
  id: string;
  name: string;
  path: string;
  folders: TreeFolder[];
  windows: TreeWindow[];
};

export type SummaryJobStatus = "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";

export type SummaryJob = {
  id: string;
  status: SummaryJobStatus | string;
  attempts: number;
  last_error: string | null;
  trigger_reason: string | null;
  run_after: string | null;
  created_at?: string;
  updated_at?: string;
};

export type VirtualWindow = {
  id: string;
  client_id: string;
  title: string;
  folder_id: string | null;
  status: string;
  tmux_session: string | null;
  tmux_window_id: string | null;
  remote_session_id: string | null;
  remote_window_id: string | null;
  cwd: string | null;
  shell_command: string | null;
  summary: string | null;
  title_tags: string[] | null;
  runtime_tags: string[];
  work_status: WorkStatus;
  title_manually_overridden: boolean;
  folder_manually_overridden: boolean;
  command_capture_supported: boolean;
  summary_job: SummaryJob | null;
  created_at: string;
};

export type AgentSession = {
  id: string;
  provider: "claude" | "codex" | string;
  source_id: string;
  source_path: string | null;
  project_path: string | null;
  virtual_window_id: string | null;
  title: string | null;
  tags: string[] | null;
  summary: string | null;
  created_at: string;
  updated_at: string;
};

export type AgentEventProjection = {
  tone: string;
  label: string;
  body: string;
  body_format: "markdown" | "json";
  subtype: string | null;
};

export type AgentRecordEvent = {
  id: string;
  ai_session_id: string | null;
  source_type: string;
  source_id: string;
  kind: string;
  payload_json: Record<string, unknown>;
  projection: AgentEventProjection | null;
  created_at: string;
};

export type AgentRecord = {
  window_id: string;
  sessions: AgentSession[];
  events: AgentRecordEvent[];
  events_total: number;
  events_limit: number;
  events_offset: number;
  events_has_more: boolean;
};

export type AgentChatMessage = {
  id: string;
  ai_session_id: string | null;
  source_type: string;
  source_id: string;
  role: "user" | "agent";
  body: string;
  body_format: "markdown" | "json";
  created_at: string;
};

export type AgentChatRecord = {
  window_id: string;
  messages: AgentChatMessage[];
  messages_total: number;
  messages_limit: number;
  messages_offset: number;
  messages_has_more: boolean;
};

export type SearchResultSource = {
  virtual_window_id?: string | null;
  title?: string;
  tags?: string[];
  folder_path?: string;
  provider?: string;
  kind?: string;
};

export type SearchResult = {
  id: string;
  index: string;
  score: number | null;
  snippet: string;
  source: SearchResultSource;
};

export type SearchResponse = {
  query: string;
  results: SearchResult[];
};

export type TerminalRecent = {
  window_id: string;
  title: string;
  last_used_at: string;
};

export type TerminalRecentPage = {
  items: TerminalRecent[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
};

export type ProjectSummary = {
  project_path: string;
  display_name: string | null;
  status: string;
  last_error: string | null;
  updated_at: string;
};
