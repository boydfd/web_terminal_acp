import type { QueryClient, QueryKey } from "@tanstack/react-query";

export type UiInvalidateEvent = {
  type: "invalidate";
  seq: number;
  resources: string[];
  client_id: string | null;
  window_id: string | null;
  reason: string | null;
};

export type UiConnectedEvent = {
  type: "connected";
  seq: number;
};

export type UiTerminalSelectionEvent = {
  type: "terminal_selection";
  seq: number;
  client_id: string;
  window_id: string;
};

export type UiEvent = UiConnectedEvent | UiInvalidateEvent | UiTerminalSelectionEvent;

const UI_EVENT_RECONNECT_BASE_MS = 500;
const UI_EVENT_RECONNECT_MAX_MS = 10000;
const WINDOW_ACTIVITY_REFRESH_DELAY_MS = 1200;

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

export function parseUiEvent(rawValue: string): UiEvent | null {
  try {
    const value: unknown = JSON.parse(rawValue);
    if (!value || typeof value !== "object") {
      return null;
    }

    const candidate = value as Record<string, unknown>;
    const seq = typeof candidate.seq === "number" ? candidate.seq : 0;
    if (candidate.type === "connected") {
      return { type: "connected", seq };
    }
    if (candidate.type === "terminal_selection") {
      if (typeof candidate.client_id !== "string" || typeof candidate.window_id !== "string") {
        return null;
      }
      return {
        type: "terminal_selection",
        seq,
        client_id: candidate.client_id,
        window_id: candidate.window_id
      };
    }
    if (candidate.type !== "invalidate" || !isStringArray(candidate.resources)) {
      return null;
    }
    return {
      type: "invalidate",
      seq,
      resources: candidate.resources,
      client_id: typeof candidate.client_id === "string" ? candidate.client_id : null,
      window_id: typeof candidate.window_id === "string" ? candidate.window_id : null,
      reason: typeof candidate.reason === "string" ? candidate.reason : null
    };
  } catch {
    return null;
  }
}

export function queryKeysForUiInvalidation(event: UiInvalidateEvent): QueryKey[] {
  const keys: QueryKey[] = [];
  const clientId = event.client_id;
  const windowId = event.window_id;
  const resources = new Set(event.resources);

  if (resources.has("clients")) {
    keys.push(["clients"]);
  }
  if (clientId !== null && resources.has("tree")) {
    keys.push(["tree", clientId]);
  }
  if (clientId !== null && resources.has("window")) {
    keys.push(["window-activity", clientId]);
  }
  if (clientId !== null && windowId !== null && resources.has("window")) {
    keys.push(["window", clientId, windowId]);
  }
  if (clientId !== null && windowId !== null && resources.has("command_history")) {
    keys.push(["command-history", clientId, windowId]);
  }
  if (clientId !== null && windowId !== null && resources.has("agent_record")) {
    keys.push(["agent-record", "chat", clientId, windowId]);
    keys.push(["agent-record", "detail", clientId, windowId]);
  }
  if (clientId !== null && windowId !== null && resources.has("git_runs")) {
    keys.push(["git-runs", clientId, windowId]);
  }

  return keys;
}

export function applyUiInvalidation(queryClient: QueryClient, event: UiInvalidateEvent): void {
  for (const queryKey of queryKeysForUiInvalidation(event)) {
    void queryClient.invalidateQueries({ queryKey });
  }
}

export function scheduleWindowActivityRefresh(
  queryClient: QueryClient,
  event: UiInvalidateEvent,
  onComplete?: (timer: number) => void
): number | null {
  if (event.client_id === null || !event.resources.includes("window")) {
    return null;
  }

  const timer = window.setTimeout(() => {
    void queryClient.refetchQueries({
      queryKey: ["window-activity", event.client_id],
      type: "active"
    });
    onComplete?.(timer);
  }, WINDOW_ACTIVITY_REFRESH_DELAY_MS);
  return timer;
}

export function nextUiEventReconnectDelay(attempt: number): number {
  const exponentialDelay = UI_EVENT_RECONNECT_BASE_MS * (2 ** Math.max(0, attempt - 1));
  return Math.min(exponentialDelay, UI_EVENT_RECONNECT_MAX_MS);
}
