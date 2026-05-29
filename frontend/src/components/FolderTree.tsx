import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchProjectSummaries, summarizeProject } from "../api";
import {
  buildTerminalSwitcherTree,
  canCreateWindowAtGroupNode,
  findPathToSwitcherWindow,
  terminalGroupingModeHasProjectRoot,
  type SwitcherGroupNode,
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
  locateSelectedWindowSignal?: number;
  deletingWindowId?: string | null;
  hasUnreadNotification?: (windowId: string) => boolean;
  onSelectWindow: (window: TreeWindow) => void;
  onDeleteWindow: (window: TreeWindow) => void;
  onCreateTerminalAtGroup?: (node: SwitcherGroupNode) => void;
  creatingTerminal?: boolean;
  createTerminalDisabled?: boolean;
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
  locatingWindowId,
  registerWindowButton,
  deletingWindowId,
  hasUnreadNotification,
  onSelectWindow,
  onDeleteWindow
}: {
  window: TreeWindow;
  selectedWindowId: string | null;
  locatingWindowId: string | null;
  registerWindowButton: (windowId: string, element: HTMLButtonElement | null) => void;
  deletingWindowId?: string | null;
  hasUnreadNotification?: (windowId: string) => boolean;
  onSelectWindow: (window: TreeWindow) => void;
  onDeleteWindow: (window: TreeWindow) => void;
}) {
  const isSelected = window.id === selectedWindowId;
  const isLocating = window.id === locatingWindowId;
  const isDeleting = deletingWindowId === window.id;
  const showUnreadDot = hasUnreadNotification?.(window.id) ?? false;
  const handleWindowButtonRef = useCallback((element: HTMLButtonElement | null) => {
    registerWindowButton(window.id, element);
  }, [registerWindowButton, window.id]);

  return (
    <li className="tree-window-row">
      <button
        type="button"
        ref={handleWindowButtonRef}
        aria-current={isSelected ? "true" : undefined}
        className={[
          "tree-window",
          isSelected ? "selected" : "",
          isLocating ? "locating" : ""
        ].filter(Boolean).join(" ")}
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
  locatingWindowId,
  registerWindowButton,
  deletingWindowId,
  summarizingProjectPath,
  hasUnreadNotification,
  onSelectWindow,
  onDeleteWindow,
  onToggleGroup,
  onSummarizeProject,
  onCreateTerminalAtGroup,
  creatingTerminal,
  createTerminalDisabled
}: {
  node: SwitcherNode;
  collapsedKeys: Set<string>;
  selectedPathKeys: Set<string>;
  selectedWindowId: string | null;
  locatingWindowId: string | null;
  registerWindowButton: (windowId: string, element: HTMLButtonElement | null) => void;
  deletingWindowId?: string | null;
  summarizingProjectPath: string | null;
  hasUnreadNotification?: (windowId: string) => boolean;
  onSelectWindow: (window: TreeWindow) => void;
  onDeleteWindow: (window: TreeWindow) => void;
  onToggleGroup: (key: string) => void;
  onSummarizeProject?: (projectPath: string) => void;
  onCreateTerminalAtGroup?: (node: SwitcherGroupNode) => void;
  creatingTerminal?: boolean;
  createTerminalDisabled?: boolean;
}) {
  if (node.type === "window") {
    return (
      <WindowNode
        window={node.window}
        selectedWindowId={selectedWindowId}
        locatingWindowId={locatingWindowId}
        registerWindowButton={registerWindowButton}
        deletingWindowId={deletingWindowId}
        hasUnreadNotification={hasUnreadNotification}
        onSelectWindow={onSelectWindow}
        onDeleteWindow={onDeleteWindow}
      />
    );
  }

  const isExpanded = selectedPathKeys.has(node.key) || !collapsedKeys.has(node.key);
  const showSummarize = node.projectPath !== undefined && !node.topicPath && onSummarizeProject !== undefined;
  const isSummarizing = showSummarize && summarizingProjectPath === node.projectPath;
  const showCreateTerminal = onCreateTerminalAtGroup !== undefined && canCreateWindowAtGroupNode(node);
  const createLabel = node.projectPath ?? node.label;

  return (
    <li className="folder-node">
      <div className="switcher-folder-row tree-folder-row">
        <button
          type="button"
          className="folder-label-button"
          aria-expanded={isExpanded}
          onClick={() => onToggleGroup(node.key)}
          title={node.projectPath ?? node.topicPath ?? node.label}
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
        {showCreateTerminal && (
          <button
            type="button"
            className="switcher-create-terminal-button tree-create-terminal-button"
            disabled={creatingTerminal || createTerminalDisabled}
            aria-label={`在 ${createLabel} 新建终端`}
            title={`在 ${createLabel} 新建终端`}
            onClick={(event) => {
              event.stopPropagation();
              onCreateTerminalAtGroup(node);
            }}
          >
            +
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
              locatingWindowId={locatingWindowId}
              registerWindowButton={registerWindowButton}
              deletingWindowId={deletingWindowId}
              summarizingProjectPath={summarizingProjectPath}
              hasUnreadNotification={hasUnreadNotification}
              onSelectWindow={onSelectWindow}
              onDeleteWindow={onDeleteWindow}
              onToggleGroup={onToggleGroup}
              onSummarizeProject={onSummarizeProject}
              onCreateTerminalAtGroup={onCreateTerminalAtGroup}
              creatingTerminal={creatingTerminal}
              createTerminalDisabled={createTerminalDisabled}
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
  locateSelectedWindowSignal = 0,
  deletingWindowId,
  hasUnreadNotification,
  onSelectWindow,
  onDeleteWindow,
  onCreateTerminalAtGroup,
  creatingTerminal,
  createTerminalDisabled
}: FolderTreeProps) {
  const queryClient = useQueryClient();
  const [summarizingProjectPath, setSummarizingProjectPath] = useState<string | null>(null);
  const [locatingWindowId, setLocatingWindowId] = useState<string | null>(null);
  const windowButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const locateClearTimeoutRef = useRef<number | null>(null);
  const handledLocateSignalRef = useRef(0);
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

  const registerWindowButton = useCallback((windowId: string, element: HTMLButtonElement | null) => {
    if (element === null) {
      windowButtonRefs.current.delete(windowId);
      return;
    }

    windowButtonRefs.current.set(windowId, element);
  }, []);

  useEffect(() => {
    setCollapsedState({ storageKey, keys: loadCollapsedKeys(storageKey, displayTree) });
  }, [displayTree, storageKey]);

  useEffect(() => {
    if (collapsedState.storageKey === storageKey) {
      writeCollapsedKeys(storageKey, collapsedState.keys);
    }
  }, [collapsedState, storageKey]);

  useEffect(() => {
    if (locateClearTimeoutRef.current !== null) {
      window.clearTimeout(locateClearTimeoutRef.current);
      locateClearTimeoutRef.current = null;
    }

    return () => {
      if (locateClearTimeoutRef.current !== null) {
        window.clearTimeout(locateClearTimeoutRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (locateSelectedWindowSignal === 0 || selectedWindowId === null) {
      return;
    }
    if (handledLocateSignalRef.current === locateSelectedWindowSignal) {
      return;
    }

    const frame = window.requestAnimationFrame(() => {
      const selectedButton = windowButtonRefs.current.get(selectedWindowId);
      if (!selectedButton) {
        return;
      }

      handledLocateSignalRef.current = locateSelectedWindowSignal;
      selectedButton.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
      setLocatingWindowId(selectedWindowId);

      if (locateClearTimeoutRef.current !== null) {
        window.clearTimeout(locateClearTimeoutRef.current);
      }
      locateClearTimeoutRef.current = window.setTimeout(() => {
        setLocatingWindowId((currentId) => (currentId === selectedWindowId ? null : currentId));
        locateClearTimeoutRef.current = null;
      }, 1200);
    });

    return () => window.cancelAnimationFrame(frame);
  }, [locateSelectedWindowSignal, selectedWindowId, selectedPathKeys]);

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
            locatingWindowId={locatingWindowId}
            registerWindowButton={registerWindowButton}
            deletingWindowId={deletingWindowId}
            summarizingProjectPath={summarizingProjectPath}
            hasUnreadNotification={hasUnreadNotification}
            onSelectWindow={onSelectWindow}
            onDeleteWindow={onDeleteWindow}
            onToggleGroup={toggleGroup}
            onSummarizeProject={
              terminalGroupingModeHasProjectRoot(groupingMode) && clientId !== null
                ? (projectPath) => summarizeMutation.mutate(projectPath)
                : undefined
            }
            onCreateTerminalAtGroup={onCreateTerminalAtGroup}
            creatingTerminal={creatingTerminal}
            createTerminalDisabled={createTerminalDisabled}
          />
        ))}
      </ul>
    </div>
  );
}
