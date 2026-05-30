import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WindowDetail } from "../src/components/WindowDetail";
import type { VirtualWindow } from "../src/types";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;
let queryClient: QueryClient | null = null;

const windowDetail: VirtualWindow = {
  id: "window-1",
  client_id: "client-1",
  title: "Codex terminal",
  folder_id: null,
  status: "ACTIVE",
  tmux_session: "test",
  tmux_window_id: "@1",
  remote_session_id: null,
  remote_window_id: null,
  cwd: "/workspace/project",
  shell_command: "/bin/bash",
  summary: null,
  title_tags: [],
  runtime_tags: ["codex", "/workspace/project"],
  work_status: {
    state: "RECENT_ACTIVE",
    label: "recent active",
    color: "green",
    last_activity_at: "2026-05-29T00:00:00Z",
    last_working_activity_at: "2026-05-29T00:00:00Z"
  },
  title_manually_overridden: false,
  folder_manually_overridden: false,
  command_capture_supported: true,
  summary_job: null,
  created_at: "2026-05-29T00:00:00Z",
  last_terminal_command_at: "2026-05-29T00:00:00Z",
  last_agent_event_at: "2026-05-29T00:00:00Z",
  last_active_at: "2026-05-29T00:00:00Z"
};

function renderWindowDetail() {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false }
    }
  });

  act(() => {
    root?.render(
      <QueryClientProvider client={queryClient as QueryClient}>
        <WindowDetail clientId="client-1" windowId="window-1" />
      </QueryClientProvider>
    );
  });
}

async function flushPromises() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function waitFor(assertion: () => void) {
  let lastError: unknown = null;
  for (let index = 0; index < 20; index += 1) {
    try {
      assertion();
      return;
    } catch (error) {
      lastError = error;
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
      });
    }
  }
  throw lastError;
}

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  root = null;
  container = null;
  queryClient?.clear();
  queryClient = null;
  vi.restoreAllMocks();
});

describe("Agent config viewer", () => {
  it("renders config sections under the Agent tab and toggles enablement", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/agent-config/skills/docker")) {
        expect(init?.method).toBe("PATCH");
        expect(init?.body).toBe(JSON.stringify({ enabled: false }));
        return new Response(JSON.stringify({
          agent: "codex",
          sections: [
            { id: "skills", name: "Skills", items: [{ id: "docker", name: "docker", enabled: false }] },
            { id: "plugins", name: "Plugins", items: [] },
            { id: "hooks", name: "Hooks", items: [] }
          ]
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.endsWith("/api/clients/client-1/windows/window-1/agent-config")) {
        return new Response(JSON.stringify({
          agent: "codex",
          sections: [
            { id: "skills", name: "Skills", items: [{ id: "docker", name: "docker", enabled: true }] },
            { id: "plugins", name: "Plugins", items: [{ id: "superpowers@openai-curated", name: "Superpowers", enabled: false }] },
            { id: "hooks", name: "Hooks", items: [] }
          ]
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.endsWith("/api/clients/client-1/windows/window-1")) {
        return new Response(JSON.stringify(windowDetail), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    globalThis.fetch = fetchMock as never;

    renderWindowDetail();
    await flushPromises();

    let agentTab: HTMLButtonElement | undefined;
    await waitFor(() => {
      agentTab = [...container!.querySelectorAll("button")].find((button) => button.textContent === "Agent");
      expect(agentTab).toBeTruthy();
    });
    act(() => {
      agentTab?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    let configTab: HTMLButtonElement | undefined;
    await waitFor(() => {
      configTab = [...container!.querySelectorAll("button")].find((button) => button.textContent === "Config");
      expect(configTab).toBeTruthy();
    });
    act(() => {
      configTab?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    await waitFor(() => {
      expect(container?.textContent).toContain("Skills");
    });
    expect(container?.textContent).toContain("docker");
    expect(container?.textContent).toContain("Plugins");
    expect(container?.textContent).toContain("Superpowers");
    expect(container?.textContent).toContain("Hooks");

    const dockerToggle = container!.querySelector('input[aria-label="Disable docker"]');
    expect(dockerToggle).toBeInstanceOf(HTMLInputElement);
    await act(async () => {
      dockerToggle?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/agent-config/skills/docker"),
      expect.objectContaining({ method: "PATCH" })
    );
  });

  it("shows title summary snapshots under the History title tab", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/title-history")) {
        return new Response(JSON.stringify({
          window_id: "window-1",
          items: [
            {
              id: "history-2",
              title: "Investigate build failure",
              summary: "The terminal moved from setup into build triage.",
              source: "summary",
              created_at: "2026-05-29T00:10:00Z"
            },
            {
              id: "history-1",
              title: "Codex terminal",
              summary: null,
              source: "initial",
              created_at: "2026-05-29T00:00:00Z"
            }
          ],
          total: 2,
          limit: 100,
          offset: 0,
          has_more: false
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url.endsWith("/api/clients/client-1/windows/window-1")) {
        return new Response(JSON.stringify(windowDetail), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    globalThis.fetch = fetchMock as never;

    renderWindowDetail();
    await flushPromises();

    let historyTab: HTMLButtonElement | undefined;
    await waitFor(() => {
      historyTab = [...container!.querySelectorAll("button")].find((button) => button.textContent === "History");
      expect(historyTab).toBeTruthy();
    });
    act(() => {
      historyTab?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    let titleTab: HTMLButtonElement | undefined;
    await waitFor(() => {
      titleTab = [...container!.querySelectorAll("button")].find((button) => button.textContent === "Title");
      expect(titleTab).toBeTruthy();
    });
    act(() => {
      titleTab?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    await waitFor(() => {
      expect(container?.textContent).toContain("Investigate build failure");
    });
    expect(container?.textContent).toContain("The terminal moved from setup into build triage.");
    expect(container?.textContent).toContain("Codex terminal");
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/title-history?"))).toBe(true);
  });
});
