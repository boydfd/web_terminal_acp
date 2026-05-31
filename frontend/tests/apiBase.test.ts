import { afterEach, describe, expect, it, vi } from "vitest";

const apiBaseModule = "../src/apiBase";

afterEach(() => {
  delete (window as Window & { __WEB_TERMINAL_API_BASE?: string }).__WEB_TERMINAL_API_BASE;
  window.localStorage.clear();
  vi.unstubAllEnvs();
  vi.resetModules();
});

async function loadApiBase() {
  return await import(apiBaseModule);
}

describe("apiBase", () => {
  it("uses the current API origin for remote client callbacks", async () => {
    (window as Window & { __WEB_TERMINAL_API_BASE?: string }).__WEB_TERMINAL_API_BASE =
      "http://control.example.com:5173";
    const { readClientAgentServerUrl } = await loadApiBase();

    expect(readClientAgentServerUrl()).toBe("http://control.example.com:5173");
  });

  it("uses VITE_CLIENT_AGENT_SERVER_URL for remote client callbacks when configured", async () => {
    vi.stubEnv("VITE_CLIENT_AGENT_SERVER_URL", "https://agent-control.example.com/backend/");
    (window as Window & { __WEB_TERMINAL_API_BASE?: string }).__WEB_TERMINAL_API_BASE =
      "http://control.example.com:5173";
    const { readClientAgentServerUrl } = await loadApiBase();

    expect(readClientAgentServerUrl()).toBe("https://agent-control.example.com/backend");
  });

  it("keeps backend URLs unchanged for remote clients", async () => {
    (window as Window & { __WEB_TERMINAL_API_BASE?: string }).__WEB_TERMINAL_API_BASE =
      "https://control.example.com/api";
    const { readClientAgentServerUrl } = await loadApiBase();

    expect(readClientAgentServerUrl()).toBe("https://control.example.com/api");
  });
});
