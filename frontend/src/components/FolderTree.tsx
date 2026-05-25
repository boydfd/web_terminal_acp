import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { fetchProjectSummaries, summarizeProject } from "../api";
import {
  buildTerminalSwitcherTree,
  findPathToSwitcherWindow,
  type SwitcherNode,
  type TerminalGroupingMode
} from "../terminalGrouping";
import type { SummaryOutputLanguage } from "../userPreferences";
import type { ProjectSummary, TreeFolder, TreeWindow } from "../types";
import { GitPendingBadge } from "./GitPendingBadge";
import { TerminalUnreadDot } from "./NotificationCenter";
import { WorkStatusDot } from "./WorkStatusBadge";

type FolderTreeProps = {
  clientId: string | null;
  folders: TreeFolder[];
  groupingMode: TerminalGroupingMode;
  summaryOutputLanguage: SummaryOutputLanguage;
  selectedWindowId: string | null;
  deletingWindowId?: string | null;
  hasUnreadNotification?: (windowId: string) => boolean;
  onSelectWindow: (window: TreeWindow) => void;
  onDeleteWindow: (window: TreeWindow) => void;
};

type CollapsedState = {
  storageKey: string;
  keys: Set<string>;
};

function collapsedStorageKey(clientId: string | null, groupingMode: TerminalGroupingMode): string {
  return `web-terminal-acp:terminals-tree:collapsed:${clientId ?? "no-client"}:${groupingMode}`;
}

function readCollapsedKeys(storageKey: string): Set<string> | null {
  if (typeof window === "undefined") {
    return null;
  }

  try {
    const rawValue = window.localStorage.getItem(storageKey);
    if (rawValue === null) {
      return null;
    }

    const parsedValue: unknown = JSON.parse(rawValue);
    if (!Array.isArray(parsedValue)) {
      return null;
    }

    return new Set(parsedValue.filter((value): value is string => typeof value === "string"));
  } catch {
    return null;
  }
}

function defaultCollapsedKeys(nodes: SwitcherNode[]): Set<string> {
  const keys = new Set<string>();

  const visit = (node: SwitcherNode, depth: number) => {
    if (node.type === "window") {
      return;
    }

    if (depth > 0) {
      keys.add(node.key);
    }

    for (const child of node.children) {
      visit(child, depth + 1);
    }
  };

  for (const node of nodes) {
    visit(node, 0);
  }

  return keys;
}

function loadCollapsedKeys(storageKey: string, displayTree: SwitcherNode[]): Set<string> {
  return readCollapsedKeys(storageKey) ?? defaultCollapsedKeys(displayTree);
}

function writeCollapsedKeys(storageKey: string, keys: Set<string>) {
  if (typeof window === "undefined") {
    return;
  }

  try {
    window.localStorage.setItem(storageKey, JSON.stringify(Array.from(keys)));
  } catch {
    return;
  }
}

function WindowNode({
  window,
  selectedWindowId,
  deletingWindowId,
  hasUnreadNotification,
  onSelectWindow,
  onDeleteWindow
}: {
  window: TreeWindow;
  selectedWindowId: string | null;
  deletingWindowId?: string | null;
  hasUnreadNotification?: (windowId: string) => boolean;
  onSelectWindow: (window: TreeWindow) => void;
  onDeleteWindow: (window: TreeWindow) => void;
}) {
  const isSelected = window.id === selectedWindowId;
  const isDeleting = deletingWindowId === window.id;
  const showUnreadDot = hasUnreadNotification?.(window.id) ?? false;

  return (
    <li className="tree-window-row">
      <button
        type="button"
        aria-current={isSelected ? "true" : undefined}
        className={isSelected ? "tree-window selected" : "tree-window"}
        onClick={() => onSelectWindow(window)}
        title={`${window.work_status.label}: ${window.title}`}
      >
        <span className="tree-window-line">
          <WorkStatusDot status={window.work_status} />
          <span className="tree-window-title">{window.title}</span>
          <TerminalUnreadDot visible={showUnreadDot} />
        </span>
      </button>
      <button
        type="button"
        className="tree-window-delete"
        aria-label={`Delete ${window.title}`}
        disabled={isDeleting}
        onClick={(event) => {
          event.stopPropagation();
          onDeleteWindow(window);
        }}
      >
        ×
      </button>
    </li>
  );
}

