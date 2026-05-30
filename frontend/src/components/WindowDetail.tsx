import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { fetchCommandHistory, fetchWindow, fetchWindowTitleHistory, retrySummary, updateWindowTitle } from "../api";
import { useAgentConfigData } from "../hooks/useAgentConfigData";
import { useAgentRecordData } from "../hooks/useAgentRecordData";
import type { GitWorktreeActivity, SummaryJob, TreeFolderCore, VirtualWindow } from "../types";
import { AgentConfigViewer } from "./AgentConfigViewer";
import { AgentRecordModal, AgentRecordViewer } from "./AgentRecordViewer";
import { CommandHistoryViewer } from "./CommandHistoryViewer";
import { DetailPanelTabs, type DetailPanelTab } from "./DetailPanelTabs";
import { GitRunViewer } from "./GitRunViewer";
import { TitleHistoryViewer } from "./TitleHistoryViewer";
import { WorkStatusBadge } from "./WorkStatusBadge";

type WindowDetailProps = {
  clientId: string | null;
  windowId: string | null;
  gitWorktree?: GitWorktreeActivity | null;
  terminalStatusLabel?: string;
  terminalStatusTone?: "connected" | "connecting" | "reconnecting" | "unavailable" | "error";
  quickInputDraft?: string;
  canSendQuickInput?: boolean;
  agentRecordShortcutLabel?: string;
  onQuickInputDraftChange?: (draft: string) => void;
  onQuickInputSubmit?: (draft: string) => boolean;
};

type SummaryStatus = {
  label: string;
  tone?: "muted" | "error";
};
type AgentDetailTab = "record" | "config";
type HistoryDetailTab = "commands" | "title";

const COMMAND_HISTORY_PAGE_SIZE = 100;
const TITLE_HISTORY_PAGE_SIZE = 100;
const MAX_TITLE_LENGTH = 255;

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function summaryStatus(summaryJob: SummaryJob | null, commandCaptureSupported: boolean): SummaryStatus {
  if (!commandCaptureSupported) {
    return { label: "unsupported shell", tone: "muted" };
  }

  if (summaryJob === null) {
    return { label: "No summary job yet.", tone: "muted" };
  }

  switch (summaryJob.status.toUpperCase()) {
    case "PENDING":
      return summaryJob.run_after
        ? { label: `waiting until ${formatDateTime(summaryJob.run_after)}` }
        : { label: "waiting" };
    case "RUNNING":
      return { label: "running" };
    case "SUCCEEDED":
      return { label: "succeeded" };
    case "FAILED":
      return { label: "failed", tone: "error" };
    default:
      return { label: summaryJob.status.toLowerCase() };
  }
}

function displayTags(item: VirtualWindow): string[] {
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const tag of item.title_tags ?? []) {
    const normalized = tag.trim();
    const key = normalized.toLocaleLowerCase();
    if (!normalized || seen.has(key)) {
      continue;
    }
    seen.add(key);
    tags.push(normalized);
  }
  return tags;
}

function renameTreeWindow(
  folders: TreeFolderCore[] | undefined,
  windowId: string,
  title: string
): TreeFolderCore[] | undefined {
  if (folders === undefined) {
    return undefined;
  }

  let changed = false;
  const nextFolders = folders.map((folder) => {
    let folderChanged = false;
    const windows = folder.windows.map((window) => {
      if (window.id !== windowId) {
        return window;
      }

      folderChanged = true;
      return { ...window, title };
    });
    const childFolders = renameTreeWindow(folder.folders, windowId, title);
    if (childFolders !== folder.folders) {
      folderChanged = true;
    }
    if (!folderChanged) {
      return folder;
    }

    changed = true;
    return { ...folder, folders: childFolders ?? folder.folders, windows };
  });

  return changed ? nextFolders : folders;
}

