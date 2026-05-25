import type { TreeFolder, TreeWindow } from "./types";

export type TerminalNotification = {
  id: string;
  clientId: string;
  windowId: string;
  windowTitle: string;
  completedAt: string;
  read: boolean;
};

const NOTIFICATIONS_STORAGE_KEY = "web-terminal-acp:terminal-notifications";
const ACK_STORAGE_PREFIX = "web-terminal-acp:terminal-notification-ack:";

function notificationStorageKey(clientId: string): string {
  return `${NOTIFICATIONS_STORAGE_KEY}:${clientId}`;
}

function ackStorageKey(clientId: string, windowId: string): string {
  return `${ACK_STORAGE_PREFIX}${clientId}:${windowId}`;
}

export function flattenTreeWindows(folders: TreeFolder[] | undefined): TreeWindow[] {
  if (!folders) {
    return [];
  }

  const windows: TreeWindow[] = [];
  const visit = (folder: TreeFolder) => {
    windows.push(...folder.windows);
    for (const child of folder.folders) {
      visit(child);
    }
  };

  for (const folder of folders) {
    visit(folder);
  }

  return windows;
}

export function readAcknowledgedCompletionAt(clientId: string, windowId: string): string | null {
  if (typeof window === "undefined") {
    return null;
  }

  return window.localStorage.getItem(ackStorageKey(clientId, windowId));
}

export function writeAcknowledgedCompletionAt(
  clientId: string,
  windowId: string,
  completedAt: string
): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(ackStorageKey(clientId, windowId), completedAt);
}

export function loadStoredNotifications(clientId: string): TerminalNotification[] {
  if (typeof window === "undefined") {
    return [];
  }

  try {
    const rawValue = window.localStorage.getItem(notificationStorageKey(clientId));
    if (rawValue === null) {
      return [];
    }

    const parsedValue: unknown = JSON.parse(rawValue);
    if (!Array.isArray(parsedValue)) {
      return [];
    }

    return parsedValue.filter(isTerminalNotification);
  } catch {
    return [];
  }
}

export function storeNotifications(clientId: string, notifications: TerminalNotification[]): void {
  if (typeof window === "undefined") {
    return;
  }

  try {
    window.localStorage.setItem(notificationStorageKey(clientId), JSON.stringify(notifications));
  } catch {
    return;
  }
}

export function syncTerminalNotifications(
  clientId: string,
  folders: TreeFolder[] | undefined
): TerminalNotification[] {
  const windows = flattenTreeWindows(folders);
  const existing = loadStoredNotifications(clientId);
  const existingById = new Map(existing.map((notification) => [notification.id, notification]));
  const next: TerminalNotification[] = [];

  for (const treeWindow of windows) {
    const completedAt = treeWindow.last_agent_task_completed_at;
    if (!completedAt) {
      continue;
    }

    const acknowledgedAt = readAcknowledgedCompletionAt(clientId, treeWindow.id);
    if (acknowledgedAt !== null && acknowledgedAt >= completedAt) {
      continue;
    }

    const id = `${clientId}:${treeWindow.id}:${completedAt}`;
    const previous = existingById.get(id);
    const read = previous?.read ?? false;

    next.push({
      id,
      clientId,
      windowId: treeWindow.id,
      windowTitle: treeWindow.title,
      completedAt,
      read: previous?.read ?? read
    });
  }

  next.sort((left, right) => right.completedAt.localeCompare(left.completedAt));
  storeNotifications(clientId, next);
  return next;
}

export function markTerminalNotificationRead(
  clientId: string,
  notification: TerminalNotification
): TerminalNotification[] {
  writeAcknowledgedCompletionAt(clientId, notification.windowId, notification.completedAt);
  const next = loadStoredNotifications(clientId).filter((item) => item.id !== notification.id);
  storeNotifications(clientId, next);
  return next;
}

export function markTerminalViewed(
  clientId: string,
  windowId: string,
  completedAt: string | null | undefined
): TerminalNotification[] {
  if (!completedAt) {
    return loadStoredNotifications(clientId);
  }

  writeAcknowledgedCompletionAt(clientId, windowId, completedAt);
  const next = loadStoredNotifications(clientId).filter(
    (item) => !(item.windowId === windowId && item.completedAt === completedAt)
  );
  storeNotifications(clientId, next);
  return next;
}

export function findNewUnreadNotifications(
  previous: TerminalNotification[],
  next: TerminalNotification[]
): TerminalNotification[] {
  const previousUnreadIds = new Set(
    previous.filter((notification) => !notification.read).map((notification) => notification.id)
  );
  return next.filter((notification) => !notification.read && !previousUnreadIds.has(notification.id));
}

export function hasUnreadTerminalNotification(
  clientId: string,
  windowId: string,
  completedAt: string | null | undefined
): boolean {
  if (!completedAt) {
    return false;
  }

  const acknowledgedAt = readAcknowledgedCompletionAt(clientId, windowId);
  return acknowledgedAt === null || acknowledgedAt < completedAt;
}

function isTerminalNotification(value: unknown): value is TerminalNotification {
  if (!value || typeof value !== "object") {
    return false;
  }

  const candidate = value as Partial<TerminalNotification>;
  return (
    typeof candidate.id === "string" &&
    typeof candidate.clientId === "string" &&
    typeof candidate.windowId === "string" &&
    typeof candidate.windowTitle === "string" &&
    typeof candidate.completedAt === "string" &&
    typeof candidate.read === "boolean"
  );
}
