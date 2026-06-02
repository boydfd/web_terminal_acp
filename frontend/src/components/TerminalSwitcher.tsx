import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchGlobalTerminalRecents, fetchProjectSummaries, fetchTerminalRecents, summarizeProject } from "../api";
import type { SummaryOutputLanguage } from "../userPreferences";
import { keyboardShortcutMatches, type KeyboardShortcut } from "../keyboardShortcuts";
import {
  buildTerminalSwitcherTree,
  canCreateWindowAtGroupNode,
  terminalGroupingModeHasProjectRoot,
  type SwitcherGroupNode,
  type SwitcherNode
} from "../terminalGrouping";
import type { TerminalGroupingMode } from "../userPreferences";
import type { GlobalTerminalRecent, ProjectSummary, TerminalRecent, TreeFolder, TreeWindow } from "../types";
import { TerminalUnreadDot } from "./NotificationCenter";
import { WorkStatusDot } from "./WorkStatusBadge";
import { useOverlayFocus } from "./useOverlayFocus";

const RECENTS_PAGE_SIZE = 20;

const AGENT_TAG_LABELS: Record<string, string> = {
  codex: "codex",
  claude_code: "claude code",
  cursor_cli: "cursor"
};

const TERMINAL_GROUPING_DESCRIPTIONS: Record<TerminalGroupingMode, string> = {
  "project-topic": "按项目 / 主题浏览 · 项目分组可点击「总结」生成名称",
  "topic": "按主题浏览",
  "time-topic": "按时间 / 主题 / 子主题浏览",
  "project-time-topic": "按项目 / 时间 / 主题 / 子主题浏览 · 项目分组可点击「总结」生成名称"
};

export type TerminalSwitcherMode = "recent" | "tree";
export type TerminalSwitcherRecentScope = "client" | "global";

type TerminalEntry = {
  window: TreeWindow;
  topicPath: string;
};

type TerminalMeta = {
  agentLabel: string;
  projectLabel: string | null;
  projectPath: string | null;
  timeValue: string;
  timeLabel: string;
  timeTitle: string;
};

type VisibleTreeItem = {
  node: SwitcherNode;
  parentKey: string | null;
};

type TerminalSwitcherProps = {
  clientId: string | null;
  folders: TreeFolder[] | undefined;
  selectedWindowId: string | null;
  mode: TerminalSwitcherMode;
  recentScope?: TerminalSwitcherRecentScope;
  terminalGroupingMode: TerminalGroupingMode;
  summaryOutputLanguage: SummaryOutputLanguage;
  isOpen: boolean;
  hasUnreadNotification?: (windowId: string) => boolean;
  onClose: () => void;
  onSelectWindow: (windowId: string, clientId?: string) => void;
  onToggleModeShortcut?: () => void;
  onCreateTerminalAtGroup?: (node: SwitcherGroupNode) => void;
  onConfigureTerminalAtGroup?: (node: SwitcherGroupNode) => void;
  creatingTerminal?: boolean;
  createTerminalDisabled?: boolean;
  switchShortcut?: KeyboardShortcut | null;
  switchShortcutLabel?: string;
};

function recentItemKey(windowId: string): string {
  return `recent:${windowId}`;
}

function scopedRecentItemKey(item: TerminalRecent | GlobalTerminalRecent, globalScope: boolean): string {
  if (globalScope) {
    return `recent:${(item as GlobalTerminalRecent).client_id}:${item.window_id}`;
  }
  return recentItemKey(item.window_id);
}

function isGlobalTerminalRecent(item: TerminalRecent | GlobalTerminalRecent): item is GlobalTerminalRecent {
  return "client_id" in item;
}

function collectGroupKeys(nodes: SwitcherNode[]): string[] {
  const keys: string[] = [];

  const visit = (node: SwitcherNode) => {
    if (node.type === "window") {
      return;
    }

    keys.push(node.key);
    for (const child of node.children) {
      visit(child);
    }
  };

  for (const node of nodes) {
    visit(node);
  }

  return keys;
}

function flattenVisibleTreeItems(nodes: SwitcherNode[], expandedKeys: Set<string>): VisibleTreeItem[] {
  const items: VisibleTreeItem[] = [];

  const visit = (node: SwitcherNode, parentKey: string | null) => {
    items.push({ node, parentKey });
    if (node.type === "window" || !expandedKeys.has(node.key)) {
      return;
    }

    for (const child of node.children) {
      visit(child, node.key);
    }
  };

  for (const node of nodes) {
    visit(node, null);
  }

  return items;
}

