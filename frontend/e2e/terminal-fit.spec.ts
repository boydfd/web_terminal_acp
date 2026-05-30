import { createHash } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { Socket } from "node:net";

import { expect, test, type Page } from "@playwright/test";

import type { ClientWindowsActivity, TerminalRecentPage, TreeFolderCore } from "../src/types";

const LOCAL_CLIENT_ID = "00000000-0000-0000-0000-000000000001";
const LOCAL_WINDOW_ID = process.env.PLAYWRIGHT_WINDOW_ID ?? "00000000-0000-0000-0000-000000000002";
const TERMINAL_ACTIVE_VIEW_STORAGE_KEY = "web-terminal-acp:active-terminal-view";
const TERMINAL_SCREENSHOT_PATH = process.env.PLAYWRIGHT_TERMINAL_SCREENSHOT
  ?? "/tmp/web-terminal-acp-terminal-perfect.png";

type TerminalMetrics = {
  hostHeight: number;
  renderedHeight: number;
  lineCount: number;
  terminalRows: number;
  stageHeight: number;
  workspaceHeight: number;
  fillRatio: number;
  connectionStatus: string | null;
  visibleText: string;
};

async function readTerminalMetrics(page: Page): Promise<TerminalMetrics | null> {
  return page.evaluate(() => {
    const host = document.querySelector(".terminal-xterm-host");
    const rows = document.querySelector(".xterm-rows");
    const stage = document.querySelector(".terminal-stage");
    const workspace = document.querySelector(".workspace");
    const status = document.querySelector(".terminal-connection-status");

    if (!(host instanceof HTMLElement) || !(rows instanceof HTMLElement)) {
      return null;
    }

    const lineElements = rows.querySelectorAll("div");
    let renderedHeight = 0;
    if (lineElements.length > 0) {
      const first = lineElements[0].getBoundingClientRect();
      const last = lineElements[lineElements.length - 1].getBoundingClientRect();
      renderedHeight = last.bottom - first.top;
    } else {
      renderedHeight = rows.getBoundingClientRect().height;
    }

    const canvas = document.querySelector(".xterm-screen canvas");
    if (canvas instanceof HTMLElement) {
      const canvasHeight = canvas.getBoundingClientRect().height;
      if (canvasHeight > renderedHeight) {
        renderedHeight = canvasHeight;
      }
    }

    const hostHeight = host.clientHeight;
    const terminalRows = lineElements.length;

    return {
      hostHeight,
      renderedHeight,
      lineCount: terminalRows,
      terminalRows,
      stageHeight: stage instanceof HTMLElement ? stage.clientHeight : 0,
      workspaceHeight: workspace instanceof HTMLElement ? workspace.clientHeight : 0,
      fillRatio: hostHeight > 0 ? renderedHeight / hostHeight : 0,
      connectionStatus: status instanceof HTMLElement ? status.textContent : null,
      visibleText: rows.textContent ?? "",
    };
  });
}

async function waitForFilledTerminal(
  page: Page,
  minRatio = 0.9,
  timeoutMs = 60_000,
  requiredText?: string,
): Promise<TerminalMetrics> {
  const started = Date.now();
  let last: TerminalMetrics | null = null;

  while (Date.now() - started < timeoutMs) {
    last = await readTerminalMetrics(page);
    if (
      last !== null
      && last.hostHeight > 300
      && last.fillRatio >= minRatio
      && last.renderedHeight > 0
      && last.lineCount >= 20
      && (requiredText === undefined || last.visibleText.includes(requiredText))
    ) {
      return last;
    }
    await page.waitForTimeout(200);
  }

  throw new Error(
    `Terminal did not fill within ${timeoutMs}ms; last=${JSON.stringify(last)}`,
  );
}

function testClient() {
  const now = "2026-05-25T13:45:00.000Z";
  return {
    id: LOCAL_CLIENT_ID,
    name: "local",
    status: "ONLINE",
    hostname: "playwright",
    install_path: null,
    version: "1.3.5",
    last_update_at: now,
    runtime: "local",
    last_seen_at: now,
    connected_at: now,
    created_at: now,
    updated_at: now,
  };
}

function testWindow() {
  const now = "2026-05-25T13:45:00.000Z";
  return {
    id: LOCAL_WINDOW_ID,
    client_id: LOCAL_CLIENT_ID,
    title: "Playwright Full Screen Terminal",
    folder_id: "00000000-0000-0000-0000-000000000010",
    status: "ACTIVE",
    tmux_session: "playwright",
    tmux_window_id: "@1",
    remote_session_id: null,
    remote_window_id: null,
    cwd: "/tmp",
    shell_command: "/bin/bash",
    summary: null,
    title_tags: ["playwright"],
    runtime_tags: ["bash", "cwd-/tmp"],
    work_status: {
      state: "RECENT_ACTIVE",
      label: "recent active",
      color: "green",
      last_activity_at: now,
      last_working_activity_at: now,
    },
    title_manually_overridden: false,
    folder_manually_overridden: false,
    command_capture_supported: true,
    summary_job: null,
    created_at: now,
  };
}

function testTree() {
  const window = testWindow();
  return [{
    id: "00000000-0000-0000-0000-000000000010",
    name: "未分类",
    path: "/未分类",
    folders: [],
    windows: [{
      id: window.id,
      title: window.title,
      status: window.status,
      title_tags: window.title_tags,
      created_at: window.created_at,
    }],
  }];
}

function terminalOutput(rows = 40): string {
  const lines = [
    "\x1b[2J\x1b[H",
    "WEB TERMINAL ACP PLAYWRIGHT FULL SCREEN CHECK",
    "top marker: visible terminal content starts here",
  ];
  for (let index = 1; index <= rows; index += 1) {
    lines.push(`fit-row-${String(index).padStart(2, "0")} ` + "#".repeat(56));
  }
  lines.push("bottom marker: visible terminal content reaches the lower viewport");
  return `${lines.join("\r\n")}\r\n`;
}

type MockApiOptions = {
  slowTreeMs?: number;
  slowSocketMs?: number;
  tree?: TreeFolderCore[];
  activity?: ClientWindowsActivity;
  recents?: TerminalRecentPage;
  onRequest?: (url: URL) => void;
  onTerminalMessage?: (message: string | Buffer, ws: MockTerminalSocket) => void;
  afterTerminalConnected?: (ws: MockTerminalSocket) => void;
};

type MockTerminalSocket = {
  send: (message: string | Buffer) => void;
};

const closeMockServers: Array<() => Promise<void>> = [];

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "content-type",
    "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
  };
}

function websocketAcceptKey(key: string): string {
  return createHash("sha1")
    .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
    .digest("base64");
}

function encodeWsFrame(message: string | Buffer): Buffer {
  const payload = Buffer.isBuffer(message) ? message : Buffer.from(message);
  const opcode = Buffer.isBuffer(message) ? 0x2 : 0x1;
  if (payload.length < 126) {
    return Buffer.concat([Buffer.from([0x80 | opcode, payload.length]), payload]);
  }
  if (payload.length < 65536) {
    const header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 126;
    header.writeUInt16BE(payload.length, 2);
    return Buffer.concat([header, payload]);
  }
  const header = Buffer.alloc(10);
  header[0] = 0x80 | opcode;
  header[1] = 127;
  header.writeBigUInt64BE(BigInt(payload.length), 2);
  return Buffer.concat([header, payload]);
}