export function WindowDetail({
  clientId,
  windowId,
  gitWorktree = null,
  terminalStatusLabel,
  terminalStatusTone,
  quickInputDraft,
  canSendQuickInput,
  agentRecordShortcutLabel = "Expand",
  onQuickInputDraftChange,
  onQuickInputSubmit
}: WindowDetailProps) {
  const [allowTitleFolderOverride, setAllowTitleFolderOverride] = useState(false);
  const [detailTab, setDetailTab] = useState<DetailPanelTab>("overview");
  const [agentDetailTab, setAgentDetailTab] = useState<AgentDetailTab>("record");
  const [historyDetailTab, setHistoryDetailTab] = useState<HistoryDetailTab>("commands");
  const [commandHistoryPage, setCommandHistoryPage] = useState(0);
  const [titleHistoryPage, setTitleHistoryPage] = useState(0);
  const [isEditingTitle, setIsEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const queryClient = useQueryClient();
  const showGitTab = gitWorktree !== null;
  const agentRecord = useAgentRecordData({
    clientId,
    windowId,
    enabled: detailTab === "agent" && agentDetailTab === "record"
  });
  const agentConfig = useAgentConfigData({
    clientId,
    windowId,
    enabled: detailTab === "agent" && agentDetailTab === "config"
  });

  useEffect(() => {
    setDetailTab("overview");
    setAgentDetailTab("record");
    setHistoryDetailTab("commands");
    setCommandHistoryPage(0);
    setTitleHistoryPage(0);
    setIsEditingTitle(false);
    setTitleDraft("");
  }, [clientId, windowId]);

  useEffect(() => {
    if (!showGitTab && detailTab === "git") {
      setDetailTab("overview");
    }
  }, [showGitTab, detailTab]);

  const windowQuery = useQuery({
    queryKey: ["window", clientId, windowId],
    queryFn: () => fetchWindow(clientId as string, windowId as string),
    enabled: clientId !== null && windowId !== null,
    refetchInterval: 10000
  });
  const commandHistoryQuery = useQuery({
    queryKey: ["command-history", clientId, windowId, commandHistoryPage, COMMAND_HISTORY_PAGE_SIZE],
    queryFn: () => fetchCommandHistory(
      clientId as string,
      windowId as string,
      COMMAND_HISTORY_PAGE_SIZE,
      commandHistoryPage * COMMAND_HISTORY_PAGE_SIZE
    ),
    enabled: clientId !== null && windowId !== null && detailTab === "history" && historyDetailTab === "commands",
    placeholderData: keepPreviousData,
    refetchInterval: 10000
  });
  const titleHistoryQuery = useQuery({
    queryKey: ["title-history", clientId, windowId, titleHistoryPage, TITLE_HISTORY_PAGE_SIZE],
    queryFn: () => fetchWindowTitleHistory(
      clientId as string,
      windowId as string,
      TITLE_HISTORY_PAGE_SIZE,
      titleHistoryPage * TITLE_HISTORY_PAGE_SIZE
    ),
    enabled: clientId !== null && windowId !== null && detailTab === "history" && historyDetailTab === "title",
    placeholderData: keepPreviousData,
    refetchInterval: 10000
  });
  const retryMutation = useMutation({
    mutationFn: ({
      clientId,
      windowId,
      allowTitleFolderOverride
    }: {
      clientId: string;
      windowId: string;
      allowTitleFolderOverride: boolean;
    }) => retrySummary(clientId, windowId, { allow_title_folder_override: allowTitleFolderOverride }),
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["window", variables.clientId, variables.windowId] });
      queryClient.invalidateQueries({ queryKey: ["tree", variables.clientId] });
      queryClient.invalidateQueries({ queryKey: ["window-activity", variables.clientId] });
    }
  });
  const renameMutation = useMutation({
    mutationFn: ({
      clientId,
      windowId,
      title
    }: {
      clientId: string;
      windowId: string;
      title: string;
    }) => updateWindowTitle(clientId, windowId, title),
    onSuccess: (updated, variables) => {
      queryClient.setQueryData(["window", variables.clientId, variables.windowId], updated);
      queryClient.setQueryData<TreeFolderCore[]>(
        ["tree", variables.clientId],
        (current) => renameTreeWindow(current, variables.windowId, updated.title)
      );
      queryClient.invalidateQueries({ queryKey: ["window", variables.clientId, variables.windowId] });
      queryClient.invalidateQueries({ queryKey: ["tree", variables.clientId] });
      queryClient.invalidateQueries({ queryKey: ["window-activity", variables.clientId] });
      queryClient.invalidateQueries({ queryKey: ["title-history", variables.clientId, variables.windowId] });
      queryClient.invalidateQueries({ queryKey: ["terminal-recents", variables.clientId] });
      setTitleDraft(updated.title);
      setIsEditingTitle(false);
    }
  });

  if (clientId === null || windowId === null) {
    return <p className="muted">Select a terminal artifact.</p>;
  }
  if (windowQuery.isLoading) {
    return <p className="muted">Loading details...</p>;
  }
  if (windowQuery.isError || !windowQuery.data) {
    return <p className="error" role="alert">Failed to load details.</p>;
  }

  const item = windowQuery.data;
  const status = summaryStatus(item.summary_job, item.command_capture_supported !== false);
  const tags = displayTags(item);
  const trimmedTitleDraft = titleDraft.trim();
  const titleSaveDisabled =
    renameMutation.isPending
    || trimmedTitleDraft.length === 0
    || trimmedTitleDraft.length > MAX_TITLE_LENGTH
    || trimmedTitleDraft === item.title;
  const manualLocks = [
    item.title_manually_overridden ? "title locked" : null,
    item.folder_manually_overridden ? "folder locked" : null
  ].filter((lock): lock is string => lock !== null);
  return (
    <div>
      <div className="window-title-header">
        {isEditingTitle ? (
          <form
            className="window-title-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (titleSaveDisabled) {
                return;
              }
              renameMutation.mutate({ clientId, windowId: item.id, title: trimmedTitleDraft });
            }}
          >
            <input
              aria-label="Terminal title"
              maxLength={MAX_TITLE_LENGTH}
              value={titleDraft}
              autoFocus
              disabled={renameMutation.isPending}
              onChange={(event) => setTitleDraft(event.target.value)}
            />
            <div className="window-title-actions">
              <button type="submit" disabled={titleSaveDisabled}>
                Save
              </button>
              <button
                type="button"
                disabled={renameMutation.isPending}
                onClick={() => {
                  setTitleDraft(item.title);
                  setIsEditingTitle(false);
                  renameMutation.reset();
                }}
              >
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <div className="window-title-display">
            <h2 title={item.title}>{item.title}</h2>
            <button
              type="button"
              className="window-title-edit-button"
              onClick={() => {
                setTitleDraft(item.title);
                setIsEditingTitle(true);
                renameMutation.reset();
              }}
            >
              Rename
            </button>
          </div>
        )}
      </div>
      {renameMutation.isError && (
        <p className="error" role="alert">
          {renameMutation.error instanceof Error ? renameMutation.error.message : "Failed to rename terminal."}
        </p>
      )}
      <DetailPanelTabs activeTab={detailTab} showGitTab={showGitTab} onTabChange={setDetailTab} />

      {detailTab === "overview" && (
        <>
          <dl className="detail-list">
            <dt>Status</dt>
            <dd>{item.status}</dd>
            <dt>Created</dt>
            <dd>{formatDateTime(item.created_at)}</dd>
            <dt>Last shell command</dt>
            <dd>{item.last_terminal_command_at ? formatDateTime(item.last_terminal_command_at) : "-"}</dd>
            <dt>Last agent event</dt>
            <dd>{item.last_agent_event_at ? formatDateTime(item.last_agent_event_at) : "-"}</dd>
            <dt>Last active</dt>
            <dd>{formatDateTime(item.last_active_at)}</dd>
            <dt>Work status</dt>
            <dd>
              <span className="detail-work-status">
                <WorkStatusBadge status={item.work_status} />
                <span className="muted">
                  {item.work_status.last_activity_at
                    ? `Last activity ${formatDateTime(item.work_status.last_activity_at)}`
                    : "No activity yet"}
                </span>
              </span>
            </dd>
            <dt>CWD</dt>
            <dd>{item.cwd ?? "-"}</dd>
            {gitWorktree && (
              <>
                <dt>Git worktree</dt>
                <dd>{gitWorktree.worktree_root}</dd>
                <dt>Branch</dt>
                <dd>{gitWorktree.branch ?? "-"}</dd>
              </>
            )}
            <dt>tmux</dt>
            <dd>{item.tmux_session ?? "-"}:{item.tmux_window_id ?? "-"}</dd>
            <dt>Summary</dt>
            <dd>{item.summary ?? "No summary yet."}</dd>
            <dt>Tags</dt>
            <dd>
              {tags.length > 0 ? (
                <span className="detail-tags">
                  {tags.map((tag) => (
                    <span key={tag} title={tag}>{tag}</span>
                  ))}
                </span>
              ) : (
                "-"
              )}
            </dd>
            <dt>Summary job</dt>
            <dd className={status.tone}>{status.label}</dd>
            {item.summary_job?.last_error && (
              <>
                <dt>Last error</dt>
                <dd className="error">{item.summary_job.last_error}</dd>
              </>
            )}
            {item.summary_job && (
              <>
                <dt>Attempts</dt>
                <dd>{item.summary_job.attempts}</dd>
              </>
            )}
            {item.summary_job?.trigger_reason && (
              <>
                <dt>Trigger</dt>
                <dd>{item.summary_job.trigger_reason}</dd>
              </>
            )}
            {item.summary_job?.run_after && (
              <>
                <dt>Run after</dt>
                <dd>{formatDateTime(item.summary_job.run_after)}</dd>
              </>
            )}
            <dt>Manual locks</dt>
            <dd>{manualLocks.length > 0 ? manualLocks.join(", ") : "-"}</dd>
          </dl>
          <div className="retry-summary">
            <label>
              <input
                type="checkbox"
                checked={allowTitleFolderOverride}
                disabled={retryMutation.isPending}
                onChange={(event) => setAllowTitleFolderOverride(event.target.checked)}
              />
              Allow title/folder override
            </label>
            <button
              disabled={retryMutation.isPending}
              onClick={() => retryMutation.mutate({ clientId, windowId: item.id, allowTitleFolderOverride })}
            >
              Retry summary
            </button>
          </div>
          {retryMutation.isError && <p className="error" role="alert">Failed to retry summary.</p>}
          {!showGitTab && (
            <p className="muted detail-git-hint">
              Git tracking appears after an agent registers a linked worktree (skill{" "}
              <code>web-terminal-git-worktree</code>).
            </p>
          )}
        </>
      )}

      {detailTab === "agent" && (
        <>
          <div className="agent-detail-tabs" role="tablist" aria-label="Agent detail">
            <button
              type="button"
              role="tab"
              aria-selected={agentDetailTab === "record"}
              className={agentDetailTab === "record" ? "selected" : undefined}
              onClick={() => setAgentDetailTab("record")}
            >
              Record
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={agentDetailTab === "config"}
              className={agentDetailTab === "config" ? "selected" : undefined}
              onClick={() => setAgentDetailTab("config")}
            >
              Config
            </button>
          </div>
          {agentDetailTab === "record" ? (
            <>
              <AgentRecordViewer
                mode={agentRecord.mode}
                chatRoleFilter={agentRecord.chatRoleFilter}
                chatRecord={agentRecord.chatRecord}
                detailRecord={agentRecord.detailRecord}
                sessions={agentRecord.sessions}
                isLoading={agentRecord.isLoading}
                isError={agentRecord.isError}
                isFetching={agentRecord.isFetching}
                onModeChange={agentRecord.setMode}
                onChatRoleFilterChange={agentRecord.setChatRoleFilter}
                onExpand={() => agentRecord.setExpanded(true)}
                expandShortcutLabel={agentRecordShortcutLabel}
                onSessionChange={agentRecord.resetPages}
                onPreviousPage={agentRecord.previousPage}
                onNextPage={agentRecord.nextPage}
              />
              <AgentRecordModal
                open={agentRecord.expanded}
                mode={agentRecord.mode}
                chatRoleFilter={agentRecord.chatRoleFilter}
                chatRecord={agentRecord.chatRecord}
                detailRecord={agentRecord.detailRecord}
                sessions={agentRecord.sessions}
                isLoading={agentRecord.isLoading}
                isError={agentRecord.isError}
                isFetching={agentRecord.isFetching}
                terminalStatusLabel={terminalStatusLabel}
                terminalStatusTone={terminalStatusTone}
                quickInputDraft={quickInputDraft}
                canSendQuickInput={canSendQuickInput}
                onQuickInputDraftChange={onQuickInputDraftChange}
                onQuickInputSubmit={onQuickInputSubmit}
                onModeChange={agentRecord.setMode}
                onChatRoleFilterChange={agentRecord.setChatRoleFilter}
                onClose={() => agentRecord.setExpanded(false)}
                onSessionChange={agentRecord.resetPages}
                onPreviousPage={agentRecord.previousPage}
                onNextPage={agentRecord.nextPage}
              />
            </>
          ) : (
            <AgentConfigViewer
              config={agentConfig.config}
              isLoading={agentConfig.isLoading}
              isError={agentConfig.isError}
              isFetching={agentConfig.isFetching}
              pendingItemId={agentConfig.pendingItemId}
              isToggling={agentConfig.isToggling}
              toggleError={agentConfig.toggleError}
              onToggleItem={agentConfig.toggleItem}
            />
          )}
        </>
      )}

      {detailTab === "history" && (
        <>
          <div className="history-detail-tabs" role="tablist" aria-label="History detail">
            <button
              type="button"
              role="tab"
              aria-selected={historyDetailTab === "commands"}
              className={historyDetailTab === "commands" ? "selected" : undefined}
              onClick={() => setHistoryDetailTab("commands")}
            >
              Commands
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={historyDetailTab === "title"}
              className={historyDetailTab === "title" ? "selected" : undefined}
              onClick={() => setHistoryDetailTab("title")}
            >
              Title
            </button>
          </div>
          {historyDetailTab === "commands" ? (
            <CommandHistoryViewer
              history={commandHistoryQuery.data ?? null}
              isLoading={commandHistoryQuery.isLoading}
              isError={commandHistoryQuery.isError}
              isFetching={commandHistoryQuery.isFetching}
              onPreviousPage={() => setCommandHistoryPage((page) => Math.max(0, page - 1))}
              onNextPage={() => setCommandHistoryPage((page) => page + 1)}
            />
          ) : (
            <TitleHistoryViewer
              history={titleHistoryQuery.data ?? null}
              isLoading={titleHistoryQuery.isLoading}
              isError={titleHistoryQuery.isError}
              isFetching={titleHistoryQuery.isFetching}
              onPreviousPage={() => setTitleHistoryPage((page) => Math.max(0, page - 1))}
              onNextPage={() => setTitleHistoryPage((page) => page + 1)}
            />
          )}
        </>
      )}

      {detailTab === "git" && showGitTab && <GitRunViewer clientId={clientId} windowId={windowId} />}
    </div>
  );
}
