import { describe, expect, it, vi } from "vitest";

import {
  applyUiInvalidation,
  nextUiEventReconnectDelay,
  parseUiEvent,
  queryKeysForUiInvalidation,
  scheduleWindowActivityRefresh
} from "../src/uiEvents";

const invalidateEvent = {
  type: "invalidate" as const,
  seq: 42,
  resources: ["window", "tree", "agent_record", "command_history", "git_runs"],
  client_id: "client-1",
  window_id: "window-1",
  reason: "ai_event"
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
      ["window-activity", "client-1"],
      ["window", "client-1", "window-1"],
      ["command-history", "client-1", "window-1"],
      ["agent-record", "chat", "client-1", "window-1"],
      ["agent-record", "detail", "client-1", "window-1"],
      ["git-runs", "client-1", "window-1"]
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

    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ["clients"] });
    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ["window-activity", "client-1"] });
    expect(queryClient.invalidateQueries).toHaveBeenCalledTimes(2);
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
      type: "active"
    });
    expect(onComplete).toHaveBeenCalledWith(timer);
    vi.useRealTimers();
  });
});

describe("nextUiEventReconnectDelay", () => {
  it("uses capped exponential backoff", () => {
    expect(nextUiEventReconnectDelay(1)).toBe(500);
    expect(nextUiEventReconnectDelay(2)).toBe(1000);
    expect(nextUiEventReconnectDelay(10)).toBe(10000);
  });
});