function findWindowInTree(folders: TreeFolder[], windowId: string): TreeWindow | null {
  for (const folder of folders) {
    const window = folder.windows.find((candidate) => candidate.id === windowId);
    if (window) {
      return window;
    }

    const childWindow = findWindowInTree(folder.folders, windowId);
    if (childWindow) {
      return childWindow;
    }
  }

  return null;
}

function agentLabelFromRuntimeTags(runtimeTags: string[] | null | undefined): string {
  for (const tag of runtimeTags ?? []) {
    const normalized = tag.trim().toLocaleLowerCase();
    const label = AGENT_TAG_LABELS[normalized];
    if (label) {
      return label;
    }
  }

  return "无";
}

function projectPathFromRuntimeTags(runtimeTags: string[] | null | undefined): string | null {
  for (const tag of runtimeTags ?? []) {
    const normalized = tag.trim();
    if (normalized.startsWith("/")) {
      return normalized;
    }
  }

  return null;
}

function projectFallbackLabel(projectPath: string): string | null {
  const segments = projectPath.split("/").filter(Boolean);
  return segments[segments.length - 1] ?? null;
}

function formatTerminalTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function formatTerminalTimeTitle(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString();
}

function terminalMeta(
  window: Pick<TreeWindow, "created_at" | "runtime_tags">,
  projectSummaryLookup: Map<string, ProjectSummary>
): TerminalMeta {
  const projectPath = projectPathFromRuntimeTags(window.runtime_tags);
  const summary = projectPath ? projectSummaryLookup.get(projectPath) : undefined;
  const displayName = summary?.display_name?.trim() || null;

  return {
    agentLabel: agentLabelFromRuntimeTags(window.runtime_tags),
    projectLabel: projectPath === null ? null : displayName ?? projectFallbackLabel(projectPath),
    projectPath,
    timeValue: window.created_at,
    timeLabel: formatTerminalTime(window.created_at),
    timeTitle: formatTerminalTimeTitle(window.created_at)
  };
}

function TerminalWindowMeta({ meta }: { meta: TerminalMeta }) {
  return (
    <span className="switcher-window-meta" aria-label="Terminal metadata">
      <span className="switcher-window-tag agent" title={`Agent: ${meta.agentLabel}`}>{meta.agentLabel}</span>
      <time className="switcher-window-meta-text" dateTime={meta.timeValue} title={`Created: ${meta.timeTitle}`}>
        {meta.timeLabel}
      </time>
      {meta.projectLabel !== null && (
        <span className="switcher-window-meta-text" title={meta.projectPath ?? meta.projectLabel}>
          {meta.projectLabel}
        </span>
      )}
    </span>
  );
}

