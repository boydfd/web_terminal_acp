import { useCallback, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { CSSProperties } from "react";

import { fetchGitRuns } from "../api";
import {
  basename,
  commitLabel,
  displayPath,
  fileCount,
  fileDelta,
  fileStatusTone,
  formatGitDateTime,
  gitRunTitle,
  patchLines,
  patchLineTone,
  shortSha,
  treeFileLabel
} from "../gitDiff";
import type { GitDiffCommit, GitDiffFile, GitWorktreeRun } from "../types";
import { useOverlayFocus } from "./useOverlayFocus";

type GitRunViewerProps = {
  clientId: string;
  windowId: string;
};

type SelectedDiff = {
  commit: GitDiffCommit;
  file: GitDiffFile;
};

type FileViewMode = "list" | "tree";

type GitFileTreeNode = {
  name: string;
  path: string;
  children: GitFileTreeNode[];
  files: GitDiffFile[];
};

type GitTreeDepthStyle = CSSProperties & {
  "--git-file-tree-depth": number;
};

function snapshotText(run: GitWorktreeRun, key: "start_snapshot_json" | "end_snapshot_json", field: string): string {
  const snapshot = run[key];
  const value = snapshot?.[field];
  return typeof value === "string" && value.trim() ? value : "-";
}

function filterCommits(commits: GitDiffCommit[], selectedSha: string): GitDiffCommit[] {
  if (selectedSha === "all") {
    return commits;
  }
  return commits.filter((commit) => commit.sha === selectedSha);
}

function buildFileTree(files: GitDiffFile[]): GitFileTreeNode {
  const root: GitFileTreeNode = { name: "", path: "", children: [], files: [] };
  for (const file of files) {
    const parts = file.path.split("/").filter(Boolean);
    if (parts.length === 0) {
      root.files.push(file);
      continue;
    }
    let node = root;
    for (const part of parts.slice(0, -1)) {
      const path = node.path ? `${node.path}/${part}` : part;
      let child = node.children.find((candidate) => candidate.name === part);
      if (!child) {
        child = { name: part, path, children: [], files: [] };
        node.children.push(child);
      }
      node = child;
    }
    node.files.push(file);
  }
  sortFileTree(root);
  return root;
}

function sortFileTree(node: GitFileTreeNode): void {
  node.children.sort((left, right) => left.name.localeCompare(right.name));
  node.files.sort((left, right) => basename(left.path).localeCompare(basename(right.path)));
  node.children.forEach(sortFileTree);
}

function RunCard({ run }: { run: GitWorktreeRun }) {
  const [selectedCommitSha, setSelectedCommitSha] = useState("all");
  const [selectedDiff, setSelectedDiff] = useState<SelectedDiff | null>(null);
  const [fileViewMode, setFileViewMode] = useState<FileViewMode>("tree");
  const diff = run.session_diff_json;
  const commits = diff?.commits ?? [];
  const visibleCommits = useMemo(
    () => filterCommits(commits, selectedCommitSha),
    [commits, selectedCommitSha]
  );
  const title = gitRunTitle(run);

  return (
    <article className="git-run-card">
      <header className="git-run-card-header">
        <strong>{title}</strong>
        <span className={`git-run-status ${run.pending_commit ? "pending" : ""}`}>
          {run.pending_commit ? "pending commit" : run.status}
        </span>
      </header>
      <dl className="detail-list git-run-meta">
        <dt>Type</dt>
        <dd>{run.run_type}</dd>
        <dt>Provider</dt>
        <dd>{run.agent_provider ?? "-"}</dd>
        <dt>Worktree</dt>
        <dd>{run.worktree_root ?? "-"}</dd>
        <dt>Discovery</dt>
        <dd>{run.discovery_method ?? "-"}</dd>
        <dt>Started</dt>
        <dd>{formatGitDateTime(run.started_at)}</dd>
        <dt>Ended</dt>
        <dd>{run.ended_at ? formatGitDateTime(run.ended_at) : "-"}</dd>
        <dt>Pending commit</dt>
        <dd>{run.pending_commit ? "Yes" : "No"}</dd>
        {run.run_type === "tracking" && (
          <>
            <dt>Start HEAD</dt>
            <dd><code>{snapshotText(run, "start_snapshot_json", "head_sha")}</code></dd>
            <dt>Current HEAD</dt>
            <dd><code>{snapshotText(run, "end_snapshot_json", "head_sha")}</code></dd>
          </>
        )}
      </dl>
      {diff?.head_moved && (
        <p className="git-run-head-range">
          HEAD <code>{shortSha(diff.start_head)}</code> {"->"} <code>{shortSha(diff.end_head)}</code>
        </p>
      )}
      {commits.length > 0 ? (
        <section className="git-commit-browser" aria-label="Git commit changes">
          <label>
            <span>Commit</span>
            <select value={selectedCommitSha} onChange={(event) => setSelectedCommitSha(event.target.value)}>
              <option value="all">All commits ({commits.length})</option>
              {commits.map((commit) => (
                <option key={commit.sha} value={commit.sha}>
                  {commitLabel(commit)}
                </option>
              ))}
            </select>
          </label>
          <div className="git-file-view-toggle" role="group" aria-label="File display mode">
            <button
              type="button"
              className={fileViewMode === "tree" ? "selected" : ""}
              onClick={() => setFileViewMode("tree")}
              aria-pressed={fileViewMode === "tree"}
            >
              Tree
            </button>
            <button
              type="button"
              className={fileViewMode === "list" ? "selected" : ""}
              onClick={() => setFileViewMode("list")}
              aria-pressed={fileViewMode === "list"}
            >
              List
            </button>
          </div>
          <div className="git-commit-list">
            {visibleCommits.map((commit) => (
              <section key={commit.sha} className="git-commit-item">
                <header>
                  <div>
                    <strong>{commit.subject || "Untitled commit"}</strong>
                    <code>{shortSha(commit.short_sha || commit.sha)}</code>
                  </div>
                  {commit.authored_at && <time>{formatGitDateTime(commit.authored_at)}</time>}
                </header>
                <CommitFileBrowser
                  commit={commit}
                  mode={fileViewMode}
                  onSelectFile={(file) => setSelectedDiff({ commit, file })}
                />
              </section>
            ))}
          </div>
        </section>
      ) : (
        diff && <p className="muted">No committed file diff captured yet.</p>
      )}
      {selectedDiff && (
        <GitDiffModal
          selection={selectedDiff}
          onClose={() => setSelectedDiff(null)}
        />
      )}
    </article>
  );
}

function CommitFileBrowser({
  commit,
  mode,
  onSelectFile
}: {
  commit: GitDiffCommit;
  mode: FileViewMode;
  onSelectFile: (file: GitDiffFile) => void;
}) {
  const files = commit.files ?? [];
  const fileTree = useMemo(() => buildFileTree(files), [files]);

  if (files.length === 0) {
    return <p className="muted">No file changes captured for this commit.</p>;
  }
  if (mode === "tree") {
    return (
      <div className="git-file-tree" aria-label="Changed files tree">
        {fileTree.files.map((file) => (
          <GitFileButton
            key={`${commit.sha}:${file.old_path ?? ""}:${file.path}`}
            file={file}
            label={treeFileLabel(file)}
            path={displayPath(file)}
            onSelect={() => onSelectFile(file)}
          />
        ))}
        {fileTree.children.map((node) => (
          <GitFileTreeBranch
            key={node.path}
            node={node}
            commitSha={commit.sha}
            depth={0}
            onSelectFile={onSelectFile}
          />
        ))}
      </div>
    );
  }
  return (
    <ul className="git-file-list">
      {files.map((file) => (
        <li key={`${commit.sha}:${file.old_path ?? ""}:${file.path}`}>
          <GitFileButton file={file} path={displayPath(file)} onSelect={() => onSelectFile(file)} />
        </li>
      ))}
    </ul>
  );
}

function GitFileTreeBranch({
  node,
  commitSha,
  depth,
  onSelectFile
}: {
  node: GitFileTreeNode;
  commitSha: string;
  depth: number;
  onSelectFile: (file: GitDiffFile) => void;
}) {
  return (
    <div className="git-file-tree-branch">
      <div className="git-file-tree-directory" style={{ "--git-file-tree-depth": depth } as GitTreeDepthStyle}>
        <span>{node.name}</span>
      </div>
      {node.files.map((file) => (
        <GitFileButton
          key={`${commitSha}:${file.old_path ?? ""}:${file.path}`}
          file={file}
          label={treeFileLabel(file)}
          path={displayPath(file)}
          depth={depth + 1}
          onSelect={() => onSelectFile(file)}
        />
      ))}
      {node.children.map((child) => (
        <GitFileTreeBranch
          key={child.path}
          node={child}
          commitSha={commitSha}
          depth={depth + 1}
          onSelectFile={onSelectFile}
        />
      ))}
    </div>
  );
}

function GitFileButton({
  file,
  label,
  path,
  depth = 0,
  onSelect
}: {
  file: GitDiffFile;
  label?: string;
  path: string;
  depth?: number;
  onSelect: () => void;
}) {
  const statusTone = fileStatusTone(file.status);
  return (
    <button
      type="button"
      className="git-file-button"
      style={{ "--git-file-tree-depth": depth } as GitTreeDepthStyle}
      onClick={onSelect}
      title={path}
    >
      <span className={`git-file-status ${statusTone}`}>{file.status ?? "modified"}</span>
      <span className="git-file-path">{label ?? path}</span>
      <span className="git-file-delta" aria-label={fileDelta(file)}>
        <span className="git-file-additions">+{fileCount(file.additions)}</span>
        <span className="git-file-deletions">-{fileCount(file.deletions)}</span>
      </span>
    </button>
  );
}

function GitDiffModal({ selection, onClose }: { selection: SelectedDiff; onClose: () => void }) {
  const { commit, file } = selection;
  const panelRef = useRef<HTMLElement | null>(null);
  const handleEscape = useCallback(() => {
    onClose();
  }, [onClose]);

  useOverlayFocus({
    isOpen: true,
    ref: panelRef,
    onEscape: handleEscape
  });

  return (
    <div className="git-diff-modal" role="dialog" aria-modal="true" aria-label="Git file diff">
      <button type="button" className="git-diff-modal-backdrop" aria-label="Close diff" onClick={onClose} />
      <section ref={panelRef} className={`git-diff-modal-panel ${fileStatusTone(file.status)}`}>
        <header>
          <div>
            <strong>{displayPath(file)}</strong>
            <p>{commitLabel(commit)}</p>
          </div>
          <button type="button" onClick={onClose}>Close</button>
        </header>
        <div className="git-diff-modal-meta">
          <span className={`git-file-status ${fileStatusTone(file.status)}`}>{file.status ?? "modified"}</span>
          <span className="git-file-additions">+{fileCount(file.additions)}</span>
          <span className="git-file-deletions">-{fileCount(file.deletions)}</span>
        </div>
        <div className="git-diff-patch" role="region" aria-label="File patch">
          {patchLines(file.patch).map((line, index) => (
            <div key={`${index}:${line}`} className={`git-diff-line ${patchLineTone(line)}`}>
              <span className="git-diff-line-number">{index + 1}</span>
              <code>{line || " "}</code>
            </div>
          ))}
        </div>
      </section>
    </div>
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
    return <p className="muted">No git worktree tracking records yet.</p>;
  }

  return (
    <div className="git-run-list">
      {runsQuery.data.runs.map((run) => (
        <RunCard key={run.id} run={run} />
      ))}
    </div>
  );
}
