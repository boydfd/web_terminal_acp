import { describe, expect, it, vi } from "vitest";

import { terminalRuntimeReadiness, waitForTerminalRuntime } from "../src/terminalCreateReadiness";
import type { VirtualWindow } from "../src/types";

function virtualWindow(overrides: Partial<VirtualWindow> = {}): VirtualWindow {
  return {
    id: "window-1",
    client_id: "client-1",
    title: "Terminal",
    folder_id: null,
    status: "ACTIVE",
    tmux_session: null,
    tmux_window_id: null,
    remote_session_id: null,
    remote_window_id: null,
    cwd: null,
    shell_command: null,
    summary: null,
    title_tags: null,
    runtime_tags: [],
    work_status: { state: "LONG_IDLE", label: "Idle", color: "gray" },
    title_manually_overridden: false,
    folder_manually_overridden: false,
    command_capture_supported: false,
    summary_job: null,
    created_at: "2026-05-31T00:00:00Z",
    last_terminal_command_at: null,
    last_agent_event_at: null,
    last_active_at: "2026-05-31T00:00:00Z",
    ...overrides
  };
}

describe("terminalCreateReadiness", () => {
  it("treats tmux and remote runtime ids as ready", () => {
    expect(terminalRuntimeReadiness(virtualWindow({
      tmux_session: "session",
      tmux_window_id: "1"
    }))).toBe("ready");
    expect(terminalRuntimeReadiness(virtualWindow({
      remote_session_id: "session",
      remote_window_id: "window"
    }))).toBe("ready");
  });

  it("treats terminal failures as failed while runtime ids are absent", () => {
    expect(terminalRuntimeReadiness(virtualWindow({ status: "ERROR" }))).toBe("failed");
    expect(terminalRuntimeReadiness(virtualWindow({ status: "DISCONNECTED" }))).toBe("failed");
  });

  it("polls until the runtime becomes ready", async () => {
    let now = 0;
    const readyWindow = virtualWindow({ remote_session_id: "session", remote_window_id: "window" });
    const fetchLatestWindow = vi.fn()
      .mockResolvedValueOnce(virtualWindow())
      .mockResolvedValueOnce(readyWindow);

    const result = await waitForTerminalRuntime(virtualWindow(), fetchLatestWindow, {
      pollMs: 5,
      timeoutMs: 100,
      sleep: async () => {
        now += 5;
      },
      now: () => now
    });

    expect(result).toBe(readyWindow);
    expect(fetchLatestWindow).toHaveBeenCalledTimes(2);
  });
});
