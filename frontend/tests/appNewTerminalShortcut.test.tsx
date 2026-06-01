import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../src/App";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("../src/components/TerminalPane", async () => {
  const React = await import("react");
  return {
    TerminalPane: React.forwardRef((_props, ref) => {
      React.useImperativeHandle(ref, () => ({
        focus: vi.fn(),
        openQuickInput: vi.fn(),
        refit: vi.fn(),
        submitQuickInput: vi.fn()
      }));
      return React.createElement("div", { "data-testid": "terminal-pane" });
    })
  };
});

let root: Root | null = null;
let container: HTMLDivElement | null = null;
let queryClient: QueryClient | null = null;
let fetchMock: ReturnType<typeof vi.fn>;

const createdWindow = {
  id: "window-2",
  client_id: "client-1",
  title: "window-2",
  folder_id: null,
  status: "ACTIVE",
  tmux_session: "session",
  tmux_window_id: "2",
  remote_session_id: null,
  remote_window_id: null,
  cwd: null,
  shell_command: null,
  summary: null,
  title_tags: [],
  runtime_tags: [],
  work_status: {
    state: "RECENT_ACTIVE",
    label: "recent active",
    color: "green"
  },
  title_manually_overridden: false,
  folder_manually_overridden: false,
  command_capture_supported: true,
  summary_job: null,
  created_at: "2026-05-31T00:00:00Z",
  last_terminal_command_at: null,
  last_agent_event_at: null,
  last_active_at: "2026-05-31T00:00:00Z"
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });
}

function pathFor(input: RequestInfo | URL): string {
  return new URL(input.toString()).pathname;
}

async function waitForRequests(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

async function waitForNewTerminalButton(): Promise<void> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await waitForRequests();
    const button = Array.from(container?.querySelectorAll("button") ?? []).find(
      (candidate) => candidate.textContent === "New terminal" && !candidate.disabled
    );
    if (button instanceof HTMLButtonElement) {
      return;
    }
  }

  throw new Error("New terminal button was not ready");
}

function renderApp(): void {
  container = document.createElement("div");
  document.body.appendChild(container);
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false }
    }
  });
  root = createRoot(container);
  act(() => {
    root?.render(
      <QueryClientProvider client={queryClient as QueryClient}>
        <App />
      </QueryClientProvider>
    );
  });
}

beforeEach(() => {
  fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const path = pathFor(input);

    if (path === "/api/auth/status") {
      return Promise.resolve(jsonResponse({ enabled: false }));
    }
    if (path === "/api/clients") {
      return Promise.resolve(jsonResponse([{
        id: "client-1",
        name: "local",
        status: "ONLINE",
        hostname: "localhost",
        install_path: null,
        version: null,
        last_update_at: null,
        runtime: "local",
        last_seen_at: null,
        connected_at: "2026-05-31T00:00:00Z",
        created_at: "2026-05-31T00:00:00Z",
        updated_at: "2026-05-31T00:00:00Z"
      }]));
    }
    if (path === "/api/clients/client-1/tree") {
      return Promise.resolve(jsonResponse([{
        id: "folder-1",
        name: "Root",
        path: "/workspace",
        folders: [],
        windows: []
      }]));
    }
    if (path === "/api/clients/client-1/windows/activity") {
      return Promise.resolve(jsonResponse({ windows: [] }));
    }
    if (path === "/api/clients/client-1/terminal-notifications") {
      return Promise.resolve(jsonResponse({ notifications: [] }));
    }
    if (path === "/api/clients/client-1/project-summaries") {
      return Promise.resolve(jsonResponse([]));
    }
    if (path === "/api/ui-settings/custom-quick-keys") {
      return Promise.resolve(jsonResponse({ quick_keys: [] }));
    }
    if (path === "/api/clients/client-1/windows" && init?.method === "POST") {
      return Promise.resolve(jsonResponse(createdWindow));
    }

    return Promise.resolve(jsonResponse({}));
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("WebSocket", class {
    close() {}
  });
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn()
  })));
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback: FrameRequestCallback) => {
    callback(0);
    return 1;
  });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  document.body.replaceChildren();
  queryClient?.clear();
  root = null;
  container = null;
  queryClient = null;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("App new terminal shortcut", () => {
  it("creates a shell terminal directly without opening the agent picker", async () => {
    renderApp();
    await waitForNewTerminalButton();

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "n",
        code: "KeyN",
        altKey: true
      }));
    });
    await waitForRequests();

    const createRequest = fetchMock.mock.calls.find(([input, init]) => (
      pathFor(input as RequestInfo | URL) === "/api/clients/client-1/windows"
      && init?.method === "POST"
    ));
    expect(createRequest).toBeDefined();
    expect(JSON.parse(createRequest?.[1]?.body as string)).toEqual({
      cwd: null,
      shell_command: null,
      folder_path: null,
      agent_launch: null
    });
    expect(container?.querySelector(".terminal-create-modal")).toBeNull();
  });
});
