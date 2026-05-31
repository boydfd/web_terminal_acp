import { describe, expect, it } from "vitest";

import {
  findNewUnreadNotifications,
  normalizeTerminalNotifications,
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

describe("normalizeTerminalNotifications", () => {
  it("maps backend notification fields to frontend view fields", () => {
    expect(normalizeTerminalNotifications([
      {
        id: "notification-1",
        client_id: "client-1",
        window_id: "window-1",
        window_title: "Terminal",
        completed_at: "2026-05-24T12:00:00Z",
        status: "ABORTED",
        read: false
      }
    ])).toEqual([
      {
        id: "notification-1",
        clientId: "client-1",
        windowId: "window-1",
        windowTitle: "Terminal",
        completedAt: "2026-05-24T12:00:00Z",
        status: "ABORTED",
        read: false
      }
    ]);
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
