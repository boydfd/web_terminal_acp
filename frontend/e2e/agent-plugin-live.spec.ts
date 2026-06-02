import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { execFileSync } from "node:child_process";

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const LIVE = process.env.WEB_TERMINAL_LIVE_OPENCLAW_E2E === "1";
const AUTH_KEY = "web-terminal-acp:auth-token";
const OPENCLAW_CLIENT = "openclaw";
const AGENTS = [
  { id: "codex", label: "Codex", command: "codex --version" },
  { id: "claude", label: "Claude Code", command: "claude --version" },
  { id: "cursor", label: "Cursor", command: "agent --version" },
  { id: "antigravity", label: "Antigravity CLI", command: "agy-p --version" },
] as const;

type Client = { id: string; name: string; status: string; runtime: string; version: string | null };
type WindowOut = {
  id: string;
  cwd: string | null;
  status: string;
  shell_command: string | null;
  remote_session_id: string | null;
  remote_window_id: string | null;
};
type AgentConfig = {
  agent: string;
  sections: Array<{ id: "skills" | "plugins" | "hooks"; name: string; items: Array<{ id: string; name: string; enabled: boolean }> }>;
};

test.skip(!LIVE, "Set WEB_TERMINAL_LIVE_OPENCLAW_E2E=1 to run against the live openclaw client.");

function authSecret(): string {
  const envPath = resolve(import.meta.dirname, "../../.env");
  const line = readFileSync(envPath, "utf8").split(/\r?\n/).find((entry) => entry.startsWith("WEB_TERMINAL_AUTH_SECRET="));
  if (!line) throw new Error("WEB_TERMINAL_AUTH_SECRET missing from .env");
  return line.split("=", 2)[1].trim().replace(/^['"]|['"]$/g, "");
}

async function api<T>(request: APIRequestContext, token: string, path: string, options: Parameters<APIRequestContext["fetch"]>[1] = {}) {
  const headers = { Authorization: `Bearer ${token}`, ...(options.headers ?? {}) };
  const response = await request.fetch(path, { ...options, headers });
  expect(response.ok(), `${options.method ?? "GET"} ${path}: ${await response.text()}`).toBeTruthy();
  return response.json() as Promise<T>;
}

async function login(request: APIRequestContext) {
  const response = await request.post("/api/auth/login", { data: { secret: authSecret() } });
  expect(response.ok()).toBeTruthy();
  return (await response.json() as { token: string }).token;
}

async function openclaw(request: APIRequestContext, token: string) {
  const clients = await api<Client[]>(request, token, "/api/clients");
  const client = clients.find((item) => item.name === OPENCLAW_CLIENT);
  expect(client, "openclaw client must be registered").toBeTruthy();
  expect(client?.runtime).toBe("remote");
  expect(client?.status).toBe("ONLINE");
  return client as Client;
}

async function createWindow(request: APIRequestContext, token: string, clientId: string, data: Record<string, unknown>) {
  return api<WindowOut>(request, token, `/api/clients/${clientId}/windows`, { method: "POST", data });
}

async function waitForBoundWindow(request: APIRequestContext, token: string, clientId: string, windowId: string) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const window = await api<WindowOut>(request, token, `/api/clients/${clientId}/windows/${windowId}`);
    if (window.remote_session_id && window.remote_window_id) return window;
    await new Promise((resolveTick) => setTimeout(resolveTick, 500));
  }
  throw new Error(`window ${windowId} did not bind a remote tmux id`);
}

