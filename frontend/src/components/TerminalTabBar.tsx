import type { TreeWindow } from "../types";
import { GitPendingBadge } from "./GitPendingBadge";
import { TerminalUnreadDot } from "./NotificationCenter";
import { WorkStatusDot } from "./WorkStatusBadge";

type TerminalTabBarProps = {
  windows: TreeWindow[];
  selectedWindowId: string | null;
  deletingWindowId?: string | null;
  hasUnreadNotification?: (windowId: string) => boolean;
  onSelectWindow: (windowId: string) => void;
  onDeleteWindow: (window: TreeWindow) => void;
};

export function TerminalTabBar({
  windows,
  selectedWindowId,
  deletingWindowId = null,
  hasUnreadNotification,
  onSelectWindow,
  onDeleteWindow
}: TerminalTabBarProps) {
  if (windows.length === 0) {
    return <p className="terminal-tab-empty muted">No terminal selected</p>;
  }

  return (
    <div className="terminal-tab-list" role="tablist" aria-label="Open terminals">
      {windows.map((treeWindow) => {
        const isSelected = treeWindow.id === selectedWindowId;
        const isDeleting = deletingWindowId === treeWindow.id;
        const showUnreadDot = hasUnreadNotification?.(treeWindow.id) ?? false;

        return (
          <div
            key={treeWindow.id}
            className={isSelected ? "terminal-tab selected" : "terminal-tab"}
            role="presentation"
          >
            <button
              type="button"
              role="tab"
              aria-selected={isSelected}
              className="terminal-tab-select"
              title={`${treeWindow.work_status.label}: ${treeWindow.title}`}
              onClick={() => onSelectWindow(treeWindow.id)}
            >
              <WorkStatusDot status={treeWindow.work_status} />
              <GitPendingBadge visible={treeWindow.git_worktree?.pending_commit === true} />
              <span className="terminal-tab-title">{treeWindow.title}</span>
              <TerminalUnreadDot visible={showUnreadDot} />
            </button>
            <button
              type="button"
              className="terminal-tab-delete"
              aria-label={`Delete ${treeWindow.title}`}
              disabled={isDeleting}
              onClick={(event) => {
                event.stopPropagation();
                onDeleteWindow(treeWindow);
              }}
            >
              ×
            </button>
          </div>
        );
      })}
    </div>
  );
}
