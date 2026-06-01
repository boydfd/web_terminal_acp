import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { fetchGitRuns } from "../api";
import {
  collectGitDiffCommitOptions,
  commitLabel,
  displayPath,
  fileCount,
  fileDelta,
  fileStatusTone,
  formatGitDateTime,
  gitDiffFileKey,
  patchLines,
  patchLineTone,
  shortSha
} from "../gitDiff";
import type { GitDiffCommitOption } from "../gitDiff";
import type { GitDiffFile } from "../types";
import { useOverlayFocus } from "./useOverlayFocus";

type GitDiffBrowserStep = "commits" | "files" | "diff";

type GitDiffBrowserModalProps = {
  clientId: string;
  windowId: string;
  isMobileLayout: boolean;
  shortcutLabel?: string;
  onClose: () => void;
};

function selectDefaultCommit(options: GitDiffCommitOption[]): GitDiffCommitOption | null {
  return options.find((option) => (option.commit.files ?? []).length > 0) ?? options[0] ?? null;
}

function selectDefaultFile(option: GitDiffCommitOption | null): GitDiffFile | null {
  return option?.commit.files?.[0] ?? null;
}

function commitFileCount(option: GitDiffCommitOption): number {
  return option.commit.files?.length ?? 0;
}

function commitSummary(option: GitDiffCommitOption): string {
  const commit = option.commit;
  const fileCount = commitFileCount(option);
  const authored = commit.authored_at ? formatGitDateTime(commit.authored_at) : null;
  const pieces = [
    shortSha(commit.short_sha || commit.sha),
    `${fileCount} ${fileCount === 1 ? "file" : "files"}`,
    authored,
    option.runTitle
  ].filter(Boolean);
  return pieces.join(" | ");
}

function ensureSelectedCommit(
  options: GitDiffCommitOption[],
  selectedCommitId: string | null
): GitDiffCommitOption | null {
  const selected = options.find((option) => option.id === selectedCommitId) ?? null;
  return selected ?? selectDefaultCommit(options);
}

function ensureSelectedFile(
  selectedCommit: GitDiffCommitOption | null,
  selectedFileKey: string | null
): GitDiffFile | null {
  const files = selectedCommit?.commit.files ?? [];
  return files.find((file) => gitDiffFileKey(file) === selectedFileKey) ?? files[0] ?? null;
}

function GitDiffPatchView({
  commitOption,
  file
}: {
  commitOption: GitDiffCommitOption;
  file: GitDiffFile;
}) {
  const tone = fileStatusTone(file.status);
  return (
    <section className={`git-diff-browser-diff ${tone}`} aria-label="File diff">
      <header className="git-diff-browser-diff-header">
        <div>
          <strong>{displayPath(file)}</strong>
          <p>{commitLabel(commitOption.commit)}</p>
        </div>
        <div className="git-diff-modal-meta">
          <span className={`git-file-status ${tone}`}>{file.status ?? "modified"}</span>
          <span className="git-file-additions">+{fileCount(file.additions)}</span>
          <span className="git-file-deletions">-{fileCount(file.deletions)}</span>
        </div>
      </header>
      <div className="git-diff-patch" role="region" aria-label="File patch">
        {patchLines(file.patch).map((line, index) => (
          <div key={`${index}:${line}`} className={`git-diff-line ${patchLineTone(line)}`}>
            <span className="git-diff-line-number">{index + 1}</span>
            <code>{line || " "}</code>
          </div>
        ))}
      </div>
    </section>
  );
}

function CommitList({
  options,
  selectedCommitId,
  onSelectCommit
}: {
  options: GitDiffCommitOption[];
  selectedCommitId: string | null;
  onSelectCommit: (option: GitDiffCommitOption) => void;
}) {
  return (
    <div className="git-diff-browser-list" role="list" aria-label="Commits">
      {options.map((option) => (
        <button
          key={option.id}
          type="button"
          className={`git-diff-browser-row${selectedCommitId === option.id ? " selected" : ""}`}
          onClick={() => onSelectCommit(option)}
        >
          <span className="git-diff-browser-row-title">{option.commit.subject || "Untitled commit"}</span>
          <span className="git-diff-browser-row-meta">{commitSummary(option)}</span>
        </button>
      ))}
    </div>
  );
}

