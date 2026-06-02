import { afterEach, describe, expect, it, vi } from "vitest";

import {
  auxTerminalWebSocketUrl,
  ensureAuxTerminal,
  fetchAgentClients,
  fetchTerminalProjects,
  fetchTree,
  fetchWindowActivity
} from "../src/api";

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" }
  });
}

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(input.toString());
}

describe("api terminal time ranges", () => {
  it("passes the selected range to the tree API", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse([]));

    await fetchTree("client-1", "7d", "/workspace/project");

    const url = requestUrl(fetchMock.mock.calls[0][0]);
    expect(url.pathname).toBe("/api/clients/client-1/tree");
    expect(url.searchParams.get("range")).toBe("7d");
    expect(url.searchParams.get("project_path")).toBe("/workspace/project");
  });

  it("passes the selected range to the terminal projects API", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse([]));

    await fetchTerminalProjects("client-1", "30d");

    const url = requestUrl(fetchMock.mock.calls[0][0]);
    expect(url.pathname).toBe("/api/clients/client-1/terminal-projects");
    expect(url.searchParams.get("range")).toBe("30d");
  });

  it("passes the selected range to the window activity API", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ windows: [] }));

    await fetchWindowActivity("client-1", {
      includeRuntimeTags: true,
      range: "14d",
      projectPath: "/workspace/project"
    });

    const url = requestUrl(fetchMock.mock.calls[0][0]);
    expect(url.pathname).toBe("/api/clients/client-1/windows/activity");
    expect(url.searchParams.get("include_runtime_tags")).toBe("true");
    expect(url.searchParams.get("range")).toBe("14d");
    expect(url.searchParams.get("project_path")).toBe("/workspace/project");
  });

  it("ensures an aux terminal for a selected window", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({
      status: "ready",
      cwd: "/workspace"
    }));

    await expect(ensureAuxTerminal("client-1", "window-1")).resolves.toEqual({
      status: "ready",
      cwd: "/workspace"
    });

    const [input, init] = fetchMock.mock.calls[0];
    expect(requestUrl(input).pathname).toBe("/api/clients/client-1/windows/window-1/aux-terminal/ensure");
    expect(init?.method).toBe("POST");
  });

  it("fetches agent client descriptors for a client", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({
      agent_clients: [
        {
          id: "codex",
          provider_id: "codex",
          label: "Codex",
          aliases: [],
          default_command: "codex",
          command_names: ["codex"]
        }
      ]
    }));

    await expect(fetchAgentClients("client-1")).resolves.toEqual({
      agent_clients: [
        {
          id: "codex",
          provider_id: "codex",
          label: "Codex",
          aliases: [],
          default_command: "codex",
          command_names: ["codex"]
        }
      ]
    });

    expect(requestUrl(fetchMock.mock.calls[0][0]).pathname).toBe("/api/clients/client-1/agent-clients");
  });

  it("builds an aux terminal websocket URL with its own path", () => {
    const url = new URL(auxTerminalWebSocketUrl("client-1", "window-1", "view-1"));

    expect(url.protocol).toBe("ws:");
    expect(url.pathname).toBe("/api/clients/client-1/windows/window-1/aux-terminal");
    expect(url.searchParams.get("view_id")).toBe("view-1");
  });
});