function decodeWsFrames(buffer: Buffer): { messages: Array<string | Buffer>; remaining: Buffer; closed: boolean } {
  const messages: Array<string | Buffer> = [];
  let offset = 0;
  let closed = false;

  while (buffer.length - offset >= 2) {
    const first = buffer[offset];
    const second = buffer[offset + 1];
    const opcode = first & 0x0f;
    const masked = (second & 0x80) !== 0;
    let length = second & 0x7f;
    let headerLength = 2;
    if (length === 126) {
      if (buffer.length - offset < 4) break;
      length = buffer.readUInt16BE(offset + 2);
      headerLength = 4;
    } else if (length === 127) {
      if (buffer.length - offset < 10) break;
      length = Number(buffer.readBigUInt64BE(offset + 2));
      headerLength = 10;
    }

    const maskLength = masked ? 4 : 0;
    const frameLength = headerLength + maskLength + length;
    if (buffer.length - offset < frameLength) break;
    const mask = masked ? buffer.subarray(offset + headerLength, offset + headerLength + 4) : null;
    const payload = Buffer.from(buffer.subarray(offset + headerLength + maskLength, offset + frameLength));
    if (mask !== null) {
      for (let index = 0; index < payload.length; index += 1) {
        payload[index] ^= mask[index % 4];
      }
    }
    if (opcode === 0x8) {
      closed = true;
      offset += frameLength;
      break;
    }
    if (opcode === 0x1) {
      messages.push(payload.toString("utf8"));
    } else if (opcode === 0x2) {
      messages.push(payload);
    }
    offset += frameLength;
  }

  return { messages, remaining: buffer.subarray(offset), closed };
}

async function startMockApiServer(options?: MockApiOptions): Promise<{ baseUrl: string; close: () => Promise<void> }> {
  const window = testWindow();
  const clientPath = `/api/clients/${LOCAL_CLIENT_ID}`;
  const windowPath = `${clientPath}/windows/${LOCAL_WINDOW_ID}`;
  const sockets = new Set<Socket>();

  const sendJson = (response: ServerResponse, payload: unknown) => {
    response.writeHead(200, {
      ...corsHeaders(),
      "Content-Type": "application/json",
    });
    response.end(JSON.stringify(payload));
  };

  const server: Server = createServer((request, response) => {
    void (async () => {
      const url = new URL(request.url ?? "/", "http://127.0.0.1");
      options?.onRequest?.(url);
      if (request.method === "OPTIONS") {
        response.writeHead(204, corsHeaders());
        response.end();
        return;
      }
      if (url.pathname === "/api/clients") {
        sendJson(response, [testClient()]);
      } else if (url.pathname === `${clientPath}/tree`) {
        if (options?.slowTreeMs) {
          await new Promise((resolve) => setTimeout(resolve, options.slowTreeMs));
        }
        sendJson(response, options?.tree ?? testTree());
      } else if (url.pathname === `${clientPath}/windows/activity`) {
        sendJson(response, options?.activity ?? {
          windows: [{
            window_id: LOCAL_WINDOW_ID,
            work_status: window.work_status,
            runtime_tags: window.runtime_tags,
            last_agent_task_completed_at: null,
            git_worktree: null,
          }],
        });
      } else if (url.pathname === windowPath) {
        sendJson(response, window);
      } else if (url.pathname === `${clientPath}/project-summaries`) {
        sendJson(response, []);
      } else if (url.pathname === `${clientPath}/terminal-recents` && request.method === "GET") {
        sendJson(response, options?.recents ?? {
          items: [{ window_id: LOCAL_WINDOW_ID, title: window.title, last_used_at: window.created_at }],
          page: Number(url.searchParams.get("page") ?? "1"),
          page_size: Number(url.searchParams.get("page_size") ?? "20"),
          total: 1,
          total_pages: 1,
        });
      } else if (url.pathname === `${clientPath}/terminal-recents` && request.method === "POST") {
        sendJson(response, { window_id: LOCAL_WINDOW_ID, title: window.title, last_used_at: window.created_at });
      } else if (url.pathname === `${clientPath}/search`) {
        sendJson(response, { query: "", results: [] });
      } else {
        response.writeHead(404, corsHeaders());
        response.end();
      }
    })().catch(() => {
      response.writeHead(500, corsHeaders());
      response.end();
    });
  });

  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
  });

  server.on("upgrade", (request: IncomingMessage, socket: Socket) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    if (url.pathname !== `${clientPath}/terminal/${LOCAL_WINDOW_ID}`) {
      socket.destroy();
      return;
    }

    const key = request.headers["sec-websocket-key"];
    if (typeof key !== "string") {
      socket.destroy();
      return;
    }
    socket.write([
      "HTTP/1.1 101 Switching Protocols",
      "Upgrade: websocket",
      "Connection: Upgrade",
      `Sec-WebSocket-Accept: ${websocketAcceptKey(key)}`,
      "\r\n",
    ].join("\r\n"));

    const ws: MockTerminalSocket = {
      send: (message) => socket.write(encodeWsFrame(message)),
    };
    let incoming = Buffer.alloc(0);
    socket.on("data", (chunk) => {
      incoming = Buffer.concat([incoming, chunk]);
      const decoded = decodeWsFrames(incoming);
      incoming = decoded.remaining;
      if (decoded.closed) {
        socket.end();
        return;
      }
      for (const message of decoded.messages) {
        options?.onTerminalMessage?.(message, ws);
        if (typeof message !== "string") {
          continue;
        }
        try {
          const parsed = JSON.parse(message) as { type?: unknown };
          if (parsed.type === "resize") {
            ws.send(terminalOutput());
          }
        } catch {
          continue;
        }
      }
    });
    setTimeout(() => {
      ws.send(JSON.stringify({ type: "terminal_status", status: "connected" }));
      ws.send(terminalOutput());
      options?.afterTerminalConnected?.(ws);
    }, options?.slowSocketMs ?? 25);
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (address === null || typeof address === "string") {
    throw new Error("mock API server did not bind to a TCP port");
  }
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    close: () => new Promise<void>((resolve) => {
      for (const socket of sockets) {
        socket.destroy();
      }
      server.close(() => resolve());
    }),
  };
}

async function mockApi(page: Page, options?: MockApiOptions) {
  const server = await startMockApiServer(options);
  closeMockServers.push(server.close);
  await page.addInitScript((apiBase) => {
    (window as Window & { __WEB_TERMINAL_API_BASE?: string }).__WEB_TERMINAL_API_BASE = apiBase;
  }, server.baseUrl);
}

function terminalPath(): string {
  return `/clients/${encodeURIComponent(LOCAL_CLIENT_ID)}/terminals/${encodeURIComponent(LOCAL_WINDOW_ID)}`;
}