function DisplayTreeNode({
  node,
  collapsedKeys,
  selectedPathKeys,
  selectedWindowId,
  deletingWindowId,
  summarizingProjectPath,
  hasUnreadNotification,
  onSelectWindow,
  onDeleteWindow,
  onToggleGroup,
  onSummarizeProject
}: {
  node: SwitcherNode;
  collapsedKeys: Set<string>;
  selectedPathKeys: Set<string>;
  selectedWindowId: string | null;
  deletingWindowId?: string | null;
  summarizingProjectPath: string | null;
  hasUnreadNotification?: (windowId: string) => boolean;
  onSelectWindow: (window: TreeWindow) => void;
  onDeleteWindow: (window: TreeWindow) => void;
  onToggleGroup: (key: string) => void;
  onSummarizeProject?: (projectPath: string) => void;
}) {
  if (node.type === "window") {
    return (
      <WindowNode
        window={node.window}
        selectedWindowId={selectedWindowId}
        deletingWindowId={deletingWindowId}
        hasUnreadNotification={hasUnreadNotification}
        onSelectWindow={onSelectWindow}
        onDeleteWindow={onDeleteWindow}
      />
    );
  }

  const isExpanded = selectedPathKeys.has(node.key) || !collapsedKeys.has(node.key);
  const showSummarize = node.projectPath !== undefined && onSummarizeProject !== undefined;
  const isSummarizing = showSummarize && summarizingProjectPath === node.projectPath;

  return (
    <li className="folder-node">
      <div className="switcher-folder-row tree-folder-row">
        <button
          type="button"
          className="folder-label-button"
          aria-expanded={isExpanded}
          onClick={() => onToggleGroup(node.key)}
          title={node.projectPath ?? node.label}
        >
          <span className="disclosure" aria-hidden="true">{isExpanded ? "▾" : "▸"}</span>
          <span>{node.label}</span>
          <span className="count">{node.count}</span>
        </button>
        {showSummarize && (
          <button
            type="button"
            className="switcher-summarize-button tree-summarize-button"
            disabled={isSummarizing}
            aria-label={`总结项目 ${node.label}`}
            title="使用目录文件与最近输入生成项目名"
            onClick={(event) => {
              event.stopPropagation();
              onSummarizeProject(node.projectPath as string);
            }}
          >
            {isSummarizing ? "…" : "总结"}
          </button>
        )}
      </div>
      {isExpanded && (
        <ul>
          {node.children.map((child) => (
            <DisplayTreeNode
              key={child.key}
              node={child}
              collapsedKeys={collapsedKeys}
              selectedPathKeys={selectedPathKeys}
              selectedWindowId={selectedWindowId}
              deletingWindowId={deletingWindowId}
              summarizingProjectPath={summarizingProjectPath}
              hasUnreadNotification={hasUnreadNotification}
              onSelectWindow={onSelectWindow}
              onDeleteWindow={onDeleteWindow}
              onToggleGroup={onToggleGroup}
              onSummarizeProject={onSummarizeProject}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

export function FolderTree({
  clientId,
  folders,
  groupingMode,
  summaryOutputLanguage,
  selectedWindowId,
  deletingWindowId,
  hasUnreadNotification,
  onSelectWindow,
  onDeleteWindow
}: FolderTreeProps) {
  const queryClient = useQueryClient();
  const [summarizingProjectPath, setSummarizingProjectPath] = useState<string | null>(null);
  const projectSummariesQuery = useQuery({
    queryKey: ["project-summaries", clientId],
    queryFn: () => fetchProjectSummaries(clientId as string),
    enabled: clientId !== null
  });
  const projectSummaryLookup = useMemo(() => {
    const lookup = new Map<string, ProjectSummary>();
    for (const summary of projectSummariesQuery.data ?? []) {
      lookup.set(summary.project_path, summary);
    }
    return lookup;
  }, [projectSummariesQuery.data]);
  const summarizeMutation = useMutation({
    mutationFn: (projectPath: string) => summarizeProject(clientId as string, projectPath, summaryOutputLanguage),
    onMutate: (projectPath) => {
      setSummarizingProjectPath(projectPath);
    },
    onSettled: () => {
      setSummarizingProjectPath(null);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["project-summaries", clientId] });
    }
  });
  const displayTree = useMemo(
    () => buildTerminalSwitcherTree(folders, groupingMode, projectSummaryLookup, ""),
    [folders, groupingMode, projectSummaryLookup]
  );
  const storageKey = collapsedStorageKey(clientId, groupingMode);
  const [collapsedState, setCollapsedState] = useState<CollapsedState>(() => ({
    storageKey,
    keys: loadCollapsedKeys(storageKey, displayTree)
  }));
  const collapsedKeys = collapsedState.storageKey === storageKey ? collapsedState.keys : loadCollapsedKeys(storageKey, displayTree);
  const selectedPathKeys = useMemo(
    () => new Set(selectedWindowId === null ? [] : findPathToSwitcherWindow(displayTree, selectedWindowId)),
    [displayTree, selectedWindowId]
  );

  useEffect(() => {
    setCollapsedState({ storageKey, keys: loadCollapsedKeys(storageKey, displayTree) });
  }, [displayTree, storageKey]);

  useEffect(() => {
    if (collapsedState.storageKey === storageKey) {
      writeCollapsedKeys(storageKey, collapsedState.keys);
    }
  }, [collapsedState, storageKey]);

  const toggleGroup = (key: string) => {
    setCollapsedState((currentState) => {
      const nextKeys = new Set(currentState.storageKey === storageKey ? currentState.keys : loadCollapsedKeys(storageKey, displayTree));
      if (nextKeys.has(key)) {
        nextKeys.delete(key);
      } else {
        nextKeys.add(key);
      }

      return { storageKey, keys: nextKeys };
    });
  };

  return (
    <div>
      <div className="tree-header">
        <h2>Terminals</h2>
      </div>
      <ul className="tree-root">
        {displayTree.map((node) => (
          <DisplayTreeNode
            key={node.key}
            node={node}
            collapsedKeys={collapsedKeys}
            selectedPathKeys={selectedPathKeys}
            selectedWindowId={selectedWindowId}
            deletingWindowId={deletingWindowId}
            summarizingProjectPath={summarizingProjectPath}
            hasUnreadNotification={hasUnreadNotification}
            onSelectWindow={onSelectWindow}
            onDeleteWindow={onDeleteWindow}
            onToggleGroup={toggleGroup}
            onSummarizeProject={
              groupingMode === "project-topic" && clientId !== null
                ? (projectPath) => summarizeMutation.mutate(projectPath)
                : undefined
            }
          />
        ))}
      </ul>
    </div>
  );
}