async function cleanupWindow(request: APIRequestContext, token: string, clientId: string, windowId: string) {
  const response = await request.delete(`/api/clients/${clientId}/windows/${windowId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect([204, 404]).toContain(response.status());
}

function firstConfigItem(config: AgentConfig) {
  for (const section of config.sections) {
    const item = section.items[0];
    if (item) return { section, item };
  }
  throw new Error(`${config.agent} has no skills/plugins/hooks to toggle`);
}

async function selectClient(page: Page) {
  await page.goto("/");
  const card = page.locator(".client-card", { hasText: OPENCLAW_CLIENT }).first();
  await expect(card).toContainText("ONLINE");
  await card.locator(".client-card-main").click();
  await expect(card).toContainText("v2.24.0");
}

async function openCreateConfig(page: Page, projectPath: string) {
  await page.keyboard.press("Alt+Shift+N");
  const picker = page.getByRole("dialog").filter({ hasText: "New terminal by project path" });
  await expect(picker).toBeVisible();
  for (const agent of ["No Agent", ...AGENTS.map((item) => item.label)]) {
    await expect(picker.getByRole("button", { name: agent })).toBeVisible();
  }
  await picker.getByLabel("Search project paths").fill(projectPath);
  await picker.getByRole("button", { name: "Codex" }).click();
  await picker.getByRole("button", { name: "配置" }).click();
  const dialog = page.getByRole("dialog", { name: "Create terminal" });
  await expect(dialog).toBeVisible();
  for (const agent of ["No Agent", ...AGENTS.map((item) => item.label)]) {
    await expect(dialog.getByRole("button", { name: agent })).toBeVisible();
  }
  await expect(dialog.locator("input").first()).toHaveValue(/codex/);
  await expect(dialog).toContainText("Skills");
  await expect(dialog).toContainText("Plugins");
  return dialog;
}

function seedRecord(clientId: string, windowId: string, suffix: string) {
  execFileSync("docker", ["compose", "--env-file", ".env", "exec", "-T", "backend", "python", "-"], {
    cwd: resolve(import.meta.dirname, "../.."),
    input: `
import asyncio, uuid
from datetime import datetime, timedelta, timezone
from app.db import SessionLocal
from app.models import AiSession, Event, EventSourceType

async def main():
    client_id = uuid.UUID("${clientId}")
    window_id = uuid.UUID("${windowId}")
    base = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        main_session = AiSession(client_id=client_id, provider="claude_code", source_id="live-main-${suffix}", project_path="/tmp/wt-acp-e2e-${suffix}", virtual_window_id=window_id, title="Live main agent")
        sub_session = AiSession(client_id=client_id, provider="claude_code", source_id="agent-live-${suffix}", source_path="/tmp/live-main-${suffix}/subagents/agent-live-${suffix}.jsonl", project_path="/tmp/wt-acp-e2e-${suffix}", virtual_window_id=window_id, title="Live subagent")
        session.add_all([main_session, sub_session])
        await session.flush()
        events = [
            Event(client_id=client_id, source_type=EventSourceType.agent_tool_record, source_id=main_session.source_id, kind="assistant_message", virtual_window_id=window_id, ai_session_id=main_session.id, fingerprint="live-call-${suffix}", created_at=base, payload_json={"provider":"claude_code","type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"call-live-${suffix}","name":"Agent","input":{"description":"Return one","prompt":"Return exactly: 1","subagent_type":"claude"}}]},"subagent_tool_use_results":[{"tool_use_id":"call-live-${suffix}","agent_id":"live-${suffix}"}]}),
            Event(client_id=client_id, source_type=EventSourceType.agent_tool_record, source_id=sub_session.source_id, kind="assistant_message", virtual_window_id=window_id, ai_session_id=sub_session.id, fingerprint="live-sub-${suffix}", created_at=base + timedelta(seconds=1), payload_json={"provider":"claude_code","type":"assistant","agentId":"live-${suffix}","isSidechain":True,"message":{"role":"assistant","content":[{"type":"text","text":"subagent internal answer"}]}}),
            Event(client_id=client_id, source_type=EventSourceType.agent_tool_record, source_id=main_session.source_id, kind="user_message", virtual_window_id=window_id, ai_session_id=main_session.id, fingerprint="live-result-${suffix}", created_at=base + timedelta(seconds=2), payload_json={"provider":"claude_code","type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"call-live-${suffix}","content":[{"type":"text","text":"1"}]}]},"toolUseResult":{"agentId":"live-${suffix}","toolUseId":"call-live-${suffix}"}}),
        ]
        session.add_all(events)
        await session.commit()

asyncio.run(main())
`,
  });
}

test("validates live agent plugin flows on the openclaw client", async ({ page, request }) => {
  const token = await login(request);
  await page.addInitScript(([key, value]) => window.localStorage.setItem(key, value), [AUTH_KEY, token]);
  const client = await openclaw(request, token);
  const suffix = randomUUID().slice(0, 8);
  const projectPath = `/tmp/wt-acp-e2e-${suffix}`;
  const windows: string[] = [];

  const agentList = await api<{ agent_clients: Array<{ id: string; label: string }> }>(
    request,
    token,
    `/api/clients/${client.id}/agent-clients`,
  );
  expect(agentList.agent_clients.map((agent) => agent.id)).toEqual(["codex", "claude", "cursor", "antigravity"]);

  const clientConfigs: Record<string, AgentConfig> = {};
  for (const agent of AGENTS) {
    const config = await api<AgentConfig>(request, token, `/api/clients/${client.id}/agent-config/${agent.id}`);
    clientConfigs[agent.id] = config;
    expect(config.sections.map((section) => section.id)).toEqual(["skills", "plugins", "hooks"]);
  }

  try {
    const seed = await createWindow(request, token, client.id, {
      cwd: projectPath,
      folder_path: `/E2E/${suffix}`,
      shell_command: `/bin/bash -lc "printf LIVE_UI_SEED_${suffix}; sleep 90"`,
    });
    windows.push(seed.id);
    await waitForBoundWindow(request, token, client.id, seed.id);

    await selectClient(page);
    await openCreateConfig(page, projectPath);
    await page.keyboard.press("Escape");

    await page.getByRole("button", { name: "Controls" }).click();
    await page.getByRole("menuitem", { name: /Settings/ }).click();
    const settings = page.getByRole("dialog", { name: "Settings" });
    await expect(settings).toBeVisible();
    await settings.getByRole("tab", { name: "Agent" }).click();
    for (const agent of AGENTS) {
      await expect(settings.getByText(`${agent.label} 启动命令`)).toBeVisible();
    }
    await settings.getByRole("button", { name: "关闭" }).click();

    for (const agent of AGENTS) {
      const source = firstConfigItem(clientConfigs[agent.id]);
      const launchConfig = {
        agent: agent.id,
        command: agent.command,
        config: {
          agent: agent.id,
          sections: clientConfigs[agent.id].sections.map((section) => ({
            id: section.id,
            items: section.items.map((item) => ({
              id: item.id,
              enabled: section.id === source.section.id && item.id === source.item.id ? !item.enabled : item.enabled,
            })),
          })),
        },
      };
      const created = await createWindow(request, token, client.id, {
        cwd: projectPath,
        folder_path: `/E2E/${suffix}`,
        agent_launch: launchConfig,
      });
      windows.push(created.id);
      const bound = await waitForBoundWindow(request, token, client.id, created.id);
      expect(bound.shell_command).toBe(agent.command);

      const config = await api<AgentConfig>(request, token, `/api/clients/${client.id}/windows/${created.id}/agent-config`);
      const launched = config.sections.find((section) => section.id === source.section.id)?.items.find((item) => item.id === source.item.id);
      expect(launched?.enabled).toBe(!source.item.enabled);

      const afterToggle = await api<AgentConfig>(
        request,
        token,
        `/api/clients/${client.id}/windows/${created.id}/agent-config/${source.section.id}/${encodeURIComponent(source.item.id)}`,
        { method: "PATCH", data: { enabled: source.item.enabled } },
      );
      const toggled = afterToggle.sections.find((section) => section.id === source.section.id)?.items.find((item) => item.id === source.item.id);
      expect(toggled?.enabled).toBe(source.item.enabled);
    }

    seedRecord(client.id, seed.id, suffix);
    const chat = await api<{ messages: Array<{ body: string; agent_message_type: string | null; target_session_id: string | null }> }>(
      request,
      token,
      `/api/clients/${client.id}/windows/${seed.id}/agent-record/chat`,
    );
    expect(chat.messages.some((message) => message.agent_message_type === "subagent_call" && message.target_session_id)).toBeTruthy();
    expect(chat.messages.map((message) => message.body).join("\n")).toContain("Description: Return one");

    await api<WindowOut>(request, token, `/api/clients/${client.id}/windows/${seed.id}`, {
      method: "PATCH",
      data: { title: `Live E2E Seed ${suffix}` },
    });
    await page.goto(`/clients/${client.id}/terminals/${seed.id}`);
    await expect(page.getByText(`Live E2E Seed ${suffix}`).first()).toBeVisible({ timeout: 30_000 });
    await page.getByRole("tab", { name: "Agent" }).click();
    await expect(page.getByText("Description: Return one").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Open subagent" }).first()).toBeVisible();
    await page.getByRole("button", { name: "Open subagent" }).first().click();
    const modal = page.getByRole("dialog", { name: "Agent record" });
    await expect(modal).toBeVisible();
    await expect(modal.getByText("subagent internal answer")).toBeVisible();
    await modal.getByRole("button", { name: "Collapse", exact: true }).click();

    await page.getByRole("tab", { name: "Config" }).click();
    await expect(page.getByRole("heading", { name: "Agent Config" })).toBeVisible();
    await expect(page.getByText("Skills").first()).toBeVisible();
    const configSwitch = page.locator(".detail-panel .agent-config-switch input").first();
    const checked = await configSwitch.isChecked();
    await configSwitch.click();
    await expect(configSwitch).toBeChecked({ checked: !checked });
  } finally {
    for (const windowId of windows.reverse()) {
      await cleanupWindow(request, token, client.id, windowId);
    }
  }
});
