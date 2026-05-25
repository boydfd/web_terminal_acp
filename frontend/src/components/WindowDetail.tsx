import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { fetchAgentRecordChat, fetchAgentRecordDetail, fetchWindow, retrySummary } from "../api";
import type { GitWorktreeActivity, SummaryJob, VirtualWindow } from "../types";
import { AgentRecordViewer, type AgentRecordDisplayMode } from "./AgentRecordViewer";
import { DetailPanelTabs, type DetailPanelTab } from "./DetailPanelTabs";
import { GitRunViewer } from "./GitRunViewer";
import { WorkStatusBadge } from "./WorkStatusBadge";

type WindowDetailProps = {
  clientId: string | null;
  windowId: string | null;
  gitWorktree?: GitWorktreeActivity | null;
  agentRecordExpandSignal?: number;
};

type SummaryStatus = {
  label: string;
  tone?: "muted" | "error";
};

const AGENT_RECORD_CHAT_PAGE_SIZE = 30;
const AGENT_RECORD_DETAIL_PAGE_SIZE = 100;

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

export function WindowDetail({
  clientId,
  windowId,
  gitWorktree = null,
  agentRecordExpandSignal = 0
}: WindowDetailProps) {
  const [allowTitleFolderOverride, setAllowTitleFolderOverride] = useState(false);
  const [detailTab, setDetailTab] = useState<DetailPanelTab>("overview");
  const [agentRecordMode, setAgentRecordMode] = useState<AgentRecordDisplayMode>("chat");
  const [agentChatPage, setAgentChatPage] = useState(0);
  const [agentDetailPage, setAgentDetailPage] = useState(0);
  const [agentRecordExpanded, setAgentRecordExpanded] = useState(false);
  const queryClient = useQueryClient();
  const showGitTab = gitWorktree !== null;

  useEffect(() => {
    setDetailTab("overview");
    setAgentRecordMode("chat");
    setAgentChatPage(0);
    setAgentDetailPage(0);
    setAgentRecordExpanded(false);
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
  const agentChatRecordQuery = useQuery({
    queryKey: ["agent-record", "chat", clientId, windowId, agentChatPage, AGENT_RECORD_CHAT_PAGE_SIZE],
    queryFn: () => fetchAgentRecordChat(
      clientId as string,
      windowId as string,
      AGENT_RECORD_CHAT_PAGE_SIZE,
      agentChatPage * AGENT_RECORD_CHAT_PAGE_SIZE
    ),
    enabled: clientId !== null && windowId !== null && detailTab === "agent" && agentRecordMode === "chat",
    placeholderData: keepPreviousData,
    refetchInterval: 10000
  });
  const agentDetailRecordQuery = useQuery({
    queryKey: ["agent-record", "detail", clientId, windowId, agentDetailPage, AGENT_RECORD_DETAIL_PAGE_SIZE],
    queryFn: () => fetchAgentRecordDetail(
      clientId as string,
      windowId as string,
      AGENT_RECORD_DETAIL_PAGE_SIZE,
      agentDetailPage * AGENT_RECORD_DETAIL_PAGE_SIZE
    ),
    enabled:
      clientId !== null
      && windowId !== null
      && detailTab === "agent"
      && (agentRecordMode === "detail" || agentRecordExpanded),
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
  const manualLocks = [
    item.title_manually_overridden ? "title locked" : null,
    item.folder_manually_overridden ? "folder locked" : null
  ].filter((lock): lock is string => lock !== null);
  const agentRecordQuery = agentRecordMode === "chat" ? agentChatRecordQuery : agentDetailRecordQuery;

  return (
    <div>
      <h2>{item.title}</h2>
      <DetailPanelTabs activeTab={detailTab} showGitTab={showGitTab} onTabChange={setDetailTab} />

      {detailTab === "overview" && (
        <>
          <dl className="detail-list">
            <dt>Status</dt>
            <dd>{item.status}</dd>
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
        <AgentRecordViewer
          mode={agentRecordMode}
          chatRecord={agentChatRecordQuery.data ?? null}
          detailRecord={agentDetailRecordQuery.data ?? null}
          sessions={agentDetailRecordQuery.data?.sessions ?? []}
          expandSignal={agentRecordExpandSignal}
          isLoading={agentRecordQuery.isLoading}
          isError={agentRecordQuery.isError}
          isFetching={agentRecordQuery.isFetching}
          onModeChange={(mode) => {
            setAgentRecordMode(mode);
            if (mode === "chat") setAgentChatPage(0);
            else setAgentDetailPage(0);
          }}
          onExpandedChange={setAgentRecordExpanded}
          onSessionChange={() => {
            setAgentChatPage(0);
            setAgentDetailPage(0);
          }}
          onPreviousPage={() => {
            if (agentRecordMode === "chat") setAgentChatPage((page) => Math.max(0, page - 1));
            else setAgentDetailPage((page) => Math.max(0, page - 1));
          }}
          onNextPage={() => {
            if (agentRecordMode === "chat") setAgentChatPage((page) => page + 1);
            else setAgentDetailPage((page) => page + 1);
          }}
        />
      )}

      {detailTab === "git" && showGitTab && <GitRunViewer clientId={clientId} windowId={windowId} />}
    </div>
  );
}