function SwitcherTreeNode({
  node,
  activeKey,
  expandedKeys,
  selectedWindowId,
  projectSummaryLookup,
  summarizingProjectPath,
  hasUnreadNotification,
  onSelectEntry,
  onToggleGroup,
  onSummarizeProject,
  onCreateTerminalAtGroup,
  onConfigureTerminalAtGroup,
  creatingTerminal,
  createTerminalDisabled
}: {
  node: SwitcherNode;
  activeKey: string | null;
  expandedKeys: Set<string>;
  selectedWindowId: string | null;
  projectSummaryLookup: Map<string, ProjectSummary>;
  summarizingProjectPath: string | null;
  hasUnreadNotification?: (windowId: string) => boolean;
  onSelectEntry: (entry: TerminalEntry) => void;
  onToggleGroup: (key: string) => void;
  onSummarizeProject?: (projectPath: string) => void;
  onCreateTerminalAtGroup?: (node: SwitcherGroupNode) => void;
  onConfigureTerminalAtGroup?: (node: SwitcherGroupNode) => void;
  creatingTerminal?: boolean;
  createTerminalDisabled?: boolean;
}) {
  if (node.type === "window") {
    const isActive = node.key === activeKey;
    const isSelected = node.window.id === selectedWindowId;
    const showUnreadDot = hasUnreadNotification?.(node.window.id) ?? false;
    const meta = terminalMeta(node.window, projectSummaryLookup);

    return (
      <li>
        <button
          type="button"
          aria-current={isSelected ? "true" : undefined}
          aria-selected={isActive}
          className={isActive ? "switcher-window active" : "switcher-window"}
          onClick={() => onSelectEntry({ window: node.window, topicPath: node.topicPath })}
          role="treeitem"
          title={`${node.window.work_status.label}: ${node.window.title}`}
        >
          <WorkStatusDot status={node.window.work_status} />
          <span className="switcher-window-title">{node.window.title}</span>
          <TerminalWindowMeta meta={meta} />
          <TerminalUnreadDot visible={showUnreadDot} />
        </button>
      </li>
    );
  }

  const isExpanded = expandedKeys.has(node.key);
  const isActive = node.key === activeKey;
  const showSummarize = node.projectPath !== undefined && !node.topicPath && onSummarizeProject !== undefined;
  const isSummarizing = showSummarize && summarizingProjectPath === node.projectPath;
  const showCreateTerminal = onCreateTerminalAtGroup !== undefined && canCreateWindowAtGroupNode(node);
  const createLabel = node.projectPath ?? node.label;

  return (
    <li className="switcher-folder-node" role="none">
      <div className="switcher-folder-row">
        <button
          type="button"
          className={isActive ? "switcher-folder-button active" : "switcher-folder-button"}
          aria-selected={isActive}
          aria-expanded={isExpanded}
          onClick={() => onToggleGroup(node.key)}
          role="treeitem"
          title={node.projectPath ?? node.topicPath ?? node.label}
        >
          <span className="disclosure" aria-hidden="true">{isExpanded ? "▾" : "▸"}</span>
          <span>{node.label}</span>
          <span className="count">{node.count}</span>
        </button>
        {showSummarize && (
          <button
            type="button"
            className="switcher-summarize-button"
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
            className="switcher-create-terminal-button"
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
        {showCreateTerminal && onConfigureTerminalAtGroup && (
          <button
            type="button"
            className="switcher-configure-terminal-button"
            disabled={creatingTerminal || createTerminalDisabled}
            aria-label={`配置 ${createLabel} 新终端`}
            title={`配置 ${createLabel} 新终端`}
            onClick={(event) => {
              event.stopPropagation();
              onConfigureTerminalAtGroup(node);
            }}
          >
            配置
          </button>
        )}
      </div>
      {isExpanded && (
        <ul role="group">
          {node.children.map((child) => (
            <SwitcherTreeNode
              key={child.key}
              node={child}
              activeKey={activeKey}
              expandedKeys={expandedKeys}
              selectedWindowId={selectedWindowId}
              projectSummaryLookup={projectSummaryLookup}
              summarizingProjectPath={summarizingProjectPath}
              hasUnreadNotification={hasUnreadNotification}
              onSelectEntry={onSelectEntry}
              onToggleGroup={onToggleGroup}
              onSummarizeProject={onSummarizeProject}
              onCreateTerminalAtGroup={onCreateTerminalAtGroup}
              onConfigureTerminalAtGroup={onConfigureTerminalAtGroup}
              creatingTerminal={creatingTerminal}
              createTerminalDisabled={createTerminalDisabled}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

export function TerminalSwitcher({
  clientId,
  folders,
  selectedWindowId,
  mode,
  recentScope = "client",
  terminalGroupingMode,
  summaryOutputLanguage,
  isOpen,
  hasUnreadNotification,
  onClose,
  onSelectWindow,
  onToggleModeShortcut,
  onCreateTerminalAtGroup,
  onConfigureTerminalAtGroup,
  creatingTerminal,
  createTerminalDisabled,
  switchShortcut,
  switchShortcutLabel = "快捷键"
}: TerminalSwitcherProps) {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [recentPage, setRecentPage] = useState(1);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(() => new Set());
  const [summarizingProjectPath, setSummarizingProjectPath] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const projectSummariesQuery = useQuery({
    queryKey: ["project-summaries", clientId],
    queryFn: () => fetchProjectSummaries(clientId as string),
    enabled: isOpen && clientId !== null
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
  const entries = useMemo(
    () => buildTerminalSwitcherTree(folders ?? [], terminalGroupingMode, projectSummaryLookup, ""),
    [folders, projectSummaryLookup, terminalGroupingMode]
  );
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const isRecentMode = mode === "recent";
  const isGlobalRecentMode = isRecentMode && recentScope === "global";
  const recentsQuery = useQuery({
    queryKey: [
      "terminal-recents",
      isGlobalRecentMode ? "global" : clientId,
      isRecentMode ? { page: recentPage, query: normalizedQuery || null } : null
    ],
    queryFn: () => {
      if (isGlobalRecentMode) {
        return fetchGlobalTerminalRecents(recentPage, RECENTS_PAGE_SIZE, normalizedQuery);
      }
      return fetchTerminalRecents(clientId as string, recentPage, RECENTS_PAGE_SIZE, normalizedQuery);
    },
    enabled: isOpen && isRecentMode && (isGlobalRecentMode || clientId !== null),
    staleTime: 0
  });
  const treeNodes = useMemo(() => {
    if (clientId === null || isRecentMode) {
      return [];
    }

    return buildTerminalSwitcherTree(folders ?? [], terminalGroupingMode, projectSummaryLookup, query);
  }, [clientId, folders, isRecentMode, projectSummaryLookup, query, terminalGroupingMode]);
  const groupKeys = useMemo(() => collectGroupKeys(treeNodes), [treeNodes]);
  const visibleTreeItems = useMemo(() => flattenVisibleTreeItems(treeNodes, expandedKeys), [expandedKeys, treeNodes]);
  const recentItems: Array<TerminalRecent | GlobalTerminalRecent> = recentsQuery.data?.items ?? [];
  const recentTotalPages = recentsQuery.data?.total_pages ?? 0;
  const recentMatchCount = recentsQuery.data?.total ?? 0;
  const recentKeys = useMemo(
    () => recentItems.map((item) => scopedRecentItemKey(item, isGlobalRecentMode)),
    [isGlobalRecentMode, recentItems]
  );
  const navigableKeys = isRecentMode ? recentKeys : visibleTreeItems.map((item) => item.node.key);

  useEffect(() => {
    if (!isOpen) {
      setQuery("");
      setActiveKey(null);
      setRecentPage(1);
      setExpandedKeys(new Set());
      return;
    }
  }, [isOpen]);

  useEffect(() => {
    setQuery("");
    setActiveKey(null);
    setRecentPage(1);
    setExpandedKeys(new Set());
  }, [isOpen, mode, recentScope]);

  const handleEscape = useCallback(() => {
    onClose();
  }, [onClose]);

  useOverlayFocus({
    isOpen,
    ref: panelRef,
    onEscape: handleEscape,
    initialFocusSelector: "input"
  });

  useEffect(() => {
    setRecentPage(1);
  }, [normalizedQuery]);

  useEffect(() => {
    if (!isOpen || isRecentMode || normalizedQuery.length === 0) {
      return;
    }

    setExpandedKeys(new Set(groupKeys));
  }, [groupKeys, isOpen, isRecentMode, normalizedQuery]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    setActiveKey((currentKey) => {
      if (isRecentMode) {
        if (currentKey !== null && recentKeys.includes(currentKey)) {
          return currentKey;
        }

        const selectedKey = selectedWindowId === null
          ? null
          : isGlobalRecentMode && clientId !== null
            ? `recent:${clientId}:${selectedWindowId}`
            : recentItemKey(selectedWindowId);
        if (selectedKey !== null && recentKeys.includes(selectedKey)) {
          return selectedKey;
        }

        return recentKeys[0] ?? null;
      }

      if (currentKey !== null && visibleTreeItems.some((item) => item.node.key === currentKey)) {
        return currentKey;
      }

      const selectedItem = visibleTreeItems.find(
        (item) => item.node.type === "window" && item.node.window.id === selectedWindowId
      );
      return selectedItem?.node.key ?? null;
    });
  }, [clientId, isGlobalRecentMode, isOpen, isRecentMode, recentKeys, selectedWindowId, visibleTreeItems]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (keyboardShortcutMatches(event, switchShortcut ?? null)) {
        event.preventDefault();
        event.stopPropagation();
        onToggleModeShortcut?.();
        return;
      }

      if (navigableKeys.length === 0) {
        return;
      }

      const activeIndex = activeKey === null ? -1 : navigableKeys.indexOf(activeKey);
      const activeItem = activeIndex >= 0 ? visibleTreeItems[activeIndex] : null;

      if (event.key === "ArrowDown") {
        event.preventDefault();
        const nextIndex = activeIndex < 0 ? 0 : (activeIndex + 1) % navigableKeys.length;
        setActiveKey(navigableKeys[nextIndex]);
        return;
      }

      if (event.key === "ArrowUp") {
        event.preventDefault();
        const nextIndex = activeIndex < 0 ? navigableKeys.length - 1 : (activeIndex - 1 + navigableKeys.length) % navigableKeys.length;
        setActiveKey(navigableKeys[nextIndex]);
        return;
      }

      if (!isRecentMode && event.key === "ArrowRight" && activeItem?.node.type === "group") {
        event.preventDefault();
        if (!expandedKeys.has(activeItem.node.key)) {
          setExpandedKeys((currentKeys) => new Set(currentKeys).add(activeItem.node.key));
        } else if (activeIndex + 1 < visibleTreeItems.length) {
          setActiveKey(visibleTreeItems[activeIndex + 1].node.key);
        }
        return;
      }

      if (!isRecentMode && event.key === "ArrowLeft" && activeItem !== null) {
        event.preventDefault();
        if (activeItem.node.type === "group" && expandedKeys.has(activeItem.node.key)) {
          setExpandedKeys((currentKeys) => {
            const nextKeys = new Set(currentKeys);
            nextKeys.delete(activeItem.node.key);
            return nextKeys;
          });
          return;
        }

        if (activeItem.parentKey !== null) {
          setActiveKey(activeItem.parentKey);
        }
        return;
      }

      if (event.key === "Enter") {
        if (activeKey === null) {
          return;
        }

        event.preventDefault();
        if (isRecentMode) {
          const item = recentItems.find((candidate) => scopedRecentItemKey(candidate, isGlobalRecentMode) === activeKey);
          if (item === undefined) {
            return;
          }
          onSelectWindow(item.window_id, isGlobalTerminalRecent(item) ? item.client_id : undefined);
          onClose();
          return;
        }

        if (activeItem === null) {
          return;
        }

        if (activeItem.node.type === "group") {
          setExpandedKeys((currentKeys) => {
            const nextKeys = new Set(currentKeys);
            if (nextKeys.has(activeItem.node.key)) {
              nextKeys.delete(activeItem.node.key);
            } else {
              nextKeys.add(activeItem.node.key);
            }
            return nextKeys;
          });
        } else {
          onSelectWindow(activeItem.node.window.id);
          onClose();
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    activeKey,
    expandedKeys,
    isOpen,
    navigableKeys,
    onClose,
    onSelectWindow,
    onToggleModeShortcut,
    isRecentMode,
    isGlobalRecentMode,
    switchShortcut,
    recentItems,
    visibleTreeItems
  ]);

  if (!isOpen) {
    return null;
  }

  const selectEntry = (entry: TerminalEntry) => {
    onSelectWindow(entry.window.id);
    onClose();
  };
  const toggleGroup = (key: string) => {
    setActiveKey(key);
    setExpandedKeys((currentKeys) => {
      const nextKeys = new Set(currentKeys);
      if (nextKeys.has(key)) {
        nextKeys.delete(key);
      } else {
        nextKeys.add(key);
      }

      return nextKeys;
    });
  };
  const selectRecent = (item: TerminalRecent | GlobalTerminalRecent) => {
    onSelectWindow(item.window_id, isGlobalTerminalRecent(item) ? item.client_id : undefined);
    onClose();
  };

  return (
    <div
      className="terminal-switcher-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        aria-modal="true"
        className="terminal-switcher"
        data-onboarding-id="terminal-switcher"
        ref={panelRef}
        role="dialog"
      >
        <div className="terminal-switcher-header">
          <div>
            <h2>Switch terminal</h2>
            <p className="muted">
              {isRecentMode
                ? `${isGlobalRecentMode ? "跨 Client 最近使用的终端" : "最近使用的终端"} · ${switchShortcutLabel} 按项目/主题浏览`
                : `${TERMINAL_GROUPING_DESCRIPTIONS[terminalGroupingMode]} · ${switchShortcutLabel} 查看最近`}
            </p>
          </div>
          <button type="button" onClick={onClose}>
            Close
          </button>
        </div>

        <input
          aria-label="Search terminals"
          value={query}
          onChange={(event) => {
            const nextQuery = event.target.value;
            setQuery(nextQuery);
            if (nextQuery.trim().length === 0) {
              setExpandedKeys(new Set());
            }
          }}
          placeholder={isRecentMode ? "Search recent terminals..." : "Search terminals..."}
        />

        {clientId === null && !isGlobalRecentMode && <p className="terminal-switcher-empty">Select a client first.</p>}
        {clientId !== null && !isGlobalRecentMode && !isRecentMode && entries.length === 0 && (
          <p className="terminal-switcher-empty">No terminals in this client.</p>
        )}

        {(clientId !== null || isGlobalRecentMode) && isRecentMode && (
          <>
            {recentsQuery.isLoading && <p className="terminal-switcher-empty">Loading recent terminals...</p>}
            {recentsQuery.isError && (
              <p className="terminal-switcher-empty">Failed to load recent terminals.</p>
            )}
            {!recentsQuery.isLoading && !recentsQuery.isError && recentItems.length === 0 && (
              <p className="terminal-switcher-empty">
                {normalizedQuery ? "No matching recent terminals." : "No recently used terminals yet."}
              </p>
            )}
            {!recentsQuery.isLoading && !recentsQuery.isError && recentItems.length > 0 && (
              <ul className="terminal-switcher-results terminal-switcher-recents" role="listbox" aria-label="Recently used terminals">
                {recentItems.map((item) => {
                  const key = scopedRecentItemKey(item, isGlobalRecentMode);
                  const treeWindow = findWindowInTree(folders ?? [], item.window_id);
                  const isActive = key === activeKey;
                  const isSelected = item.window_id === selectedWindowId
                    && (!isGlobalTerminalRecent(item) || item.client_id === clientId);
                  const showUnreadDot = hasUnreadNotification?.(item.window_id) ?? false;
                  const meta = treeWindow ? terminalMeta(treeWindow, projectSummaryLookup) : null;
                  const clientName = isGlobalTerminalRecent(item) ? item.client_name : null;

                  return (
                    <li key={key}>
                      <button
                        type="button"
                        aria-selected={isActive}
                        aria-current={isSelected ? "true" : undefined}
                        className={isActive ? "switcher-window active" : "switcher-window"}
                        onClick={() => selectRecent(item)}
                        role="option"
                        title={item.title}
                      >
                        {treeWindow ? <WorkStatusDot status={treeWindow.work_status} /> : <span className="switcher-window-placeholder" aria-hidden="true" />}
                        <span className="switcher-window-title">{item.title}</span>
                        {clientName !== null && <span className="switcher-client-name">{clientName}</span>}
                        {meta !== null && <TerminalWindowMeta meta={meta} />}
                        <TerminalUnreadDot visible={showUnreadDot} />
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
            {!recentsQuery.isLoading && !recentsQuery.isError && recentTotalPages > 1 && (
              <div className="terminal-switcher-pagination">
                <button
                  type="button"
                  disabled={recentPage <= 1}
                  onClick={() => setRecentPage((page) => Math.max(1, page - 1))}
                >
                  Previous
                </button>
                <span>
                  Page {recentPage} / {recentTotalPages}
                  {normalizedQuery ? ` (${recentMatchCount} matches)` : ""}
                </span>
                <button
                  type="button"
                  disabled={recentPage >= recentTotalPages}
                  onClick={() => setRecentPage((page) => page + 1)}
                >
                  Next
                </button>
              </div>
            )}
          </>
        )}

        {clientId !== null && !isRecentMode && entries.length > 0 && treeNodes.length === 0 && (
          <p className="terminal-switcher-empty">No matching terminals.</p>
        )}

        {clientId !== null && !isRecentMode && entries.length > 0 && treeNodes.length > 0 && (
          <ul className="terminal-switcher-results switcher-topic-tree" role="tree">
            {treeNodes.map((node) => (
              <SwitcherTreeNode
                key={node.key}
                node={node}
                activeKey={activeKey}
                expandedKeys={expandedKeys}
                selectedWindowId={selectedWindowId}
                projectSummaryLookup={projectSummaryLookup}
                summarizingProjectPath={summarizingProjectPath}
                hasUnreadNotification={hasUnreadNotification}
                onSelectEntry={selectEntry}
                onToggleGroup={toggleGroup}
                onSummarizeProject={
                  terminalGroupingModeHasProjectRoot(terminalGroupingMode) && clientId !== null
                    ? (projectPath) => summarizeMutation.mutate(projectPath)
                    : undefined
                }
                onCreateTerminalAtGroup={onCreateTerminalAtGroup}
                onConfigureTerminalAtGroup={onConfigureTerminalAtGroup}
                creatingTerminal={creatingTerminal}
                createTerminalDisabled={createTerminalDisabled}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
