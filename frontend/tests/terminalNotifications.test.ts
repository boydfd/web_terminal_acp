import { afterEach, describe, expect, it } from "vitest";

import type { TreeFolder } from "../src/types";
import {
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
    read
  };
}

const CLIENT_ID = "client-1";
const WINDOW_ID = "window-1";
const COMPLETED_AT = "2026-05-24T12:00:00.000Z";

function treeWithCompletion(completedAt: string): TreeFolder[] {
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
          last_agent_task_completed_at: completedAt
        }
      ]
    }
  ];
}

afterEach(() => {
  window.localStorage.clear();
});

describe("markTerminalNotificationRead", () => {
  it("removes the notification from storage after it is opened", () => {
    syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT));
    const [stored] = loadStoredNotifications(CLIENT_ID);

    const next = markTerminalNotificationRead(CLIENT_ID, stored);

    expect(next).toEqual([]);
    expect(loadStoredNotifications(CLIENT_ID)).toEqual([]);
  });
});

describe("syncTerminalNotifications", () => {
  it("does not restore notifications that were already acknowledged", () => {
    syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT));
    const [stored] = loadStoredNotifications(CLIENT_ID);
    markTerminalNotificationRead(CLIENT_ID, stored);

    const next = syncTerminalNotifications(CLIENT_ID, treeWithCompletion(COMPLETED_AT));

    expect(next).toEqual([]);
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
