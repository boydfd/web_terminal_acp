import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../src/App";
import { CODEX_COMPOSER_SUBMIT_INPUT } from "../src/terminalQuickKeys";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const submitQuickInputMock = vi.fn();
const terminalPaneUnmounts = new Map<string, number>();

vi.mock("../src/components/TerminalPane", async () => {
  const React = await import("react");
  return {
    TerminalPane: React.forwardRef((props: { selectionEnabled?: boolean; windowId?: string | null }, ref) => {
      const testId = props.selectionEnabled === false ? "aux-terminal-pane" : "terminal-pane";
      React.useImperativeHandle(ref, () => ({
        focus: vi.fn(),
        openQuickInput: vi.fn(),
        refit: vi.fn(),
        setQuickInputDraft: vi.fn(),
        submitQuickInput: submitQuickInputMock
      }));
      React.useEffect(() => {
        return () => {
          terminalPaneUnmounts.set(testId, (terminalPaneUnmounts.get(testId) ?? 0) + 1);
        };
      }, [testId]);
      return React.createElement("div", {
        "data-testid": testId,
        "data-window-id": props.windowId ?? ""
      });
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

const treeWindow = {
  id: "window-1",
  title: "Codex window",
  status: "ACTIVE",
  title_tags: [],
  created_at: "2026-05-31T00:00:00Z"
};

const codexWindowActivity = {
  window_id: "window-1",
  work_status: {
    state: "RECENT_ACTIVE",
    label: "recent active",
    color: "green"
  },
  runtime_tags: ["codex", "/workspace"],
  last_agent_task_completed_at: null,
  last_agent_task_status: null,
  last_agent_task_status_at: null,
  git_worktree: null
};

const codexWindowDetail = {
  id: "window-1",
  client_id: "client-1",
  title: "Codex window",
  folder_id: "folder-1",
  status: "ACTIVE",
  tmux_session: "session",
  tmux_window_id: "1",
  remote_session_id: null,
  remote_window_id: null,
  cwd: "/workspace",
  shell_command: "codex",
  summary: null,
  title_tags: [],
  runtime_tags: ["codex", "/workspace"],
  work_status: codexWindowActivity.work_status,
  title_manually_overridden: false,
  folder_manually_overridden: false,
  command_capture_supported: true,
  summary_job: null,
  created_at: "2026-05-31T00:00:00Z",
  last_terminal_command_at: null,
  last_agent_event_at: null,
  last_active_at: "2026-05-31T00:00:00Z"
};

const otherTreeWindow = {
  id: "window-3",
  title: "Other window",
  status: "ACTIVE",
  title_tags: [],
  created_at: "2026-05-31T00:00:00Z"
};

const otherWindowActivity = {
  window_id: "window-3",
  work_status: {
    state: "RECENT_ACTIVE",
    label: "recent active",
    color: "green"
  },
  runtime_tags: ["claude_code", "/other"],
  last_agent_task_completed_at: null,
  last_agent_task_status: null,
  last_agent_task_status_at: null,
  git_worktree: null
};

const otherWindowDetail = {
  ...codexWindowDetail,
  id: "window-3",
  title: "Other window",
  folder_id: "folder-2",
  cwd: "/other",
  shell_command: "claude",
  runtime_tags: ["claude_code", "/other"],
  work_status: otherWindowActivity.work_status
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

function searchParamFor(input: RequestInfo | URL, name: string): string | null {
  return new URL(input.toString()).searchParams.get(name);
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

async function waitForElement<T extends Element>(
  selector: string,
  guard: (element: Element) => element is T
): Promise<T> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await waitForRequests();
    const element = container?.querySelector(selector);
    if (element !== undefined && element !== null && guard(element)) {
      return element;
    }
  }

  throw new Error(`Element ${selector} was not ready`);
}

async function waitForButtonText(text: string, enabled = false): Promise<HTMLButtonElement> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await waitForRequests();
    const button = Array.from(container?.querySelectorAll("button") ?? []).find(
      (candidate) => candidate.textContent === text && (!enabled || !candidate.disabled)
    );
    if (button instanceof HTMLButtonElement) {
      return button;
    }
  }

  throw new Error(`Button ${text} was not ready`);
}

async function waitForProjectCard(projectPath: string): Promise<HTMLButtonElement> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await waitForRequests();
    const button = container?.querySelector(`button.terminal-project-card[title="${projectPath}"]`);
    if (button instanceof HTMLButtonElement) {
      return button;
    }
  }

  throw new Error(`Project card ${projectPath} was not ready`);
}

async function waitForSelectedProject(projectPath: string): Promise<void> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const button = await waitForProjectCard(projectPath);
    if (button.getAttribute("aria-current") === "true") {
      return;
    }
  }

  throw new Error(`Project ${projectPath} was not selected`);
}

