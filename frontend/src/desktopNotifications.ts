import type { TerminalNotification } from "./terminalNotifications";

const DESKTOP_NOTIFICATIONS_KEY = "web-terminal-acp:desktop-notifications-enabled";

export function readDesktopNotificationsEnabled(): boolean {
  if (typeof window === "undefined") {
    return true;
  }

  const stored = window.localStorage.getItem(DESKTOP_NOTIFICATIONS_KEY);
  if (stored === null) {
    return true;
  }

  return stored === "1";
}

export function writeDesktopNotificationsEnabled(enabled: boolean): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(DESKTOP_NOTIFICATIONS_KEY, enabled ? "1" : "0");
}

export function desktopNotificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

export async function ensureDesktopNotificationPermission(): Promise<NotificationPermission> {
  if (!desktopNotificationsSupported()) {
    return "denied";
  }

  if (Notification.permission === "granted" || Notification.permission === "denied") {
    return Notification.permission;
  }

  return Notification.requestPermission();
}

export function showAgentTaskDesktopNotification(notification: TerminalNotification): void {
  if (!readDesktopNotificationsEnabled() || !desktopNotificationsSupported()) {
    return;
  }

  if (Notification.permission !== "granted") {
    return;
  }

  if (typeof document !== "undefined" && document.visibilityState === "visible") {
    const hasFocus = typeof document.hasFocus === "function" ? document.hasFocus() : true;
    if (hasFocus) {
      return;
    }
  }

  const body = notification.status === "ABORTED" ? "Agent 可能已中断" : "Agent 任务已完成";
  const desktopNotification = new Notification(notification.windowTitle, {
    body,
    tag: notification.id
  });
  desktopNotification.onclick = () => {
    window.focus();
    desktopNotification.close();
  };
}
