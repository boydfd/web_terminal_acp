import { afterEach, describe, expect, it } from "vitest";

import type { TreeFolder } from "../src/types";
import {
  clearTerminalNotifications,
  deleteTerminalNotification,
  findNewUnreadNotifications,
  loadStoredNotifications,
  markTerminalNotificationRead,
  syncTerminalNotifications,
  type TerminalNotification
} from "../src/terminalNotifications";

function notification(id: string, read: boolean): TerminalNotification {
  return {
    id,
    clientId: "client-1",
    windowId: "window-1",
    windowTitle: "Terminal",
    completedAt: "2026-05-24T12:00:00.000Z",
    status: "FINISHED",
    read
  };
}

const CLIENT_ID = "client-1";
const WINDOW_ID = "window-1";
const COMPLETED_AT = "2026-05-24T12:00:00.000Z";
const COMPLETED_AT_WITHOUT_MILLIS = "2026-05-24T12:00:00Z";

function treeWithCompletion(completedAt: string): TreeFolder[] {
  return treeWithTaskStatus("FINISHED", completedAt);
}

function treeWithTaskStatus(status: "FINISHED" | "ABORTED", completedAt: string): TreeFolder[] {
  return [
    {
      id: "folder-1",
      name: "Folder",
      path: "/folder-1",
      folders: [],
      windows: [
        {
          id: WINDOW_ID,
          title: "Terminal",
          status: "OPEN",
          runtime_tags: [],
          work_status: { state: "RECENT_ACTIVE", label: "Recent", color: "green" },
          created_at: "2026-05-24T10:00:00.000Z",
          last_agent_task_completed_at: status === "FINISHED" ? completedAt : null,
          last_agent_task_status: status,
          last_agent_task_status_at: completedAt
        }
      ]
    }
  ];
}

afterEach(() => {
  window.localStorage.clear();
});

describe("markTerminalNotificationRead", () => {
  it("marks the notification read without deleting it after it is opened", () => {
    syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT));
    const [stored] = loadStoredNotifications(CLIENT_ID);

    const next = markTerminalNotificationRead(CLIENT_ID, stored);

    expect(next).toEqual([{ ...stored, read: true }]);
    expect(loadStoredNotifications(CLIENT_ID)).toEqual([{ ...stored, read: true }]);
  });
});

describe("deleteTerminalNotification", () => {
  it("removes the notification and keeps sync from restoring it", () => {
    syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT));
    const [stored] = loadStoredNotifications(CLIENT_ID);

    const next = deleteTerminalNotification(CLIENT_ID, stored);

    expect(next).toEqual([]);
    expect(syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT))).toEqual([]);
  });
});

describe("clearTerminalNotifications", () => {
  it("clears stored notifications and keeps sync from restoring them", () => {
    syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT));

    const next = clearTerminalNotifications(CLIENT_ID);

    expect(next).toEqual([]);
    expect(loadStoredNotifications(CLIENT_ID)).toEqual([]);
    expect(syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT))).toEqual([]);
  });

  it("keeps cleared notifications hidden when the same completion time changes ISO format", () => {
    syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT));

    const next = clearTerminalNotifications(CLIENT_ID);

    expect(next).toEqual([]);
    expect(syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT_WITHOUT_MILLIS))).toEqual([]);
  });
});

describe("syncTerminalNotifications", () => {
  it("preserves notifications that were already acknowledged as read", () => {
    syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT));
    const [stored] = loadStoredNotifications(CLIENT_ID);
    markTerminalNotificationRead(CLIENT_ID, stored);

    const next = syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT));

    expect(next).toEqual([{ ...stored, read: true }]);
  });

  it("creates abort notifications from agent task status", () => {
    const [stored] = syncTerminalNotifications(
      CLIENT_ID,
      treeWithTaskStatus("ABORTED", COMPLETED_AT)
    );

    expect(stored.status).toBe("ABORTED");
    expect(stored.completedAt).toBe(COMPLETED_AT);
    expect(stored.id).toBe(`${CLIENT_ID}:${WINDOW_ID}:ABORTED:${COMPLETED_AT}`);
  });
});

describe("findNewUnreadNotifications", () => {
  it("returns unread notifications that were not unread before", () => {
    const previous = [notification("a", true), notification("b", false)];
    const next = [notification("a", true), notification("b", false), notification("c", false)];

    expect(findNewUnreadNotifications(previous, next)).toEqual([notification("c", false)]);
  });

  it("returns nothing when unread set is unchanged", () => {
    const previous = [notification("a", false)];
    const next = [notification("a", false)];

    expect(findNewUnreadNotifications(previous, next)).toEqual([]);
  });
});