async function waitForTerminalPaneWindow(windowId: string): Promise<void> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await waitForRequests();
    const pane = container?.querySelector('[data-testid="terminal-pane"]');
    if (pane?.getAttribute("data-window-id") === windowId) {
      return;
    }
  }

  throw new Error(`Terminal pane did not switch to ${windowId}`);
}

async function waitForSwitcherWindowButton(text: string): Promise<HTMLButtonElement> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await waitForRequests();
    const button = Array.from(container?.querySelectorAll(".terminal-switcher .switcher-window") ?? []).find(
      (candidate) => candidate.textContent?.includes(text)
    );
    if (button instanceof HTMLButtonElement && !button.disabled) {
      return button;
    }
  }

  throw new Error(`Switcher window ${text} was not ready`);
}

async function waitForSwitcherWindowButtonText(
  text: string,
  expectedText: string
): Promise<HTMLButtonElement> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const button = await waitForSwitcherWindowButton(text);
    if (button.textContent?.includes(expectedText)) {
      return button;
    }
  }

  throw new Error(`Switcher window ${text} did not include ${expectedText}`);
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
  window.history.replaceState(null, "", "/clients/client-1/terminals/window-1");
  submitQuickInputMock.mockReset();
  submitQuickInputMock.mockReturnValue(true);
  terminalPaneUnmounts.clear();
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
    if (path === "/api/clients/client-1/terminal-projects" && searchParamFor(input, "range") === "7d") {
      return Promise.resolve(jsonResponse([
        { project_path: "/workspace", window_count: 1 },
        { project_path: "/other", window_count: 1 }
      ]));
    }
    if (path === "/api/clients/client-1/tree" && searchParamFor(input, "range") === "7d") {
      const projectPath = searchParamFor(input, "project_path");
      if (projectPath === "/workspace") {
        return Promise.resolve(jsonResponse([{
          id: "folder-1",
          name: "Root",
          path: "/workspace",
          folders: [],
          windows: [treeWindow]
        }]));
      }
      if (projectPath === "/other") {
        return Promise.resolve(jsonResponse([{
          id: "folder-2",
          name: "Other",
          path: "/other",
          folders: [],
          windows: [otherTreeWindow]
        }]));
      }
      return Promise.resolve(jsonResponse([
        {
          id: "folder-1",
          name: "Root",
          path: "/workspace",
          folders: [],
          windows: [treeWindow]
        },
        {
          id: "folder-2",
          name: "Other",
          path: "/other",
          folders: [],
          windows: [otherTreeWindow]
        }
      ]));
    }
    if (
      path === "/api/clients/client-1/windows/activity"
      && searchParamFor(input, "range") === "7d"
      && searchParamFor(input, "include_runtime_tags") === "true"
    ) {
      if (searchParamFor(input, "project_path") === "/other") {
        return Promise.resolve(jsonResponse({ windows: [otherWindowActivity] }));
      }
      if (searchParamFor(input, "project_path") === null) {
        return Promise.resolve(jsonResponse({ windows: [codexWindowActivity, otherWindowActivity] }));
      }
      return Promise.resolve(jsonResponse({ windows: [codexWindowActivity] }));
    }
    if (path === "/api/clients/client-1/terminal-notifications") {
      return Promise.resolve(jsonResponse({ notifications: [] }));
    }
    if (path === "/api/clients/client-1/project-summaries") {
      return Promise.resolve(jsonResponse([]));
    }
    if (path === "/api/clients/client-1/terminal-recents") {
      return Promise.resolve(jsonResponse({
        items: [
          { window_id: "window-3", title: "Other window", last_used_at: "2026-05-31T00:00:00Z" },
          { window_id: "window-1", title: "Codex window", last_used_at: "2026-05-31T00:00:00Z" }
        ],
        page: 1,
        page_size: 20,
        total: 2,
        total_pages: 1,
        has_next: false,
        has_previous: false
      }));
    }
    if (path === "/api/ui-settings/custom-quick-keys") {
      return Promise.resolve(jsonResponse({ quick_keys: [] }));
    }
    if (path === "/api/clients/client-1/windows" && init?.method === "POST") {
      return Promise.resolve(jsonResponse(createdWindow));
    }
    if (path === "/api/clients/client-1/windows/window-1") {
      return Promise.resolve(jsonResponse(codexWindowDetail));
    }
    if (path === "/api/clients/client-1/windows/window-3") {
      return Promise.resolve(jsonResponse(otherWindowDetail));
    }
    if (path === "/api/clients/client-1/windows/window-1/aux-terminal/ensure" && init?.method === "POST") {
      return Promise.resolve(jsonResponse({ status: "ready", cwd: "/workspace" }));
    }
    if (path === "/api/clients/client-1/windows/window-1/agent-record/chat") {
      return Promise.resolve(jsonResponse({
        window_id: "window-1",
        messages: [],
        messages_total: 0,
        messages_limit: 30,
        messages_offset: 0,
        messages_has_more: false
      }));
    }
    if (path === "/api/clients/client-1/windows/window-1/agent-record/detail") {
      return Promise.resolve(jsonResponse({
        window_id: "window-1",
        sessions: [],
        events: [],
        events_total: 0,
        events_limit: 100,
        events_offset: 0,
        events_has_more: false
      }));
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

  it("submits Agent Record quick input to Codex with enhanced Enter", async () => {
    renderApp();
    const agentTab = await waitForButtonText("Agent");

    act(() => {
      agentTab.click();
    });
    const expandButton = await waitForButtonText("Expand", true);
    act(() => {
      expandButton.click();
    });

    const textarea = await waitForElement(
      ".agent-record-quick-input textarea",
      (element): element is HTMLTextAreaElement => element instanceof HTMLTextAreaElement
    );
    const descriptor = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
    act(() => {
      descriptor?.set?.call(textarea, "fix this");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
      textarea.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Enter" }));
    });

    expect(submitQuickInputMock).toHaveBeenCalledWith(`fix this${CODEX_COMPOSER_SUBMIT_INPUT}`);
  });

  it("opens an aux terminal drawer from the default shortcut", async () => {
    renderApp();
    await waitForNewTerminalButton();

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "A",
        code: "KeyA",
        altKey: true,
        shiftKey: true
      }));
    });
    await waitForRequests();

    const ensureRequest = fetchMock.mock.calls.find(([input, init]) => (
      pathFor(input as RequestInfo | URL) === "/api/clients/client-1/windows/window-1/aux-terminal/ensure"
      && init?.method === "POST"
    ));
    expect(ensureRequest).toBeDefined();
    expect(container?.querySelector(".aux-terminal-drawer")).not.toBeNull();
    expect(container?.querySelector('[data-testid="aux-terminal-pane"]')).not.toBeNull();
  });

  it("keeps the aux terminal mounted when the drawer is closed", async () => {
    renderApp();
    await waitForNewTerminalButton();

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "A",
        code: "KeyA",
        altKey: true,
        shiftKey: true
      }));
    });
    await waitForRequests();

    const drawer = container?.querySelector(".aux-terminal-drawer");
    expect(drawer?.getAttribute("data-open")).toBe("true");
    expect(container?.querySelector('[data-testid="aux-terminal-pane"]')).not.toBeNull();

    const closeButton = await waitForButtonText("Close", true);
    act(() => {
      closeButton.click();
    });
    await waitForRequests();

    expect(container?.querySelector(".aux-terminal-drawer")?.getAttribute("data-open")).toBe("false");
    expect(container?.querySelector('[data-testid="aux-terminal-pane"]')).not.toBeNull();
    expect(terminalPaneUnmounts.get("aux-terminal-pane") ?? 0).toBe(0);
  });

  it("lets the sidebar tree browse another project without forcing it back to the active terminal", async () => {
    renderApp();
    await waitForSelectedProject("/workspace");

    const otherProject = await waitForProjectCard("/other");
    act(() => {
      otherProject.click();
    });

    await waitForSelectedProject("/other");
    await waitForButtonText("Other window", true);
    expect(container?.querySelector('[data-testid="terminal-pane"]')?.getAttribute("data-window-id")).toBe("window-1");
  });

  it("syncs the sidebar project again when a terminal is selected from that tree", async () => {
    renderApp();
    await waitForSelectedProject("/workspace");

    const otherProject = await waitForProjectCard("/other");
    act(() => {
      otherProject.click();
    });
    await waitForSelectedProject("/other");

    const otherWindowButton = await waitForButtonText("Other window", true);
    act(() => {
      otherWindowButton.click();
    });

    await waitForTerminalPaneWindow("window-3");
    await waitForSelectedProject("/other");
  });

  it("syncs the sidebar project when switching terminals through the switcher", async () => {
    renderApp();
    await waitForSelectedProject("/workspace");

    const otherProject = await waitForProjectCard("/other");
    act(() => {
      otherProject.click();
    });
    await waitForSelectedProject("/other");

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "w",
        code: "KeyW",
        altKey: true
      }));
    });

    const otherWindowButton = await waitForSwitcherWindowButton("Other window");
    act(() => {
      otherWindowButton.click();
    });

    await waitForTerminalPaneWindow("window-3");
    await waitForSelectedProject("/other");
  });

  it("shows recent terminal status from other projects in the switcher", async () => {
    renderApp();
    await waitForSelectedProject("/workspace");

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "w",
        code: "KeyW",
        altKey: true
      }));
    });

    const otherWindowButton = await waitForSwitcherWindowButtonText("Other window", "claude code");

    expect(otherWindowButton.textContent).toContain("claude code");
    expect(otherWindowButton.textContent).toContain("other");
    expect(otherWindowButton.querySelector(".work-status-dot")).not.toBeNull();
  });
});
