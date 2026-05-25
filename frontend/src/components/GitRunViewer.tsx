import { useQuery } from "@tanstack/react-query";

import { fetchGitRuns } from "../api";
import type { GitWorktreeRun } from "../types";

type GitRunViewerProps = {
  clientId: string;
  windowId: string;
};

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function RunCard({ run }: { run: GitWorktreeRun }) {
  const diff = run.session_diff_json;
  return (
    <article className="git-run-card">
      <header className="git-run-card-header">
        <strong>Agent run #{run.command_sequence}</strong>
        <span className="muted">{run.status}</span>
      </header>
      <dl className="detail-list git-run-meta">
        <dt>Provider</dt>
        <dd>{run.agent_provider ?? "-"}</dd>
        <dt>Worktree</dt>
        <dd>{run.worktree_root ?? "-"}</dd>
        <dt>Discovery</dt>
        <dd>{run.discovery_method ?? "-"}</dd>
        <dt>Started</dt>
        <dd>{formatDateTime(run.started_at)}</dd>
        <dt>Ended</dt>
        <dd>{run.ended_at ? formatDateTime(run.ended_at) : "-"}</dd>
        <dt>Pending commit</dt>
        <dd>{run.pending_commit ? "Yes" : "No"}</dd>
      </dl>
      {diff && (
        <div className="git-run-diff">
          {diff.head_moved && (
            <p>
              HEAD: <code>{String(diff.start_head ?? "?")}</code> → <code>{String(diff.end_head ?? "?")}</code>
            </p>
          )}
          {diff.end_diff_stat?.trim() && <pre>{diff.end_diff_stat}</pre>}
          {diff.end_status_porcelain?.trim() && <pre>{diff.end_status_porcelain}</pre>}
        </div>
      )}
    </article>
  );
}

export function GitRunViewer({ clientId, windowId }: GitRunViewerProps) {
  const runsQuery = useQuery({
    queryKey: ["git-runs", clientId, windowId],
    queryFn: () => fetchGitRuns(clientId, windowId),
    refetchInterval: 10000
  });

  if (runsQuery.isLoading) {
    return <p className="muted">Loading git runs...</p>;
  }
  if (runsQuery.isError) {
    return <p className="error" role="alert">Failed to load git runs.</p>;
  }
  if (!runsQuery.data?.supported) {
    return (
      <p className="muted">
        No linked git worktree bound to this terminal. Use skill <code>web-terminal-git-worktree</code> in the agent
        shell.
      </p>
    );
  }
  if (runsQuery.data.runs.length === 0) {
    return <p className="muted">No agent git runs recorded yet.</p>;
  }

  return (
    <div className="git-run-list">
      {runsQuery.data.runs.map((run) => (
        <RunCard key={run.id} run={run} />
      ))}
    </div>
  );
}
