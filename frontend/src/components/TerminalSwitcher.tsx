import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import { fetchProjectSummaries, fetchTerminalRecents, summarizeProject } from "../api";
import type { SummaryOutputLanguage } from "../userPreferences";
import { buildTerminalSwitcherTree, type SwitcherNode } from "../terminalGrouping";
import type { TerminalGroupingMode } from "../userPreferences";
import type { ProjectSummary, TerminalRecent, TreeFolder, TreeWindow } from "../types";
import { GitPendingBadge } from "./GitPendingBadge";
import { TerminalUnreadDot } from "./NotificationCenter";
import { WorkStatusDot } from "./WorkStatusBadge";

const RECENTS_PAGE_SIZE = 20;

export type TerminalSwitcherMode = "recent" | "tree";

type TerminalEntry = {
  window: TreeWindow;
  topicPath: string;
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
  terminalGroupingMode: TerminalGroupingMode;
  summaryOutputLanguage: SummaryOutputLanguage;
  isOpen: boolean;
  hasUnreadNotification?: (windowId: string) => boolean;
  onClose: () => void;
  onSelectWindow: (windowId: string) => void;
};

function isTerminalSwitcherShortcut(event: KeyboardEvent): boolean {
  const key = event.key.toLocaleLowerCase();
  return event.altKey && !event.ctrlKey && !event.metaKey && (event.code === "KeyW" || key === "w" || event.keyCode === 87);
}

function recentItemKey(windowId: string): string {
  return `recent:${windowId}`;
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

function SwitcherTreeNode({
  node,
  activeKey,
  expandedKeys,
  selectedWindowId,
  summarizingProjectPath,
  hasUnreadNotification,
  onSelectEntry,
  onToggleGroup,
  onSummarizeProject
}: {
  node: SwitcherNode;
  activeKey: string | null;
  expandedKeys: Set<string>;
  selectedWindowId: string | null;
  summarizingProjectPath: string | null;
  hasUnreadNotification?: (windowId: string) => boolean;
  onSelectEntry: (entry: TerminalEntry) => void;
  onToggleGroup: (key: string) => void;
  onSummarizeProject?: (projectPath: string) => void;
}) {
  if (node.type === "window") {
    const isActive = node.key === activeKey;
    const isSelected = node.window.id === selectedWindowId;
    const showUnreadDot = hasUnreadNotification?.(node.window.id) ?? false;

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
          <TerminalUnreadDot visible={showUnreadDot} />
        </button>
      </li>
    );
  }

  const isExpanded = expandedKeys.has(node.key);
  const isActive = node.key === activeKey;
  const showSummarize = node.projectPath !== undefined && onSummarizeProject !== undefined;
  const isSummarizing = showSummarize && summarizingProjectPath === node.projectPath;

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
          title={node.projectPath ?? node.label}
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
              summarizingProjectPath={summarizingProjectPath}
              hasUnreadNotification={hasUnreadNotification}
              onSelectEntry={onSelectEntry}
              onToggleGroup={onToggleGroup}
              onSummarizeProject={onSummarizeProject}
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
  terminalGroupingMode,
  summaryOutputLanguage,
  isOpen,
  hasUnreadNotification,
  onClose,
  onSelectWindow
}: TerminalSwitcherProps) {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [recentPage, setRecentPage] = useState(1);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(() => new Set());
  const [summarizingProjectPath, setSummarizingProjectPath] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const projectSummariesQuery = useQuery({
    queryKey: ["project-summaries", clientId],
    queryFn: () => fetchProjectSummaries(clientId as string),
    enabled: isOpen && clientId !== null && mode === "tree"
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
  const recentsQuery = useQuery({
    queryKey: [
      "terminal-recents",
      clientId,
      isRecentMode ? { page: recentPage, query: normalizedQuery || null } : null
    ],
    queryFn: () => fetchTerminalRecents(clientId as string, recentPage, RECENTS_PAGE_SIZE, normalizedQuery),
    enabled: isOpen && clientId !== null && isRecentMode,
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
  const recentItems = recentsQuery.data?.items ?? [];
  const recentTotalPages = recentsQuery.data?.total_pages ?? 0;
  const recentMatchCount = recentsQuery.data?.total ?? 0;
  const recentKeys = useMemo(
    () => recentItems.map((item) => recentItemKey(item.window_id)),
    [recentItems]
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

    requestAnimationFrame(() => inputRef.current?.focus());
  }, [isOpen]);

  useEffect(() => {
    setQuery("");
    setActiveKey(null);
    setRecentPage(1);
    setExpandedKeys(new Set());
    if (isOpen) {
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [isOpen, mode]);

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

        const selectedKey = selectedWindowId === null ? null : recentItemKey(selectedWindowId);
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
  }, [isOpen, isRecentMode, recentKeys, selectedWindowId, visibleTreeItems]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (isTerminalSwitcherShortcut(event)) {
        return;
      }

      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
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
          const windowId = activeKey.slice("recent:".length);
          onSelectWindow(windowId);
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
    isRecentMode,
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
  const selectRecent = (item: TerminalRecent) => {
    onSelectWindow(item.window_id);
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
        role="dialog"
      >
        <div className="terminal-switcher-header">
          <div>
            <h2>Switch terminal</h2>
            <p className="muted">
              {isRecentMode
                ? "最近使用的终端 · Alt+W 按项目/主题浏览"
                : terminalGroupingMode === "project-topic"
                  ? "按项目 / 主题浏览 · 项目分组可点击「总结」生成名称 · Alt+W 查看最近"
                  : "按主题浏览 · Alt+W 查看最近使用的终端"}
            </p>
          </div>
          <button type="button" onClick={onClose}>
            Close
          </button>
        </div>

        <input
          ref={inputRef}
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

        {clientId === null && <p className="terminal-switcher-empty">Select a client first.</p>}
        {clientId !== null && entries.length === 0 && (
          <p className="terminal-switcher-empty">No terminals in this client.</p>
        )}

        {clientId !== null && isRecentMode && (
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
                  const key = recentItemKey(item.window_id);
                  const treeWindow = findWindowInTree(folders ?? [], item.window_id);
                  const isActive = key === activeKey;
                  const isSelected = item.window_id === selectedWindowId;
                  const showUnreadDot = hasUnreadNotification?.(item.window_id) ?? false;

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
                summarizingProjectPath={summarizingProjectPath}
                hasUnreadNotification={hasUnreadNotification}
                onSelectEntry={selectEntry}
                onToggleGroup={toggleGroup}
                onSummarizeProject={
                  terminalGroupingMode === "project-topic" && clientId !== null
                    ? (projectPath) => summarizeMutation.mutate(projectPath)
                    : undefined
                }
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
