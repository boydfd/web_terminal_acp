import type { VirtualWindow } from "./types";

export type TerminalRuntimeReadiness = "ready" | "pending" | "failed";

const DEFAULT_TERMINAL_CREATE_READY_POLL_MS = 500;
const DEFAULT_TERMINAL_CREATE_READY_TIMEOUT_MS = 65000;

export function terminalRuntimeReadiness(window: Pick<
  VirtualWindow,
  "status" | "tmux_session" | "tmux_window_id" | "remote_session_id" | "remote_window_id"
>): TerminalRuntimeReadiness {
  if (
    (window.tmux_session !== null && window.tmux_window_id !== null) ||
    (window.remote_session_id !== null && window.remote_window_id !== null)
  ) {
    return "ready";
  }
  if (window.status === "ERROR" || window.status === "DISCONNECTED") {
    return "failed";
  }
  return "pending";
}

type WaitForTerminalRuntimeOptions = {
  pollMs?: number;
  timeoutMs?: number;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
};

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    globalThis.setTimeout(resolve, ms);
  });
}

export async function waitForTerminalRuntime(
  initialWindow: VirtualWindow,
  fetchLatestWindow: (clientId: string, windowId: string) => Promise<VirtualWindow>,
  options: WaitForTerminalRuntimeOptions = {}
): Promise<VirtualWindow> {
  const pollMs = options.pollMs ?? DEFAULT_TERMINAL_CREATE_READY_POLL_MS;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TERMINAL_CREATE_READY_TIMEOUT_MS;
  const sleep = options.sleep ?? delay;
  const now = options.now ?? (() => Date.now());
  let currentWindow = initialWindow;
  const deadline = now() + timeoutMs;

  while (terminalRuntimeReadiness(currentWindow) === "pending" && now() < deadline) {
    await sleep(pollMs);
    try {
      currentWindow = await fetchLatestWindow(currentWindow.client_id, currentWindow.id);
    } catch (error) {
      if (now() >= deadline) {
        throw error;
      }
    }
  }

  return currentWindow;
}
