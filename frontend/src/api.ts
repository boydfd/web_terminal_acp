import type {
  AgentChatRecord,
  AgentRecord,
  BootstrapClientInput,
  BootstrapClientResult,
  Client,
  ClientUpdateResult,
  SearchResponse,
  ProjectSummary,
  TerminalRecent,
  TerminalRecentPage,
  ClientWindowsActivity,
  GitWorktreeRunList,
  TreeFolderCore,
  VirtualWindow
} from "./types";
import type { SummaryOutputLanguage } from "./userPreferences";

export type RetrySummaryPayload = {
  allow_title_folder_override: boolean;
};

const API_BASE = import.meta.env.VITE_API_BASE || window.location.origin;

function apiBaseUrl(): URL {
  const base = new URL(API_BASE);
  if (!base.pathname.endsWith("/")) {
    base.pathname = `${base.pathname}/`;
  }
  return base;
}

function apiUrl(path: string): string {
  return new URL(path.replace(/^\/+/, ""), apiBaseUrl()).toString();
}

function pathSegment(segment: string): string {
  return encodeURIComponent(segment);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    }
  });
  if (!response.ok) {
    let detail: string | null = null;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      detail = null;
    }
    throw new Error(detail ? `${response.status} ${detail}` : `${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export function terminalWebSocketUrl(clientId: string, windowId: string): string {
  const url = new URL(apiUrl(`/api/clients/${pathSegment(clientId)}/terminal/${pathSegment(windowId)}`));
  if (url.protocol === "http:") {
    url.protocol = "ws:";
  } else if (url.protocol === "https:") {
    url.protocol = "wss:";
  }
  return url.toString();
}

export function terminalSelectionWebSocketUrl(clientId: string): string {
  const url = new URL(apiUrl(`/api/clients/${pathSegment(clientId)}/terminal-selection`));
  if (url.protocol === "http:") {
    url.protocol = "ws:";
  } else if (url.protocol === "https:") {
    url.protocol = "wss:";
  }
  return url.toString();
}

export function fetchClients(): Promise<Client[]> {
  return request<Client[]>("/api/clients");
}

export function bootstrapClient(payload: BootstrapClientInput): Promise<BootstrapClientResult> {
  return request<BootstrapClientResult>("/api/clients/bootstrap", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function updateClient(clientId: string): Promise<ClientUpdateResult> {
  return request<ClientUpdateResult>(`/api/clients/${pathSegment(clientId)}/update`, {
    method: "POST"
  });
}

export function fetchTree(clientId: string): Promise<TreeFolderCore[]> {
  return request<TreeFolderCore[]>(`/api/clients/${pathSegment(clientId)}/tree`);
}

export function fetchWindowActivity(
  clientId: string,
  options?: { includeRuntimeTags?: boolean }
): Promise<ClientWindowsActivity> {
  const params = new URLSearchParams();
  if (options?.includeRuntimeTags) {
    params.set("include_runtime_tags", "true");
  }
  const query = params.toString();
  const suffix = query ? `?${query}` : "";
  return request<ClientWindowsActivity>(
    `/api/clients/${pathSegment(clientId)}/windows/activity${suffix}`
  );
}

export function fetchGitRuns(clientId: string, windowId: string, limit = 50, offset = 0): Promise<GitWorktreeRunList> {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset)
  });
  return request<GitWorktreeRunList>(
    `/api/clients/${pathSegment(clientId)}/windows/${pathSegment(windowId)}/git-runs?${params.toString()}`
  );
}

export function createWindow(clientId: string): Promise<VirtualWindow> {
  return request<VirtualWindow>(`/api/clients/${pathSegment(clientId)}/windows`, {
    method: "POST",
    body: JSON.stringify({ cwd: null, shell_command: null })
  });
}

export async function deleteWindow(clientId: string, windowId: string): Promise<void> {
  const response = await fetch(apiUrl(`/api/clients/${pathSegment(clientId)}/windows/${pathSegment(windowId)}`), {
    method: "DELETE",
    headers: {
      "Content-Type": "application/json"
    }
  });
  if (!response.ok) {
    let detail: string | null = null;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      detail = null;
    }
    throw new Error(detail ? `${response.status} ${detail}` : `${response.status} ${response.statusText}`);
  }
}

export function fetchWindow(clientId: string, windowId: string): Promise<VirtualWindow> {
  return request<VirtualWindow>(`/api/clients/${pathSegment(clientId)}/windows/${pathSegment(windowId)}`);
}

export function fetchAgentRecordChat(clientId: string, windowId: string, limit = 30, offset = 0): Promise<AgentChatRecord> {
  const params = new URLSearchParams({
    messages_limit: String(limit),
    messages_offset: String(offset)
  });
  return request<AgentChatRecord>(
    `/api/clients/${pathSegment(clientId)}/windows/${pathSegment(windowId)}/agent-record/chat?${params.toString()}`
  );
}

export function fetchAgentRecordDetail(clientId: string, windowId: string, limit = 100, offset = 0): Promise<AgentRecord> {
  const params = new URLSearchParams({
    events_limit: String(limit),
    events_offset: String(offset)
  });
  return request<AgentRecord>(
    `/api/clients/${pathSegment(clientId)}/windows/${pathSegment(windowId)}/agent-record/detail?${params.toString()}`
  );
}

export function retrySummary(
  clientId: string,
  windowId: string,
  payload?: RetrySummaryPayload
): Promise<VirtualWindow> {
  return request<VirtualWindow>(
    `/api/clients/${pathSegment(clientId)}/windows/${pathSegment(windowId)}/summary_jobs`,
    {
      method: "POST",
      ...(payload ? { body: JSON.stringify(payload) } : {})
    }
  );
}

export function search(clientId: string, query: string): Promise<SearchResponse> {
  return request<SearchResponse>(`/api/clients/${pathSegment(clientId)}/search?q=${encodeURIComponent(query)}`);
}

export function fetchTerminalRecents(
  clientId: string,
  page = 1,
  pageSize = 20,
  query = ""
): Promise<TerminalRecentPage> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize)
  });
  const trimmedQuery = query.trim();
  if (trimmedQuery.length > 0) {
    params.set("q", trimmedQuery);
  }
  return request<TerminalRecentPage>(
    `/api/clients/${pathSegment(clientId)}/terminal-recents?${params.toString()}`
  );
}

export function recordTerminalRecent(
  clientId: string,
  payload: Pick<TerminalRecent, "window_id" | "title">
): Promise<TerminalRecent> {
  return request<TerminalRecent>(`/api/clients/${pathSegment(clientId)}/terminal-recents`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function fetchProjectSummaries(clientId: string): Promise<ProjectSummary[]> {
  return request<ProjectSummary[]>(`/api/clients/${pathSegment(clientId)}/project-summaries`);
}

export function summarizeProject(
  clientId: string,
  projectPath: string,
  outputLanguage: SummaryOutputLanguage
): Promise<ProjectSummary> {
  return request<ProjectSummary>(`/api/clients/${pathSegment(clientId)}/project-summaries/summarize`, {
    method: "POST",
    body: JSON.stringify({
      project_path: projectPath,
      output_language: outputLanguage
    })
  });
}
