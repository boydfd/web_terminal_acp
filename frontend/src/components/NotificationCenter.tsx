import { useEffect, useRef } from "react";

import type { TerminalNotification } from "../terminalNotifications";

type NotificationCenterProps = {
  isOpen: boolean;
  notifications: TerminalNotification[];
  onClose: () => void;
  onSelectNotification: (notification: TerminalNotification) => void;
  onDeleteNotification: (notification: TerminalNotification) => void;
  onClearNotifications: () => void;
};

export function NotificationCenter({
  isOpen,
  notifications,
  onClose,
  onSelectNotification,
  onDeleteNotification,
  onClearNotifications
}: NotificationCenterProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const unreadCount = notifications.filter((notification) => !notification.read).length;

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node) || panelRef.current?.contains(target)) {
        return;
      }
      onClose();
    };

    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("pointerdown", handlePointerDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("pointerdown", handlePointerDown);
    };
  }, [isOpen, onClose]);

  if (!isOpen) {
    return null;
  }

  return (
    <div className="notification-center-backdrop" role="presentation">
      <div ref={panelRef} className="notification-center" role="dialog" aria-label="通知中心">
        <div className="notification-center-header">
          <div>
            <h2>通知中心</h2>
            <p className="muted">
              {unreadCount > 0 ? `${unreadCount} 条未读` : "暂无未读通知"}
            </p>
          </div>
          <div className="notification-center-actions">
            <button type="button" disabled={notifications.length === 0} onClick={onClearNotifications}>
              清空全部
            </button>
            <button type="button" onClick={onClose}>
              关闭
            </button>
          </div>
        </div>

        {notifications.length === 0 ? (
          <p className="notification-center-empty">
            Agent 任务完成后会在这里显示通知；可在设置中开启系统桌面通知。
          </p>
        ) : (
          <ul className="notification-center-list">
            {notifications.map((notification) => (
              <li
                key={notification.id}
                className={notification.read ? "notification-item read" : "notification-item unread"}
              >
                <button
                  type="button"
                  className="notification-item-main"
                  onClick={() => onSelectNotification(notification)}
                >
                  <span className="notification-item-title">{notification.windowTitle}</span>
                  <span className="notification-item-body">
                    {notification.status === "ABORTED" ? "Agent 可能已中断" : "Agent 任务已完成"}
                  </span>
                  <span className="notification-item-time">
                    {new Date(notification.completedAt).toLocaleString()}
                  </span>
                </button>
                <button
                  type="button"
                  className="notification-item-delete"
                  aria-label={`删除通知 ${notification.windowTitle}`}
                  title="删除通知"
                  onClick={() => onDeleteNotification(notification)}
                >
                  <TrashIcon />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export function NotificationBellButton({
  unreadCount,
  isOpen,
  onClick
}: {
  unreadCount: number;
  isOpen: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={isOpen ? "notification-bell active" : "notification-bell"}
      data-onboarding-id="notification-bell"
      aria-expanded={isOpen}
      aria-label={unreadCount > 0 ? `通知中心，${unreadCount} 条未读` : "通知中心"}
      onClick={onClick}
    >
      <NotificationBellIcon />
      {unreadCount > 0 && <span className="notification-bell-badge">{unreadCount}</span>}
    </button>
  );
}

export function TerminalUnreadDot({ visible }: { visible: boolean }) {
  if (!visible) {
    return null;
  }

  return <span className="terminal-unread-dot" aria-label="有新通知" />;
}

function NotificationBellIcon() {
  return (
    <svg
      className="notification-bell-icon"
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M18 8a6 6 0 0 0-12 0c0 6.2-2.2 7.3-3 8h18c-.8-.7-3-1.8-3-8Z" />
      <path d="M10 20a2.4 2.4 0 0 0 4 0" />
      <path className="notification-bell-shine" d="M19.4 3.6 21 2m.2 5h-2.1" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg
      className="notification-trash-icon"
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M4 7h16" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
      <path d="M6 7l1 13h10l1-13" />
      <path d="M9 7V4h6v3" />
    </svg>
  );
}