function FileList({
  commitOption,
  selectedFileKey,
  onSelectFile
}: {
  commitOption: GitDiffCommitOption | null;
  selectedFileKey: string | null;
  onSelectFile: (file: GitDiffFile) => void;
}) {
  const files = commitOption?.commit.files ?? [];

  if (!commitOption) {
    return <p className="muted git-diff-browser-empty">Select a commit first.</p>;
  }
  if (files.length === 0) {
    return <p className="muted git-diff-browser-empty">No file changes captured for this commit.</p>;
  }

  return (
    <div className="git-diff-browser-list" role="list" aria-label="Changed files">
      {files.map((file) => {
        const key = gitDiffFileKey(file);
        const tone = fileStatusTone(file.status);
        return (
          <button
            key={key}
            type="button"
            className={`git-diff-browser-row git-diff-browser-file-row${selectedFileKey === key ? " selected" : ""}`}
            onClick={() => onSelectFile(file)}
            title={displayPath(file)}
          >
            <span className={`git-file-status ${tone}`}>{file.status ?? "modified"}</span>
            <span className="git-diff-browser-row-title">{displayPath(file)}</span>
            <span className="git-file-delta" aria-label={fileDelta(file)}>
              <span className="git-file-additions">+{fileCount(file.additions)}</span>
              <span className="git-file-deletions">-{fileCount(file.deletions)}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

export function GitDiffBrowserModal({
  clientId,
  windowId,
  isMobileLayout,
  shortcutLabel = "Git diff",
  onClose
}: GitDiffBrowserModalProps) {
  const [selectedCommitId, setSelectedCommitId] = useState<string | null>(null);
  const [selectedFileKey, setSelectedFileKey] = useState<string | null>(null);
  const [mobileStep, setMobileStep] = useState<GitDiffBrowserStep>("commits");
  const panelRef = useRef<HTMLElement | null>(null);
  const runsQuery = useQuery({
    queryKey: ["git-runs", clientId, windowId],
    queryFn: () => fetchGitRuns(clientId, windowId),
    refetchInterval: 10000
  });
  const commitOptions = useMemo(
    () => collectGitDiffCommitOptions(runsQuery.data?.runs ?? []),
    [runsQuery.data?.runs]
  );
  const selectedCommit = ensureSelectedCommit(commitOptions, selectedCommitId);
  const selectedFile = ensureSelectedFile(selectedCommit, selectedFileKey);
  const selectedFileStableKey = selectedFile ? gitDiffFileKey(selectedFile) : null;

  useEffect(() => {
    if (commitOptions.length === 0) {
      setSelectedCommitId(null);
      setSelectedFileKey(null);
      return;
    }

    const nextCommit = ensureSelectedCommit(commitOptions, selectedCommitId);
    if (nextCommit?.id !== selectedCommitId) {
      const defaultFile = selectDefaultFile(nextCommit);
      setSelectedCommitId(nextCommit?.id ?? null);
      setSelectedFileKey(defaultFile ? gitDiffFileKey(defaultFile) : null);
      return;
    }

    const nextFile = ensureSelectedFile(nextCommit, selectedFileKey);
    const nextFileKey = nextFile ? gitDiffFileKey(nextFile) : null;
    if (nextFileKey !== selectedFileKey) {
      setSelectedFileKey(nextFileKey);
    }
  }, [commitOptions, selectedCommitId, selectedFileKey]);

  useEffect(() => {
    if (!isMobileLayout) {
      setMobileStep("commits");
    }
  }, [isMobileLayout]);

  const handleEscape = useCallback(() => {
    if (isMobileLayout && mobileStep === "diff") {
      setMobileStep("files");
      return;
    }
    if (isMobileLayout && mobileStep === "files") {
      setMobileStep("commits");
      return;
    }
    onClose();
  }, [isMobileLayout, mobileStep, onClose]);

  useOverlayFocus({
    isOpen: true,
    ref: panelRef,
    onEscape: handleEscape
  });

  const handleSelectCommit = (option: GitDiffCommitOption) => {
    setSelectedCommitId(option.id);
    const file = selectDefaultFile(option);
    setSelectedFileKey(file ? gitDiffFileKey(file) : null);
    if (isMobileLayout) {
      setMobileStep("files");
    }
  };
  const handleSelectFile = (file: GitDiffFile) => {
    setSelectedFileKey(gitDiffFileKey(file));
    if (isMobileLayout) {
      setMobileStep("diff");
    }
  };
  const goBack = () => {
    if (mobileStep === "diff") {
      setMobileStep("files");
      return;
    }
    if (mobileStep === "files") {
      setMobileStep("commits");
      return;
    }
    onClose();
  };
  const title = isMobileLayout && mobileStep === "files"
    ? "Select file"
    : isMobileLayout && mobileStep === "diff"
      ? "File diff"
      : "Git diff";
  const subtitle = selectedCommit
    ? `${commitLabel(selectedCommit.commit)} | ${selectedCommit.runTitle}`
    : shortcutLabel;

  return (
    <div className="git-diff-browser-modal" role="dialog" aria-modal="true" aria-label="Git diff browser">
      <section ref={panelRef} className={`git-diff-browser-panel${isMobileLayout ? " mobile" : ""}`}>
        <header className="git-diff-browser-header">
          <div>
            <strong>{title}</strong>
            <p>{subtitle}</p>
          </div>
          <div className="git-diff-browser-header-actions">
            {isMobileLayout && mobileStep !== "commits" && (
              <button type="button" onClick={goBack}>Back</button>
            )}
            <button type="button" onClick={onClose}>Close</button>
          </div>
        </header>
        {runsQuery.isLoading && <p className="muted git-diff-browser-empty">Loading git diff...</p>}
        {runsQuery.isError && <p className="error git-diff-browser-empty" role="alert">Failed to load git diff.</p>}
        {runsQuery.data && !runsQuery.data.supported && (
          <p className="muted git-diff-browser-empty">
            No linked git worktree bound to this terminal.
          </p>
        )}
        {runsQuery.data?.supported && commitOptions.length === 0 && (
          <p className="muted git-diff-browser-empty">No committed file diff captured yet.</p>
        )}
        {runsQuery.data?.supported && commitOptions.length > 0 && !isMobileLayout && (
          <div className="git-diff-browser-grid">
            <section className="git-diff-browser-column" aria-label="Commit selection">
              <div className="git-diff-browser-column-header">
                <strong>Commits</strong>
                <span>{commitOptions.length}</span>
              </div>
              <CommitList
                options={commitOptions}
                selectedCommitId={selectedCommit?.id ?? null}
                onSelectCommit={handleSelectCommit}
              />
            </section>
            <section className="git-diff-browser-column" aria-label="File selection">
              <div className="git-diff-browser-column-header">
                <strong>Files</strong>
                <span>{selectedCommit ? commitFileCount(selectedCommit) : 0}</span>
              </div>
              <FileList
                commitOption={selectedCommit}
                selectedFileKey={selectedFileStableKey}
                onSelectFile={handleSelectFile}
              />
            </section>
            {selectedCommit && selectedFile ? (
              <GitDiffPatchView commitOption={selectedCommit} file={selectedFile} />
            ) : (
              <p className="muted git-diff-browser-empty">Select a file to inspect its patch.</p>
            )}
          </div>
        )}
        {runsQuery.data?.supported && commitOptions.length > 0 && isMobileLayout && (
          <div className="git-diff-browser-mobile-step">
            {mobileStep === "commits" && (
              <CommitList
                options={commitOptions}
                selectedCommitId={selectedCommit?.id ?? null}
                onSelectCommit={handleSelectCommit}
              />
            )}
            {mobileStep === "files" && (
              <FileList
                commitOption={selectedCommit}
                selectedFileKey={selectedFileStableKey}
                onSelectFile={handleSelectFile}
              />
            )}
            {mobileStep === "diff" && selectedCommit && selectedFile && (
              <GitDiffPatchView commitOption={selectedCommit} file={selectedFile} />
            )}
          </div>
        )}
      </section>
    </div>
  );
}