test.describe("terminal viewport fit", () => {
  test.afterEach(async () => {
    const cleanup = closeMockServers.splice(0);
    await Promise.all(cleanup.map((close) => close()));
  });

  test("fills workspace on fast load", async ({ page }) => {
    await mockApi(page);
    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });

    const metrics = await waitForFilledTerminal(page, 0.9, 60_000, "fit-row-40");
    expect(metrics.fillRatio).toBeGreaterThanOrEqual(0.9);
    expect(metrics.renderedHeight).toBeGreaterThan(300);
    expect(metrics.visibleText).toContain("top marker");
    expect(metrics.visibleText).toContain("fit-row-40");
  });

  test("does not duplicate activity polling on initial terminal load", async ({ page }) => {
    const activityQueries: string[] = [];
    await mockApi(page, {
      onRequest: (url) => {
        if (url.pathname.endsWith("/windows/activity")) {
          activityQueries.push(url.searchParams.toString());
        }
      },
    });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });
    await waitForFilledTerminal(page, 0.9, 60_000, "fit-row-40");

    expect(activityQueries).toHaveLength(1);
    expect(activityQueries[0]).toBe("include_runtime_tags=true");
  });

  test("Alt+L locates the selected terminal in the sidebar list", async ({ page }) => {
    const selectedWindow = testWindow();
    const extraWindows = Array.from({ length: 28 }, (_, index) => {
      const suffix = String(index + 1).padStart(2, "0");
      return {
        id: `00000000-0000-0000-0000-0000000010${suffix}`,
        title: `Scrollable Terminal ${suffix}`,
        status: "ACTIVE",
        title_tags: [`scroll-${suffix}`],
        created_at: selectedWindow.created_at,
      };
    });

    await mockApi(page, {
      tree: [{
        id: "00000000-0000-0000-0000-000000000010",
        name: "未分类",
        path: "/未分类",
        folders: [],
        windows: [selectedWindow, ...extraWindows],
      }],
    });
    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    const selectedTerminal = page.locator(".tree-window.selected", { hasText: selectedWindow.title });
    await expect(selectedTerminal).toBeVisible();

    await page.locator(".sidebar").evaluate((sidebar) => {
      sidebar.scrollTop = sidebar.scrollHeight;
    });
    await expect.poll(
      () => selectedTerminal.evaluate((element) => {
        const sidebar = element.closest(".sidebar");
        if (!(sidebar instanceof HTMLElement)) {
          return false;
        }
        const elementBox = element.getBoundingClientRect();
        const sidebarBox = sidebar.getBoundingClientRect();
        return elementBox.bottom > sidebarBox.top && elementBox.top < sidebarBox.bottom;
      }),
      { timeout: 5000 },
    ).toBe(false);

    await page.keyboard.press("Alt+L");

    await expect(selectedTerminal).toHaveClass(/locating/);
    await expect.poll(
      () => page.evaluate(() => document.activeElement?.classList.contains("xterm-helper-textarea") ?? false),
      { timeout: 5000 },
    ).toBe(true);
    await expect.poll(
      () => selectedTerminal.evaluate((element) => {
        const sidebar = element.closest(".sidebar");
        if (!(sidebar instanceof HTMLElement)) {
          return false;
        }
        const elementBox = element.getBoundingClientRect();
        const sidebarBox = sidebar.getBoundingClientRect();
        return elementBox.top >= sidebarBox.top && elementBox.bottom <= sidebarBox.bottom;
      }),
      { timeout: 5000 },
    ).toBe(true);
  });

  test("Alt+W switcher fits narrow mobile viewports", async ({ page }) => {
    const now = "2026-05-25T13:45:00.000Z";
    const selectedWindow = testWindow();
    const longProjectPath = "/workspace/really-long-project-name-with-many-segments/apps/mobile-web-terminal/client";
    const windows = [
      {
        ...selectedWindow,
        title: "Playwright mobile terminal with an intentionally long title that should not overflow",
      },
      ...Array.from({ length: 4 }, (_, index) => {
        const suffix = String(index + 1).padStart(2, "0");
        return {
          id: `00000000-0000-0000-0000-0000000020${suffix}`,
          title: `Agent workspace ${suffix} with verbose terminal task title and project metadata`,
          status: "ACTIVE",
          title_tags: [`mobile-${suffix}`],
          created_at: now,
        };
      }),
    ];

    await page.setViewportSize({ width: 390, height: 844 });
    await mockApi(page, {
      tree: [{
        id: "00000000-0000-0000-0000-000000000010",
        name: "移动端终端切换测试分组",
        path: "/移动端终端切换测试分组",
        folders: [],
        windows,
      }],
      activity: {
        windows: windows.map((window, index) => ({
          window_id: window.id,
          work_status: selectedWindow.work_status,
          runtime_tags: ["codex", `${longProjectPath}-${index}`],
          last_agent_task_completed_at: null,
          git_worktree: null,
        })),
      },
      recents: {
        items: windows.map((window) => ({
          window_id: window.id,
          title: window.title,
          last_used_at: now,
        })),
        page: 1,
        page_size: 20,
        total: windows.length,
        total_pages: 1,
      },
    });
    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });

    await page.keyboard.press("Alt+W");
    await expect(page.locator(".terminal-switcher")).toBeVisible();

    const metrics = await page.evaluate(() => {
      const viewportWidth = document.documentElement.clientWidth;
      const dialog = document.querySelector(".terminal-switcher");
      const rows = Array.from(document.querySelectorAll(".switcher-window"));
      const metaRows = Array.from(document.querySelectorAll(".switcher-window-meta"));
      const rects = [dialog, ...rows, ...metaRows]
        .filter((element): element is Element => element instanceof Element)
        .map((element) => element.getBoundingClientRect());

      return {
        viewportWidth,
        documentScrollWidth: document.documentElement.scrollWidth,
        maxRight: Math.max(...rects.map((rect) => rect.right)),
        minLeft: Math.min(...rects.map((rect) => rect.left)),
        dialogWidth: dialog instanceof HTMLElement ? dialog.getBoundingClientRect().width : 0,
        rowCount: rows.length,
      };
    });

    expect(metrics.rowCount).toBeGreaterThan(1);
    expect(metrics.documentScrollWidth).toBeLessThanOrEqual(metrics.viewportWidth);
    expect(metrics.minLeft).toBeGreaterThanOrEqual(0);
    expect(metrics.maxRight).toBeLessThanOrEqual(metrics.viewportWidth);
    expect(metrics.dialogWidth).toBeLessThanOrEqual(374);
  });

  test("reconnects and focuses terminal after page reload", async ({ page }) => {
    let connectedCount = 0;

    await mockApi(page, {
      afterTerminalConnected: () => {
        connectedCount += 1;
      },
    });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });
    await expect.poll(() => connectedCount, { timeout: 5000 }).toBeGreaterThanOrEqual(1);
    await expect.poll(
      () => page.evaluate(() => document.querySelector(".terminal-connection-status")?.textContent ?? null),
      { timeout: 5000 },
    ).toBeNull();

    await page.locator(".terminal-xterm-host").click({ position: { x: 20, y: 20 } });
    await expect.poll(
      () => page.evaluate((storageKey) => {
        const raw = window.localStorage.getItem(storageKey);
        if (raw === null) {
          return null;
        }
        try {
          const parsed = JSON.parse(raw) as { viewId?: unknown };
          return typeof parsed.viewId === "string" ? parsed.viewId : null;
        } catch {
          return null;
        }
      }, TERMINAL_ACTIVE_VIEW_STORAGE_KEY),
      { timeout: 5000 },
    ).not.toBeNull();
    const previousViewId = await page.evaluate((storageKey) => {
      const raw = window.localStorage.getItem(storageKey);
      return raw === null ? null : (JSON.parse(raw) as { viewId: string }).viewId;
    }, TERMINAL_ACTIVE_VIEW_STORAGE_KEY);

    await page.reload({ waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });
    await expect.poll(() => connectedCount, { timeout: 5000 }).toBeGreaterThanOrEqual(2);
    await expect.poll(
      () => page.evaluate(() => document.querySelector(".terminal-connection-status")?.textContent ?? null),
      { timeout: 5000 },
    ).toBeNull();
    await expect.poll(
      () => page.evaluate(() => document.activeElement?.classList.contains("xterm-helper-textarea") ?? false),
      { timeout: 5000 },
    ).toBe(true);

    const reloadedViewId = await page.evaluate((storageKey) => {
      const raw = window.localStorage.getItem(storageKey);
      return raw === null ? null : (JSON.parse(raw) as { viewId: string }).viewId;
    }, TERMINAL_ACTIVE_VIEW_STORAGE_KEY);
    expect(reloadedViewId).not.toBeNull();
    expect(reloadedViewId).not.toBe(previousViewId);
  });

  test("shows the terminal when opened directly on mobile", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await mockApi(page);

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".mobile-terminal-active .xterm-rows", { timeout: 30_000 });

    const shellClass = await page.locator(".app-shell").getAttribute("class");
    expect(shellClass).toContain("mobile-terminal-active");

    const metrics = await waitForFilledTerminal(page, 0.9, 60_000, "fit-row-40");
    expect(metrics.workspaceHeight).toBeGreaterThan(700);
    expect(metrics.hostHeight).toBeGreaterThan(600);
    expect(metrics.visibleText).toContain("fit-row-40");
  });

  test("stays responsive when mobile quick input squeezes virtual keys", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.addInitScript(() => {
      const visualViewportListeners = new Map<string, Set<EventListenerOrEventListenerObject>>();
      const dispatchVisualViewportEvent = (type: string) => {
        const event = new Event(type);
        for (const listener of visualViewportListeners.get(type) ?? []) {
          if (typeof listener === "function") {
            listener.call(window.visualViewport, event);
          } else {
            listener.handleEvent(event);
          }
        }
      };
      const visualViewport = {
        width: 390,
        height: 844,
        offsetLeft: 0,
        offsetTop: 0,
        pageLeft: 0,
        pageTop: 0,
        scale: 1,
        addEventListener: (type: string, listener: EventListenerOrEventListenerObject | null) => {
          if (listener === null) {
            return;
          }
          const listeners = visualViewportListeners.get(type) ?? new Set<EventListenerOrEventListenerObject>();
          listeners.add(listener);
          visualViewportListeners.set(type, listeners);
        },
        removeEventListener: (type: string, listener: EventListenerOrEventListenerObject | null) => {
          if (listener === null) {
            return;
          }
          visualViewportListeners.get(type)?.delete(listener);
        },
        dispatchEvent: (event: Event) => {
          dispatchVisualViewportEvent(event.type);
          return true;
        },
      };
      Object.defineProperty(window, "visualViewport", {
        value: visualViewport,
        configurable: true,
      });
      Object.defineProperty(window, "__setVisualViewportHeight", {
        value: (height: number) => {
          visualViewport.height = height;
          dispatchVisualViewportEvent("resize");
        },
        configurable: true,
      });

      const state = {
        resizeObserverCallbacks: 0,
        renderResizeObserverCallbacks: 0,
        resizeMessages: 0,
        maxTimerDelayMs: 0,
        lastTickAt: performance.now(),
      };
      Object.defineProperty(window, "__mobileKeyboardSqueeze", {
        value: state,
        configurable: true,
      });

      window.setInterval(() => {
        const now = performance.now();
        state.maxTimerDelayMs = Math.max(state.maxTimerDelayMs, now - state.lastTickAt);
        state.lastTickAt = now;
      }, 50);

      const NativeResizeObserver = window.ResizeObserver;
      class InstrumentedResizeObserver extends NativeResizeObserver {
        constructor(callback: ResizeObserverCallback) {
          super((entries, observer) => {
            state.resizeObserverCallbacks += 1;
            if (entries.some((entry) => {
              const target = entry.target;
              return target instanceof HTMLElement
                && (
                  target.matches(".xterm-screen canvas")
                  || target.matches(".xterm-rows")
                );
            })) {
              state.renderResizeObserverCallbacks += 1;
            }
            callback(entries, observer);
          });
        }
      }
      Object.defineProperty(window, "ResizeObserver", {
        value: InstrumentedResizeObserver,
        configurable: true,
      });

      const originalPostMessage = Worker.prototype.postMessage;
      Worker.prototype.postMessage = function postMessage(message: unknown, transfer?: Transferable[]) {
        if (
          typeof message === "object"
          && message !== null
          && "type" in message
          && (message as { type?: unknown }).type === "json"
        ) {
          const rawData = (message as { data?: unknown }).data;
          if (typeof rawData === "string") {
            try {
              const parsed = JSON.parse(rawData) as { type?: unknown };
              if (parsed.type === "resize") {
                state.resizeMessages += 1;
              }
            } catch {
              // Ignore non-JSON worker payloads.
            }
          }
        }
        return originalPostMessage.call(this, message, transfer ?? []);
      };
    });
    await mockApi(page);

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".mobile-terminal-active .xterm-rows", { timeout: 30_000 });
    await waitForFilledTerminal(page, 0.9, 60_000, "fit-row-40");

    const virtualKeys = page.locator(".terminal-virtual-keys");
    if (!await virtualKeys.isVisible()) {
      await page.getByRole("button", { name: "Controls" }).click();
      await page.getByRole("menuitem", { name: /Virtual keys/ }).click();
    }
    await expect(virtualKeys).toBeVisible();

    await page.keyboard.press("Alt+I");
    const quickInput = page.getByLabel("Quick terminal input");
    await expect(quickInput).toBeFocused();

    const beforeSqueeze = await page.evaluate(() => ({
      ...(window as Window & {
        __mobileKeyboardSqueeze?: {
          resizeObserverCallbacks: number;
          renderResizeObserverCallbacks: number;
          resizeMessages: number;
          maxTimerDelayMs: number;
          lastTickAt: number;
        };
      }).__mobileKeyboardSqueeze,
    }));
    await page.evaluate(() => {
      const state = (window as Window & {
        __mobileKeyboardSqueeze?: {
          maxTimerDelayMs: number;
          lastTickAt: number;
        };
      }).__mobileKeyboardSqueeze;
      if (state !== undefined) {
        state.maxTimerDelayMs = 0;
        state.lastTickAt = performance.now();
      }
    });

    await page.evaluate(() => {
      (window as Window & {
        __setVisualViewportHeight?: (height: number) => void;
      }).__setVisualViewportHeight?.(340);
    });
    await quickInput.fill("soft keyboard squeeze");
    await page.waitForTimeout(1500);

    await expect(quickInput).toHaveValue("soft keyboard squeeze");
    await expect(page.locator(".terminal-virtual-keys")).toBeVisible();
    await expect.poll(
      () => page.evaluate(() => {
        const shell = document.querySelector(".app-shell");
        return shell instanceof HTMLElement ? shell.getBoundingClientRect().height : 0;
      }),
      { timeout: 3000 },
    ).toBeLessThanOrEqual(360);

    const afterSqueeze = await page.evaluate(() => ({
      ...(window as Window & {
        __mobileKeyboardSqueeze?: {
          resizeObserverCallbacks: number;
          renderResizeObserverCallbacks: number;
          resizeMessages: number;
          maxTimerDelayMs: number;
          lastTickAt: number;
        };
      }).__mobileKeyboardSqueeze,
    }));
    expect(afterSqueeze.resizeObserverCallbacks - beforeSqueeze.resizeObserverCallbacks).toBeLessThan(60);
    expect(afterSqueeze.renderResizeObserverCallbacks - beforeSqueeze.renderResizeObserverCallbacks).toBeLessThan(20);
    expect(afterSqueeze.resizeMessages - beforeSqueeze.resizeMessages).toBeLessThan(12);
    expect(afterSqueeze.maxTimerDelayMs).toBeLessThan(250);
  });

  test("fills workspace when tree API is slow", async ({ page }) => {
    await mockApi(page, { slowTreeMs: 6000 });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });

    const metrics = await waitForFilledTerminal(page, 0.9, 90_000, "fit-row-40");
    expect(metrics.fillRatio).toBeGreaterThanOrEqual(0.9);
  });

  test("fills workspace when terminal websocket is slow", async ({ page }) => {
    await mockApi(page, { slowSocketMs: 8000 });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });

    const metrics = await waitForFilledTerminal(page, 0.9, 90_000, "fit-row-40");
    expect(metrics.fillRatio).toBeGreaterThanOrEqual(0.9);
  });

  test("fills workspace when tree and websocket are both slow", async ({ page }) => {
    await mockApi(page, { slowTreeMs: 5000, slowSocketMs: 8000 });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });

    const metrics = await waitForFilledTerminal(page, 0.9, 120_000, "fit-row-40");
    expect(metrics.fillRatio).toBeGreaterThanOrEqual(0.9);
  });

  test("stays filled after delayed layout settle", async ({ page }) => {
    await mockApi(page, { slowTreeMs: 4000 });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });
    await waitForFilledTerminal(page, 0.9, 90_000, "fit-row-40");

    await page.waitForTimeout(3000);
    const afterSettle = await readTerminalMetrics(page);
    expect(afterSettle).not.toBeNull();
    expect(afterSettle!.fillRatio).toBeGreaterThanOrEqual(0.9);
  });

  test("captures a full visible terminal screenshot", async ({ page }) => {
    await mockApi(page, { slowSocketMs: 1500 });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });

    const metrics = await waitForFilledTerminal(page, 0.92, 90_000, "bottom marker");
    expect(metrics.connectionStatus).toBeNull();
    expect(metrics.visibleText).toContain("top marker");
    expect(metrics.visibleText).toContain("fit-row-40");
    expect(metrics.visibleText).toContain("bottom marker");
    expect(metrics.renderedHeight).toBeGreaterThan(700);
    await page.screenshot({ path: TERMINAL_SCREENSHOT_PATH, fullPage: false });
  });

  test("sends Escape to tmux when terminal already has focus", async ({ page }) => {
    const inputs: string[] = [];
    let resolveEscapeInput: ((text: string) => void) | null = null;
    const escapeInputPromise = new Promise<string>((resolve) => {
      resolveEscapeInput = resolve;
    });

    await mockApi(page, {
      onTerminalMessage: (message) => {
        if (!Buffer.isBuffer(message)) {
          return;
        }

        const text = message.toString("utf8");
        inputs.push(text);
        if (text.includes("\x1b")) {
          resolveEscapeInput?.(text);
        }
      },
    });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });
    await page.locator(".terminal-xterm-host").click({ position: { x: 20, y: 20 } });
    await page.waitForFunction(() => document.activeElement?.classList.contains("xterm-helper-textarea"));

    await page.keyboard.press("Escape");

    const receivedInput = await Promise.race([
      escapeInputPromise,
      page.waitForTimeout(2000).then(() => {
        throw new Error("Timed out waiting for Escape terminal input");
      }),
    ]);
    expect(receivedInput).toBe("\x1b");
    await expect.poll(
      () => page.evaluate(() => document.activeElement?.classList.contains("xterm-helper-textarea") ?? false),
      { timeout: 2000 },
    ).toBe(true);

    const inputCountAfterEscape = inputs.length;
    await page.keyboard.press("a");
    await expect.poll(() => inputs.slice(inputCountAfterEscape).join(""), { timeout: 2000 }).toContain("a");
  });

  test("keeps terminal input sends responsive during agent output bursts", async ({ page }) => {
    type ServerPendingInput = {
      expected: string;
      startedAt: number;
      resolve: (delayMs: number) => void;
      reject: (error: Error) => void;
      timer: ReturnType<typeof setTimeout>;
    };

    const pendingInputs: ServerPendingInput[] = [];
    const browserDispatchDelays: number[] = [];
    const serverReceiveDelays: number[] = [];
    const inputSequence = "bcdfhijklmqrsvwxyz!$";
    let burstTimer: ReturnType<typeof setTimeout> | null = null;
    let burstIndex = 0;

    const waitForInput = (expected: string): Promise<number> => new Promise((resolve, reject) => {
      const pending: ServerPendingInput = {
        expected,
        startedAt: Date.now(),
        resolve,
        reject,
        timer: setTimeout(() => {
          const index = pendingInputs.indexOf(pending);
          if (index >= 0) {
            pendingInputs.splice(index, 1);
          }
          reject(new Error(`Timed out waiting for terminal input ${expected}`));
        }, 2000),
      };
      pendingInputs.push(pending);
    });

    await page.addInitScript(() => {
      type BrowserInputRecord = {
        key: string;
        text: string | null;
        keydownAt: number;
        onDataAt?: number;
        preHandlerDelayMs?: number;
        dispatchAt: number;
        delayMs: number;
        echoAt?: number;
        echoDelayMs?: number;
      };
      const records: BrowserInputRecord[] = [];
      const pending: Array<{ key: string; keydownAt: number }> = [];
      const decoder = new TextDecoder();

      Object.defineProperty(window, "__terminalInputDispatchRecords", {
        value: records,
        configurable: true,
      });
      Object.defineProperty(window, "__terminalInputOnDataPending", {
        value: pending,
        configurable: true,
      });

      (window as Window & {
        __WEB_TERMINAL_TEST_ON_TERMINAL_DATA__?: (data: string, onDataAt: number) => void;
      }).__WEB_TERMINAL_TEST_ON_TERMINAL_DATA__ = (data, onDataAt) => {
        const index = pending.findIndex((candidate) => data.includes(candidate.key));
        if (index < 0) {
          return;
        }
        const candidate = pending[index];
        const existing = records.find((record) => (
          record.key === candidate.key
          && record.keydownAt === candidate.keydownAt
        ));
        if (existing !== undefined) {
          existing.onDataAt = onDataAt;
          existing.preHandlerDelayMs = onDataAt - candidate.keydownAt;
        }
      };

      window.addEventListener("keydown", (event) => {
        if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
          pending.push({ key: event.key, keydownAt: performance.now() });
        }
      }, { capture: true });

      const originalPostMessage = Worker.prototype.postMessage;
      Worker.prototype.postMessage = function postMessage(message: unknown, transfer?: Transferable[]) {
        if (
          typeof message === "object"
          && message !== null
          && "type" in message
          && (message as { type?: unknown }).type === "input"
        ) {
          const data = (message as { data?: unknown }).data;
          const text = ArrayBuffer.isView(data) ? decoder.decode(data) : null;
          const index = pending.findIndex((candidate) => text.includes(candidate.key));
          if (index >= 0) {
            const [candidate] = pending.splice(index, 1);
            const dispatchAt = performance.now();
            records.push({
              key: candidate.key,
              text,
              keydownAt: candidate.keydownAt,
              onDataAt: dispatchAt,
              preHandlerDelayMs: dispatchAt - candidate.keydownAt,
              dispatchAt,
              delayMs: dispatchAt - candidate.keydownAt,
            });
          }
        }
        return originalPostMessage.call(this, message, transfer ?? []);
      };

      (window as Window & {
        __WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__?: (data: string | Uint8Array, parsedAt: number) => void;
      }).__WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__ = (data, parsedAt) => {
        const text = typeof data === "string" ? data : decoder.decode(data);
        for (const record of records) {
          if (record.echoAt !== undefined || record.text === null || !text.includes(record.key)) {
            continue;
          }
          record.echoAt = parsedAt;
          record.echoDelayMs = parsedAt - record.keydownAt;
        }
      };
    });

    await mockApi(page, {
      onTerminalMessage: (message, ws) => {
        if (!Buffer.isBuffer(message)) {
          return;
        }
        const text = message.toString("utf8");
        const index = pendingInputs.findIndex((pending) => text.includes(pending.expected));
        ws.send(Buffer.from(text));
        if (index < 0) {
          return;
        }
        const [pending] = pendingInputs.splice(index, 1);
        clearTimeout(pending.timer);
        pending.resolve(Date.now() - pending.startedAt);
      },
      afterTerminalConnected: (ws) => {
        const sendBurst = () => {
          for (let index = 0; index < 80; index += 1) {
            burstIndex += 1;
            ws.send(Buffer.from(`agent-output-${burstIndex} ${"#".repeat(160)}\r\n`));
          }
          if (burstIndex < 8000) {
            burstTimer = setTimeout(sendBurst, 0);
          }
        };
        sendBurst();
      },
    });

    try {
      await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
      await page.waitForSelector(".xterm-rows", { timeout: 30_000 });
      await page.locator(".terminal-xterm-host").click({ position: { x: 20, y: 20 } });
      await page.waitForFunction(() => document.activeElement?.classList.contains("xterm-helper-textarea"));

      for (const expected of inputSequence) {
        const currentBrowserRecordCount = await page.evaluate(() => (
          (window as Window & { __terminalInputDispatchRecords?: unknown[] }).__terminalInputDispatchRecords?.length ?? 0
        ));
        const browserDispatchPromise = page.waitForFunction(
          (count) => (
            (window as Window & { __terminalInputDispatchRecords?: unknown[] }).__terminalInputDispatchRecords?.length ?? 0
          ) > count,
          currentBrowserRecordCount,
          { timeout: 2000 },
        );
        const inputPromise = waitForInput(expected);
        await page.keyboard.type(expected);
        await browserDispatchPromise;
        const browserRecord = await page.evaluate(() => {
          const records = (window as Window & {
            __terminalInputDispatchRecords?: Array<{
              delayMs: number;
              preHandlerDelayMs?: number;
              dispatchAt: number;
              onDataAt?: number;
            }>;
          }).__terminalInputDispatchRecords ?? [];
          return records[records.length - 1] ?? null;
        });
        if (browserRecord !== null) {
          browserDispatchDelays.push(browserRecord.delayMs);
        }
        serverReceiveDelays.push(await inputPromise);
        await page.waitForTimeout(25);
      }
    } finally {
      if (burstTimer !== null) {
        clearTimeout(burstTimer);
      }
      for (const pending of pendingInputs.splice(0)) {
        clearTimeout(pending.timer);
        pending.reject(new Error("Test finished before terminal input was observed"));
      }
    }
    const sorted = [...browserDispatchDelays].sort((left, right) => left - right);
    const p95 = sorted[Math.floor((sorted.length - 1) * 0.95)];
    expect(browserDispatchDelays.length).toBe(inputSequence.length);
    expect(Math.max(...browserDispatchDelays)).toBeLessThan(10);
    expect(p95).toBeLessThan(10);
    expect(serverReceiveDelays.length).toBe(inputSequence.length);
  });

  test("flushes echoed input promptly when output queue is idle", async ({ page }) => {
    type BrowserInputRecord = {
      key: string;
      keydownAt: number;
      dispatchAt?: number;
      dispatchDelayMs?: number;
      interactiveAt?: number;
      interactiveDelayMs?: number;
      writeAfterInteractiveMs?: number;
      echoAt?: number;
      echoDelayMs?: number;
    };

    await page.addInitScript(() => {
      const records: BrowserInputRecord[] = [];
      const pending: Array<{ key: string; keydownAt: number }> = [];
      const decoder = new TextDecoder();

      Object.defineProperty(window, "__terminalInputEchoRecords", {
        value: records,
        configurable: true,
      });
      Object.defineProperty(window, "__terminalLastWriteAt", {
        value: { current: 0 },
        configurable: true,
      });

      window.addEventListener("keydown", (event) => {
        if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
          pending.push({ key: event.key, keydownAt: performance.now() });
        }
      }, { capture: true });

      const originalPostMessage = Worker.prototype.postMessage;
      Worker.prototype.postMessage = function postMessage(message: unknown, transfer?: Transferable[]) {
        if (
          typeof message === "object"
          && message !== null
          && "type" in message
          && (message as { type?: unknown }).type === "input"
        ) {
          const data = (message as { data?: unknown }).data;
          const text = ArrayBuffer.isView(data) ? decoder.decode(data) : null;
          if (text !== null) {
            const index = pending.findIndex((candidate) => text.includes(candidate.key));
            if (index >= 0) {
              const [candidate] = pending.splice(index, 1);
              const dispatchAt = performance.now();
              records.push({
                key: candidate.key,
                keydownAt: candidate.keydownAt,
                dispatchAt,
                dispatchDelayMs: dispatchAt - candidate.keydownAt,
              });
            }
          }
        }
        return originalPostMessage.call(this, message, transfer ?? []);
      };

      (window as Window & {
        __WEB_TERMINAL_TEST_ON_INTERACTIVE_OUTPUT__?: (data: string | Uint8Array, receivedAt: number) => void;
      }).__WEB_TERMINAL_TEST_ON_INTERACTIVE_OUTPUT__ = (data, receivedAt) => {
        const text = typeof data === "string" ? data : decoder.decode(data);
        for (const record of records) {
          if (record.interactiveAt !== undefined || !text.includes(record.key)) {
            continue;
          }
          record.interactiveAt = receivedAt;
          record.interactiveDelayMs = receivedAt - record.keydownAt;
        }
      };

      (window as Window & {
        __WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__?: (data: string | Uint8Array, parsedAt: number) => void;
      }).__WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__ = (data, parsedAt) => {
        ((window as Window & {
          __terminalLastWriteAt?: { current: number };
        }).__terminalLastWriteAt ??= { current: 0 }).current = parsedAt;
        const text = typeof data === "string" ? data : decoder.decode(data);
        for (const record of records) {
          if (record.echoAt !== undefined || !text.includes(record.key)) {
            continue;
          }
          record.echoAt = parsedAt;
          record.echoDelayMs = parsedAt - record.keydownAt;
          if (record.interactiveAt !== undefined) {
            record.writeAfterInteractiveMs = parsedAt - record.interactiveAt;
          }
        }
      };
    });

    await mockApi(page, {
      onTerminalMessage: (message, ws) => {
        if (Buffer.isBuffer(message)) {
          ws.send(message);
        }
      },
    });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });
    await waitForFilledTerminal(page, 0.9, 60_000, "fit-row-40");
    await page.locator(".terminal-xterm-host").click({ position: { x: 20, y: 20 } });
    await page.waitForFunction(() => document.activeElement?.classList.contains("xterm-helper-textarea"));
    await expect.poll(
      () => page.evaluate(() => {
        const lastWriteAt = (window as Window & {
          __terminalLastWriteAt?: { current: number };
        }).__terminalLastWriteAt?.current ?? 0;
        return lastWriteAt > 0 ? performance.now() - lastWriteAt : 0;
      }),
      { timeout: 3000 },
    ).toBeGreaterThan(100);
    await page.keyboard.type("z");

    await expect.poll(
      () => page.evaluate(() => {
        const records = (window as Window & {
          __terminalInputEchoRecords?: Array<{ echoDelayMs?: number }>;
        }).__terminalInputEchoRecords ?? [];
        return records.filter((record) => typeof record.echoDelayMs === "number").length;
      }),
      { timeout: 2000 },
    ).toBe(1);

    const record = await page.evaluate(() => {
      const records = (window as Window & {
        __terminalInputEchoRecords?: Array<{
          dispatchDelayMs?: number;
          interactiveDelayMs?: number;
          writeAfterInteractiveMs?: number;
          echoDelayMs?: number;
        }>;
      }).__terminalInputEchoRecords ?? [];
      return records[0] ?? null;
    });
    expect(record).not.toBeNull();
    expect(record?.dispatchDelayMs).toBeLessThan(10);
    expect(record?.interactiveDelayMs).toBeLessThan(25);
    expect(record?.writeAfterInteractiveMs).toBeLessThan(10);
    expect(record?.echoDelayMs).toBeLessThan(35);
  });

  test("printable fast path marks xterm user input without duplicate send", async ({ page }) => {
    await page.addInitScript(() => {
      const decoder = new TextDecoder();
      const state = {
        inputMessages: [] as string[],
        writeDelayMs: null as number | null,
        keydownAt: 0,
      };
      Object.defineProperty(window, "__terminalFastPathUserInput", {
        value: state,
        configurable: true,
      });

      window.addEventListener("keydown", (event) => {
        if (event.key === "q") {
          state.keydownAt = performance.now();
        }
      }, { capture: true });

      const originalPostMessage = Worker.prototype.postMessage;
      Worker.prototype.postMessage = function postMessage(message: unknown, transfer?: Transferable[]) {
        if (
          typeof message === "object"
          && message !== null
          && "type" in message
          && (message as { type?: unknown }).type === "input"
        ) {
          const data = (message as { data?: unknown }).data;
          if (ArrayBuffer.isView(data)) {
            state.inputMessages.push(decoder.decode(data));
          }
        }
        return originalPostMessage.call(this, message, transfer ?? []);
      };

      (window as Window & {
        __WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__?: (data: string | Uint8Array, parsedAt: number) => void;
      }).__WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__ = (data, parsedAt) => {
        const text = typeof data === "string" ? data : decoder.decode(data);
        if (state.writeDelayMs === null && text.includes("q")) {
          state.writeDelayMs = parsedAt - state.keydownAt;
        }
      };
    });

    await mockApi(page, {
      onTerminalMessage: (message, ws) => {
        if (Buffer.isBuffer(message) && message.toString("utf8") === "q") {
          ws.send(Buffer.from("q"));
        }
      },
    });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });
    await waitForFilledTerminal(page, 0.9, 60_000, "fit-row-40");
    await page.locator(".terminal-xterm-host").click({ position: { x: 20, y: 20 } });
    await page.waitForFunction(() => document.activeElement?.classList.contains("xterm-helper-textarea"));
    await page.keyboard.type("q");

    await expect.poll(
      () => page.evaluate(() => {
        return (window as Window & {
          __terminalFastPathUserInput?: { writeDelayMs: number | null };
        }).__terminalFastPathUserInput?.writeDelayMs ?? null;
      }),
      { timeout: 2000 },
    ).not.toBeNull();

    const state = await page.evaluate(() => {
      return (window as Window & {
        __terminalFastPathUserInput?: { inputMessages: string[]; writeDelayMs: number | null };
      }).__terminalFastPathUserInput;
    });
    expect(state?.inputMessages.filter((message) => message === "q")).toHaveLength(1);
    expect(state?.writeDelayMs).toBeLessThan(35);
  });

  test("worker waits for output ack before posting another burst chunk", async ({ page }) => {
    await page.addInitScript(() => {
      const NativeWorker = window.Worker;
      const state = {
        outputCount: 0,
        outputCountBeforeFirstAck: null as number | null,
      };
      Object.defineProperty(window, "__terminalWorkerFlowControl", {
        value: state,
        configurable: true,
      });

      class InstrumentedWorker extends NativeWorker {
        private messageHandler: ((this: Worker, event: MessageEvent) => unknown) | null = null;

        constructor(scriptURL: string | URL, options?: WorkerOptions) {
          super(scriptURL, options);
          super.addEventListener("message", (event: MessageEvent) => {
            const data = event.data as { type?: unknown } | null;
            if (data?.type === "output" || data?.type === "interactive-output") {
              state.outputCount += 1;
              if (state.outputCount === 1) {
                window.setTimeout(() => {
                  state.outputCountBeforeFirstAck = state.outputCount;
                  this.messageHandler?.call(this, event);
                }, 80);
                return;
              }
            }
            this.messageHandler?.call(this, event);
          });
        }

        get onmessage(): ((this: Worker, event: MessageEvent) => unknown) | null {
          return this.messageHandler;
        }

        set onmessage(handler: ((this: Worker, event: MessageEvent) => unknown) | null) {
          this.messageHandler = handler;
        }
      }

      Object.defineProperty(window, "Worker", {
        value: InstrumentedWorker,
        configurable: true,
      });
    });

    await mockApi(page, {
      afterTerminalConnected: (ws) => {
        ws.send(Buffer.from("flow-control-one\r\n"));
        ws.send(Buffer.from("flow-control-two\r\n"));
      },
    });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });

    await expect.poll(
      () => page.evaluate(() => {
        return (window as Window & {
          __terminalWorkerFlowControl?: { outputCountBeforeFirstAck: number | null };
        }).__terminalWorkerFlowControl?.outputCountBeforeFirstAck ?? null;
      }),
      { timeout: 3000 },
    ).toBe(1);

    await expect.poll(
      () => page.evaluate(() => {
        return (window as Window & {
          __terminalWorkerFlowControl?: { outputCount: number };
        }).__terminalWorkerFlowControl?.outputCount ?? 0;
      }),
      { timeout: 3000 },
    ).toBeGreaterThan(1);
  });

  test("browser releases worker output before terminal write callback", async ({ page }) => {
    await page.addInitScript(() => {
      const NativeWorker = window.Worker;
      const decoder = new TextDecoder();
      const marker = "worker-ack-before-write";
      const state = {
        markerOutputDelivered: false,
        markerWriteCalled: false,
        outputAckBeforeMarkerWrite: false,
      };
      Object.defineProperty(window, "__terminalWorkerAckFlow", {
        value: state,
        configurable: true,
      });

      (window as Window & {
        __WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__?: (data: string | Uint8Array, parsedAt: number) => void;
      }).__WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__ = (data) => {
        const text = typeof data === "string" ? data : decoder.decode(data);
        if (text.includes(marker)) {
          state.markerWriteCalled = true;
        }
      };

      class InstrumentedWorker extends NativeWorker {
        private messageHandler: ((this: Worker, event: MessageEvent) => unknown) | null = null;

        constructor(scriptURL: string | URL, options?: WorkerOptions) {
          super(scriptURL, options);
          super.addEventListener("message", (event: MessageEvent) => {
            const data = event.data as { type?: unknown; data?: unknown } | null;
            if (data?.type === "output" || data?.type === "interactive-output") {
              const chunk = data.data;
              const text = typeof chunk === "string" ? chunk : decoder.decode(chunk as Uint8Array);
              if (text.includes(marker)) {
                state.markerOutputDelivered = true;
              }
            }
            this.messageHandler?.call(this, event);
          });
        }

        get onmessage(): ((this: Worker, event: MessageEvent) => unknown) | null {
          return this.messageHandler;
        }

        set onmessage(handler: ((this: Worker, event: MessageEvent) => unknown) | null) {
          this.messageHandler = handler;
        }
      }

      const originalPostMessage = NativeWorker.prototype.postMessage;
      InstrumentedWorker.prototype.postMessage = function postMessage(message: unknown, transfer?: Transferable[]) {
        if (
          typeof message === "object"
          && message !== null
          && "type" in message
          && (message as { type?: unknown }).type === "output-ack"
          && state.markerOutputDelivered
          && !state.markerWriteCalled
        ) {
          state.outputAckBeforeMarkerWrite = true;
        }
        return originalPostMessage.call(this, message, transfer ?? []);
      };

      Object.defineProperty(window, "Worker", {
        value: InstrumentedWorker,
        configurable: true,
      });
    });

    await mockApi(page, {
      afterTerminalConnected: (ws) => {
        ws.send(Buffer.from("worker-ack-before-write\r\n"));
      },
    });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });

    await expect.poll(
      () => page.evaluate(() => {
        return (window as Window & {
          __terminalWorkerAckFlow?: { outputAckBeforeMarkerWrite: boolean };
        }).__terminalWorkerAckFlow?.outputAckBeforeMarkerWrite ?? false;
      }),
      { timeout: 3000 },
    ).toBe(true);
  });

  test("worker preserves queued output before echoed input", async ({ page }) => {
    await page.addInitScript(() => {
      const NativeWorker = window.Worker;
      const state = {
        delivered: [] as string[],
        delayingStale: false,
        delayedFirstStale: false,
        releaseDelayedStale: null as null | (() => void),
      };
      Object.defineProperty(window, "__terminalWorkerInputOrdering", {
        value: state,
        configurable: true,
      });

      class InstrumentedWorker extends NativeWorker {
        private messageHandler: ((this: Worker, event: MessageEvent) => unknown) | null = null;

        constructor(scriptURL: string | URL, options?: WorkerOptions) {
          super(scriptURL, options);
          super.addEventListener("message", (event: MessageEvent) => {
            const data = event.data as { type?: unknown; data?: unknown } | null;
            if (data?.type === "output" || data?.type === "interactive-output") {
              const chunk = data.data;
              const text = typeof chunk === "string" ? chunk : new TextDecoder().decode(chunk as Uint8Array);
              state.delivered.push(text);
              if (!state.delayedFirstStale && text.includes("stale-before-input-0")) {
                state.delayedFirstStale = true;
                state.delayingStale = true;
                state.releaseDelayedStale = () => {
                  state.delayingStale = false;
                  state.releaseDelayedStale = null;
                  this.messageHandler?.call(this, event);
                };
                return;
              }
            }
            this.messageHandler?.call(this, event);
          });
        }

        get onmessage(): ((this: Worker, event: MessageEvent) => unknown) | null {
          return this.messageHandler;
        }

        set onmessage(handler: ((this: Worker, event: MessageEvent) => unknown) | null) {
          this.messageHandler = handler;
        }
      }

      Object.defineProperty(window, "Worker", {
        value: InstrumentedWorker,
        configurable: true,
      });
    });

    let burstTimer: NodeJS.Timeout | null = null;
    await mockApi(page, {
      afterTerminalConnected: (ws) => {
        ws.send(Buffer.from("stale-before-input-0\r\n"));
        for (let index = 1; index < 20; index += 1) {
          ws.send(Buffer.from(`stale-before-input-${index}\r\n`));
        }
      },
      onTerminalMessage: (message, ws) => {
        if (!Buffer.isBuffer(message)) {
          return;
        }
        const text = message.toString("utf8");
        if (!text.includes("~")) {
          return;
        }
        burstTimer = setTimeout(() => {
          ws.send(Buffer.from("~"));
        }, 0);
      },
    });

    try {
      await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
      await page.waitForSelector(".xterm-rows", { timeout: 30_000 });
      await page.locator(".terminal-xterm-host").click({ position: { x: 20, y: 20 } });
      await page.waitForFunction(() => document.activeElement?.classList.contains("xterm-helper-textarea"));
      await expect.poll(
        () => page.evaluate(() => {
          return (window as Window & {
            __terminalWorkerInputOrdering?: { delayingStale: boolean };
          }).__terminalWorkerInputOrdering?.delayingStale ?? false;
        }),
        { timeout: 3000 },
      ).toBe(true);

      await page.keyboard.type("~");
      await page.waitForTimeout(50);
      await page.evaluate(() => {
        (window as Window & {
          __terminalWorkerInputOrdering?: { releaseDelayedStale: null | (() => void) };
        }).__terminalWorkerInputOrdering?.releaseDelayedStale?.();
      });

      await expect.poll(
        () => page.evaluate(() => {
          const state = (window as Window & {
            __terminalWorkerInputOrdering?: { delivered: string[] };
          }).__terminalWorkerInputOrdering;
          return state?.delivered.find((chunk) => chunk.includes("~")) ?? null;
        }),
        { timeout: 3000 },
      ).toBe("~");

      const delivered = await page.evaluate(() => {
        return (window as Window & {
          __terminalWorkerInputOrdering?: { delivered: string[] };
        }).__terminalWorkerInputOrdering?.delivered ?? [];
      });
      const joined = delivered.join("");
      const firstStaleIndex = joined.indexOf("stale-before-input-0");
      const lastStaleIndex = joined.indexOf("stale-before-input-19");
      const echoIndex = joined.indexOf("~");
      expect(firstStaleIndex).toBeGreaterThanOrEqual(0);
      expect(lastStaleIndex).toBeGreaterThan(firstStaleIndex);
      expect(echoIndex).toBeGreaterThan(lastStaleIndex);
    } finally {
      if (burstTimer !== null) {
        clearTimeout(burstTimer);
      }
    }
  });

  test("browser sends server output ack only after terminal write callback", async ({ page }) => {
    await page.addInitScript(() => {
      const NativeWorker = window.Worker;
      const state = {
        ackedBytes: [] as number[],
        serverAckBeforeWrite: false,
        serverAckCount: 0,
        writeHookCalled: false,
      };
      Object.defineProperty(window, "__terminalServerAckFlow", {
        value: state,
        configurable: true,
      });

      (window as Window & {
        __WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__?: (data: string | Uint8Array, parsedAt: number) => void;
      }).__WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__ = () => {
        state.writeHookCalled = true;
      };

      const originalPostMessage = NativeWorker.prototype.postMessage;
      NativeWorker.prototype.postMessage = function postMessage(message: unknown, transfer?: Transferable[]) {
        if (
          typeof message === "object"
          && message !== null
          && "type" in message
          && (message as { type?: unknown }).type === "server-output-ack"
        ) {
          state.serverAckCount += 1;
          const bytes = (message as { bytes?: unknown }).bytes;
          if (typeof bytes === "number") {
            state.ackedBytes.push(bytes);
          }
          if (!state.writeHookCalled) {
            state.serverAckBeforeWrite = true;
          }
        }
        return originalPostMessage.call(this, message, transfer ?? []);
      };
    });

    await mockApi(page, {
      afterTerminalConnected: (ws) => {
        ws.send(Buffer.from("server-ack-after-write\r\n"));
      },
    });

    await page.goto(terminalPath(), { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".xterm-rows", { timeout: 30_000 });

    await expect.poll(
      () => page.evaluate(() => {
        return (window as Window & {
          __terminalServerAckFlow?: { serverAckCount: number };
        }).__terminalServerAckFlow?.serverAckCount ?? 0;
      }),
      { timeout: 3000 },
    ).toBeGreaterThan(0);

    const state = await page.evaluate(() => {
      return (window as Window & {
        __terminalServerAckFlow?: {
          ackedBytes: number[];
          serverAckBeforeWrite: boolean;
          serverAckCount: number;
          writeHookCalled: boolean;
        };
      }).__terminalServerAckFlow;
    });
    expect(state?.writeHookCalled).toBe(true);
    expect(state?.serverAckBeforeWrite).toBe(false);
    expect(state?.ackedBytes.some((bytes) => bytes > 0)).toBe(true);
  });
});
