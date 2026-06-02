import { describe, expect, it, vi } from "vitest";

import {
  applyUiInvalidation,
  nextUiEventReconnectDelay,
  parseUiEvent,
  reserveWindowActivityRefresh,
  queryKeysForUiInvalidation,
  scheduleWindowActivityRefresh
} from "../src/uiEvents";

const invalidateEvent = {
  type: "invalidate" as const,
  seq: 42,
  resources: ["window", "tree", "terminal_notifications", "agent_record", "command_history", "title_history", "git_runs"],
  client_id: "client-1",
  window_id: "window-1",
  reason: "window_updated"
};

describe("parseUiEvent", () => {
  it("parses backend invalidation payloads", () => {
    expect(parseUiEvent(JSON.stringify(invalidateEvent))).toEqual(invalidateEvent);
  });

  it("ignores malformed payloads", () => {
    expect(parseUiEvent("{")).toBeNull();
    expect(parseUiEvent(JSON.stringify({ type: "invalidate", resources: [1] }))).toBeNull();
  });
});

describe("queryKeysForUiInvalidation", () => {
  it("maps backend resources to the queries that drive terminal notifications", () => {
    expect(queryKeysForUiInvalidation(invalidateEvent)).toEqual([
      ["tree", "client-1"],
      ["terminal-projects", "client-1"],
      ["terminal-notifications", "client-1"],
      ["window-activity", "client-1"],
      ["window", "client-1", "window-1"],
      ["command-history", "client-1", "window-1"],
      ["title-history", "client-1", "window-1"],
      ["agent-record", "chat", "client-1", "window-1"],
      ["agent-record", "detail", "client-1", "window-1"],
      ["git-runs", "client-1", "window-1"]
    ]);
  });

  it("does not immediately refetch activity for activity-only window events", () => {
    expect(queryKeysForUiInvalidation({
      ...invalidateEvent,
      resources: ["window"],
      reason: "terminal_output"
    })).toEqual([
      ["window", "client-1", "window-1"]
    ]);
  });
});

describe("applyUiInvalidation", () => {
  it("invalidates every mapped query key", () => {
    const queryClient = {
      invalidateQueries: vi.fn()
    };

    applyUiInvalidation(queryClient as never, {
      ...invalidateEvent,
      resources: ["clients", "window"],
      window_id: null
    });

    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ["clients"], exact: true });
    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["window-activity", "client-1"],
      exact: false
    });
    expect(queryClient.invalidateQueries).toHaveBeenCalledTimes(2);
  });

  it("keeps activity-only invalidations from refetching active activity queries immediately", () => {
    const queryClient = {
      invalidateQueries: vi.fn()
    };

    applyUiInvalidation(queryClient as never, {
      ...invalidateEvent,
      resources: ["window"],
      reason: "ai_event"
    });

    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({
      queryKey: ["window", "client-1", "window-1"],
      exact: true
    });
    expect(queryClient.invalidateQueries).toHaveBeenCalledTimes(1);
  });
});

describe("scheduleWindowActivityRefresh", () => {
  it("refetches active activity queries after stale-cache refresh has time to complete", () => {
    vi.useFakeTimers();
    const queryClient = {
      refetchQueries: vi.fn()
    };
    const onComplete = vi.fn();

    const timer = scheduleWindowActivityRefresh(queryClient as never, invalidateEvent, onComplete);

    expect(timer).not.toBeNull();
    expect(queryClient.refetchQueries).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1200);
    expect(queryClient.refetchQueries).toHaveBeenCalledWith({
      queryKey: ["window-activity", "client-1"],
      exact: false,
      type: "active"
    });
    expect(onComplete).toHaveBeenCalledWith(timer);
    vi.useRealTimers();
  });
});

describe("reserveWindowActivityRefresh", () => {
  it("rate limits high-frequency activity invalidations by client", () => {
    const lastRefreshAtByClient = new Map<string, number>();

    expect(reserveWindowActivityRefresh(invalidateEvent, lastRefreshAtByClient, 10_000)).toBe(true);
    expect(reserveWindowActivityRefresh(invalidateEvent, lastRefreshAtByClient, 11_000)).toBe(false);
    expect(reserveWindowActivityRefresh(invalidateEvent, lastRefreshAtByClient, 13_000)).toBe(true);
  });

  it("ignores events that cannot affect window activity", () => {
    const lastRefreshAtByClient = new Map<string, number>();

    expect(reserveWindowActivityRefresh({
      ...invalidateEvent,
      resources: ["clients"],
    }, lastRefreshAtByClient, 10_000)).toBe(false);
    expect(lastRefreshAtByClient.size).toBe(0);
  });
});

describe("nextUiEventReconnectDelay", () => {
  it("uses capped exponential backoff", () => {
    expect(nextUiEventReconnectDelay(1)).toBe(500);
    expect(nextUiEventReconnectDelay(2)).toBe(1000);
    expect(nextUiEventReconnectDelay(10)).toBe(10000);
  });
});
