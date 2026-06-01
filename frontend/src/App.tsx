import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  bootstrapClient,
  createClientRegistrationKey,
  createWindow,
  clearTerminalNotifications,
  deleteClient,
  deleteWindow,
  dismissTerminalNotification,
  fetchAuthStatus,
  fetchClients,
  fetchCustomQuickKeys,
  fetchProjectSummaries,
  fetchTerminalNotifications,
  fetchTree,
  fetchWindow,
  fetchWindowActivity,
  login,
  markTerminalNotificationRead,
  recordTerminalRecent,
  uiEventsWebSocketUrl,
  updateClient,
  updateCustomQuickKeys
} from "./api";
import { AddClientModal } from "./components/AddClientModal";
import { AgentRecordModal } from "./components/AgentRecordViewer";
import { ClientList } from "./components/ClientList";
import { ClientSwitcher } from "./components/ClientSwitcher";
import { FolderTree } from "./components/FolderTree";
import { GitDiffBrowserModal } from "./components/GitDiffBrowserModal";
import { BackendConnectionGate, LoginGate } from "./components/LoginGate";
import { OnboardingTour, type OnboardingAction, type OnboardingStep } from "./components/OnboardingTour";
import { ProjectTerminalPicker } from "./components/ProjectTerminalPicker";
import { SearchPanel } from "./components/SearchPanel";
import { MobileShortcutFab, type MobileShortcutDirection } from "./components/MobileShortcutFab";
import { NotificationBellButton, NotificationCenter } from "./components/NotificationCenter";
import { TerminalPane, type TerminalConnectionStatus, type TerminalPaneHandle } from "./components/TerminalPane";
import {
  TerminalCreateModal,
  type TerminalCreateContext,
  type TerminalCreateSubmit
} from "./components/TerminalCreateModal";
import { useAgentRecordData } from "./hooks/useAgentRecordData";
import { readMobileLayout, useMobileLayout } from "./hooks/useMobileLayout";
import { readInitialSettings, SettingsModal, type SettingsView } from "./components/SettingsModal";
import {
  TerminalSwitcher,
  type TerminalSwitcherMode,
  type TerminalSwitcherRecentScope
} from "./components/TerminalSwitcher";
import { WindowDetail } from "./components/WindowDetail";
import {
  type SummaryOutputLanguage,
  type TerminalGroupingMode,
  type ThemeSkinId
} from "./userPreferences";
import {
  effectiveKeyboardShortcut,
  keyboardShortcutLabel,
  keyboardShortcutMatches,
  readKeyboardShortcutBindings,
  writeKeyboardShortcutBindings,
  type KeyboardShortcutBindings,
  type KeyboardShortcutId
} from "./keyboardShortcuts";
import { terminalThemeForSkin, themeSkinClassName } from "./themeSkins";
import { clearAuthToken, readAuthToken, writeAuthToken } from "./auth";
import { isOnboardingEnabled } from "./onboarding";
import {
  clearLegacyCustomQuickKeys,
  decodeQuickKeyInput,
  normalizeCustomQuickKeys,
  readLegacyCustomQuickKeys,
  type CustomQuickKey
} from "./terminalQuickKeys";
import { ensureDesktopNotificationPermission, showAgentTaskDesktopNotification } from "./desktopNotifications";
import {
  findNewUnreadNotifications,
  normalizeTerminalNotifications,
  type TerminalNotification
} from "./terminalNotifications";
import {
  collectCreatableProjectPaths,
  createWindowInputForGroupNode,
  type SwitcherGroupNode
} from "./terminalGrouping";
import {
  activityHasWorkingTerminal,
  mergeTreeWithActivity,
  windowActivityMap
} from "./terminalTree";
import { terminalRuntimeReadiness, waitForTerminalRuntime } from "./terminalCreateReadiness";
import {
  applyUiInvalidation,
  isActivityOnlyWindowInvalidation,
  nextUiEventReconnectDelay,
  parseUiEvent,
  reserveWindowActivityRefresh,
  scheduleWindowActivityRefresh,
  type UiInvalidateEvent
} from "./uiEvents";
import type {
  AgentLaunchConfig,
  AgentLaunchKind,
  BootstrapClientInput,
  Client,
  TreeFolder,
  VirtualWindow
} from "./types";
import { readApiBase, writeConfiguredApiBase } from "./apiBase";
type TerminalViewportMode = "desktop" | "phone" | "fixed";
type AddClientMode = "bootstrap" | "registration";

const TERMINAL_VIEWPORT_STORAGE_KEY = "web-terminal-acp:terminal-viewport-mode";
const CLIENT_WINDOW_SELECTION_STORAGE_KEY = "web-terminal-acp:client-window-selections";
const CLIENT_RECENCY_STORAGE_KEY = "web-terminal-acp:client-recency";
const TERMINAL_ENTER_INPUT = "\r";
const MOBILE_SHORTCUT_DIRECTION_INPUT: Record<MobileShortcutDirection, string> = {
  up: "\x1b[A",
  down: "\x1b[B",
  left: "\x1b[D",
  right: "\x1b[C"
};

type OnboardingShortcutLabels = {
  settings: string;
  switchTerminal: string;
  newTerminal: string;
  newTerminalProject: string;
  quickInput: string;
};

function buildOnboardingSteps(shortcuts: OnboardingShortcutLabels): OnboardingStep[] {
  return [
    {
      id: "layout",
      title: "先看整体布局",
      body: "左侧管理 Clients 和 Terminals，中间是实时终端，右侧是详情、Agent 记录、历史和搜索。日常主流程基本都在这三栏之间完成。",
      targetId: "app-layout"
    },
    {
      id: "remote-bootstrap",
      title: "添加 remote client：SSH Bootstrap",
      body: "第一种方式在左侧栏 Add client 里。填写目标机器 SSH 信息后，服务端会登录目标机器、上传 remote client，并自动完成注册。适合你能从服务端直接 SSH 到目标机器的场景。",
      path: ["Add client", "SSH Bootstrap"],
      targetId: "remote-bootstrap-form",
      action: "remote-bootstrap"
    },
    {
      id: "remote-registration-menu",
      title: "切到注册 Key 添加方式",
      body: "第二种方式也在 Add client 里。切到 Registration Key，可以生成一次性 Key 和安装脚本。",
      path: ["Add client", "Registration Key"],
      targetId: "add-client-registration-tab",
      action: "remote-registration-menu"
    },
    {
      id: "remote-registration",
      title: "添加 remote client：注册 Key 脚本",
      body: "生成一次性 Key，把脚本命令复制到目标机器运行，由目标机器主动连回服务端。适合目标机器不能被 SSH 直连、但可以访问服务端的场景。",
      path: ["Add client", "Registration Key", "生成一次性注册 Key"],
      targetId: "remote-registration-panel",
      action: "remote-registration"
    },
    {
      id: "clients",
      title: "选择工作机器",
      body: "Clients 里会显示 local 和 remote client。remote client 在线后可以选中并进入对应机器的 terminal 树，离线时仍会保留记录等待重连。",
      targetId: "client-list",
      action: "details"
    },
    {
      id: "terminal-tree",
      title: "Terminal 树是工作入口",
      body: "Terminals 按项目、主题或时间分组。你可以选择已有终端、在分组上新建终端，也可以用“总结”给项目路径生成更易读的名称。",
      targetId: "terminal-tree",
      action: "details"
    },
    {
      id: "new-terminal",
      title: "直接新建普通终端",
      body: "New terminal 会立即创建普通 shell，不再先选择 Agent。需要按项目新建或配置 Agent 时，可以使用项目/分组里的配置入口。",
      path: ["New terminal"],
      shortcutLabels: [shortcuts.newTerminal, `${shortcuts.newTerminalProject} 按项目新建`],
      targetId: "new-terminal-button",
      action: "details"
    },
    {
      id: "terminal-pane",
      title: "中间区域就是终端本体",
      body: "这里显示服务端真实终端输出，也接收键盘输入。Controls 里可以切换桌面、手机或 1920x1080 视图，并打开虚拟按键。",
      path: ["选择任一 terminal"],
      targetId: "terminal-pane",
      action: "details"
    },
    {
      id: "quick-input",
      title: "快速输入适合长文本",
      body: "Quick input 会先把文字写在面板里，确认后一次性发送到终端。它适合移动端输入、多行提示词或需要避免误触回车的内容。",
      path: ["打开任一 terminal", "Quick input"],
      shortcutLabels: [shortcuts.quickInput, "面板内 Ctrl+Enter / Cmd+Enter 发送"],
      targetId: "quick-input-panel",
      action: "quick-input"
    },
    {
      id: "switcher",
      title: "快速切换终端",
      body: "打开最近终端后，再按同一个快捷键会切到项目/主题树。这里可以搜索、分页、展开分组，是多任务时最高频的导航入口。",
      path: ["任意主界面", "Switch terminal"],
      shortcutLabels: [shortcuts.switchTerminal],
      targetId: "terminal-switcher",
      action: "switch-terminal"
    },
    {
      id: "details",
      title: "右侧详情看上下文",
      body: "Overview 看状态、CWD、summary 和 work status；Agent 看对话和事件；History 看命令与标题历史；Git 会在绑定 worktree 后出现。",
      path: ["打开任一 terminal", "Details"],
      targetId: "detail-panel",
      action: "details"
    },
    {
      id: "search",
      title: "搜索历史产物",
      body: "Artifact search 会搜索当前 client 的终端标题、标签、Agent 事件和记录片段，搜索结果可以直接跳回对应 window。",
      path: ["右侧详情", "Artifact search"],
      targetId: "artifact-search",
      action: "details"
    },
    {
      id: "notifications",
      title: "通知用于回到完成的任务",
      body: "Agent 任务完成或中断后会在通知中心出现；点击通知会跳到对应终端。设置里也可以开启系统桌面通知。",
      path: ["顶部通知按钮", "Settings", "系统桌面通知"],
      targetId: "notification-bell",
      action: "details"
    },
    {
      id: "settings",
      title: "最后看设置",
      body: "设置里可以改分组方式、主题、快捷键、快速按键和 Agent 启动命令，也能再次打开新手引导。",
      path: ["Settings"],
      shortcutLabels: [shortcuts.settings],
      targetId: "settings-modal",
      action: "settings"
    }
  ];
}

type TerminalRouteSelection = {
  clientId: string | null;
  windowId: string | null;
};

type ClientWindowSelection = {
  windowId: string | null;
  usedAt: number;
};

type ClientWindowSelections = Record<string, ClientWindowSelection>;

type DeleteWindowVariables = {
  clientId: string;
  windowId: string;
  nextWindowId: string | null;
};

type DeleteClientVariables = {
  clientId: string;
};

type CreateWindowVariables = {
  clientId: string;
  cwd?: string | null;
  folder_path?: string | null;
  agent_launch?: AgentLaunchConfig | null;
  afterCreate?: () => void;
};

type PendingTerminalCreate = {
  clientId: string;
  windowId: string;
  title: string;
};

function terminalStatusLabel(status: TerminalConnectionStatus): string {
  switch (status) {
    case "connected":
      return "Terminal connected";
    case "connecting":
      return "Terminal connecting...";
    case "reconnecting":
      return "Terminal reconnecting...";
    case "unavailable":
      return "Client offline";
    case "error":
      return "Terminal error";
  }
}

function isTerminalViewportMode(value: string | null): value is TerminalViewportMode {
  return value === "desktop" || value === "phone" || value === "fixed";
}

function readTerminalViewportMode(): TerminalViewportMode {
  if (typeof window === "undefined") {
    return "desktop";
  }

  const storedMode = window.localStorage.getItem(TERMINAL_VIEWPORT_STORAGE_KEY);
  return isTerminalViewportMode(storedMode) ? storedMode : "desktop";
}

function isXtermInput(element: EventTarget | null): boolean {
  if (!(element instanceof HTMLElement)) {
    return false;
  }

  return element.classList.contains("xterm-helper-textarea") || element.closest(".xterm") !== null;
}

function isBlockingTextInput(element: EventTarget | null): boolean {
  if (!(element instanceof HTMLElement) || isXtermInput(element)) {
    return false;
  }

  const tag = element.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || element.isContentEditable;
}

function isGlobalShortcutTextTarget(element: EventTarget | null): boolean {
  if (!(element instanceof HTMLElement) || isXtermInput(element)) {
    return false;
  }

  if (element.closest(".terminal-quick-input-panel") !== null) {
    return false;
  }

  return isBlockingTextInput(element);
}

function findTreeWindow(
  folders: TreeFolder[] | undefined,
  windowId: string | null
): TreeFolder["windows"][number] | null {
  if (!folders || windowId === null) {
    return null;
  }

  for (const folder of folders) {
    const window = folder.windows.find((candidate) => candidate.id === windowId);
    if (window) {
      return window;
    }

    const childWindow = findTreeWindow(folder.folders, windowId);
    if (childWindow) {
      return childWindow;
    }
  }

  return null;
}

function findWindowTitle(folders: TreeFolder[] | undefined, windowId: string | null): string | null {
  if (!folders || windowId === null) {
    return null;
  }

  return findTreeWindow(folders, windowId)?.title ?? null;
}

function flattenTreeWindows(folders: TreeFolder[] | undefined): TreeFolder["windows"][number][] {
  if (!folders) {
    return [];
  }

  const windows: TreeFolder["windows"][number][] = [];
  const visit = (folder: TreeFolder) => {
    windows.push(...folder.windows);
    for (const child of folder.folders) {
      visit(child);
    }
  };
  for (const folder of folders) {
    visit(folder);
  }
  return windows;
}

function pickWindowAfterDelete(folders: TreeFolder[] | undefined, deletedWindowId: string): string | null {
  const windows = flattenTreeWindows(folders);
  const index = windows.findIndex((window) => window.id === deletedWindowId);
  if (windows.length <= 1) {
    return null;
  }
  if (index === -1) {
    return windows[0]?.id ?? null;
  }
  const nextWindow = windows[index + 1] ?? windows[index - 1];
  return nextWindow?.id ?? null;
}

function treeContainsWindow(folders: TreeFolder[] | undefined, windowId: string | null): boolean {
  if (!folders || windowId === null) {
    return false;
  }

  for (const folder of folders) {
    if (folder.windows.some((window) => window.id === windowId)) {
      return true;
    }

    if (treeContainsWindow(folder.folders, windowId)) {
      return true;
    }
  }

  return false;
}

function readTerminalRouteSelection(): TerminalRouteSelection {
  if (typeof window === "undefined") {
    return { clientId: null, windowId: null };
  }

  const clientTerminalMatch = window.location.pathname.match(/^\/clients\/([^/]+)\/terminals\/([^/]+)\/?$/);
  if (clientTerminalMatch) {
    return {
      clientId: decodeURIComponent(clientTerminalMatch[1]),
      windowId: decodeURIComponent(clientTerminalMatch[2])
    };
  }

  const clientMatch = window.location.pathname.match(/^\/clients\/([^/]+)\/?$/);
  if (clientMatch) {
    return {
      clientId: decodeURIComponent(clientMatch[1]),
      windowId: null
    };
  }

  return { clientId: null, windowId: null };
}

function terminalRoutePath(clientId: string | null, windowId: string | null): string {
  if (clientId === null) {
    return "/";
  }

  const encodedClientId = encodeURIComponent(clientId);
  if (windowId === null) {
    return `/clients/${encodedClientId}`;
  }

  return `/clients/${encodedClientId}/terminals/${encodeURIComponent(windowId)}`;
}

function writeTerminalRoute(clientId: string | null, windowId: string | null, mode: "push" | "replace") {
  if (typeof window === "undefined") {
    return;
  }

  const nextPath = terminalRoutePath(clientId, windowId);
  if (`${window.location.pathname}${window.location.search}${window.location.hash}` === nextPath) {
    return;
  }

  const method = mode === "push" ? "pushState" : "replaceState";
  window.history[method]({ clientId, windowId }, "", nextPath);
}

function readJsonObjectStorage(key: string): Record<string, unknown> {
  if (typeof window === "undefined") {
    return {};
  }

  try {
    const parsed = JSON.parse(window.localStorage.getItem(key) ?? "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

function readClientWindowSelections(): ClientWindowSelections {
  const parsed = readJsonObjectStorage(CLIENT_WINDOW_SELECTION_STORAGE_KEY);
  const selections: ClientWindowSelections = {};
  for (const [clientId, value] of Object.entries(parsed)) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      continue;
    }
    const record = value as Record<string, unknown>;
    selections[clientId] = {
      windowId: typeof record.windowId === "string" ? record.windowId : null,
      usedAt: typeof record.usedAt === "number" && Number.isFinite(record.usedAt) ? record.usedAt : 0
    };
  }
  return selections;
}

function writeClientWindowSelections(selections: ClientWindowSelections): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(CLIENT_WINDOW_SELECTION_STORAGE_KEY, JSON.stringify(selections));
}

function readClientRecency(): string[] {
  if (typeof window === "undefined") {
    return [];
  }

  try {
    const parsed = JSON.parse(window.localStorage.getItem(CLIENT_RECENCY_STORAGE_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === "string") : [];
  } catch {
    return [];
  }
}

function writeClientRecency(clientIds: string[]): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(CLIENT_RECENCY_STORAGE_KEY, JSON.stringify(clientIds));
}

function rememberClientRecency(clientIds: string[], clientId: string): string[] {
  return [clientId, ...clientIds.filter((candidate) => candidate !== clientId)].slice(0, 100);
}

function isRemoteClientOffline(client: Client | null | undefined): boolean {
  if (client === null || client === undefined) {
    return false;
  }

  return client.runtime === "remote" && client.status !== "ONLINE";
}

function useVisualViewportHeightCssVariable(): void {
  useEffect(() => {
    const root = document.documentElement;
    let frame: number | null = null;
    let lastHeight = "";

    const sync = () => {
      frame = null;
      // Some mobile keyboards shrink visualViewport without changing the layout viewport used by 100dvh.
      const viewportHeight = window.visualViewport?.height ?? window.innerHeight;
      if (viewportHeight <= 0) {
        return;
      }

      const nextHeight = `${Math.round(viewportHeight)}px`;
      if (nextHeight === lastHeight) {
        return;
      }

      lastHeight = nextHeight;
      root.style.setProperty("--web-terminal-viewport-height", nextHeight);
    };

    const scheduleSync = () => {
      if (frame !== null) {
        return;
      }
      frame = window.requestAnimationFrame(sync);
    };

    sync();
    window.visualViewport?.addEventListener("resize", scheduleSync);
    window.visualViewport?.addEventListener("scroll", scheduleSync);
    window.addEventListener("resize", scheduleSync);
    window.addEventListener("orientationchange", scheduleSync);
    return () => {
      if (frame !== null) {
        window.cancelAnimationFrame(frame);
      }
      window.visualViewport?.removeEventListener("resize", scheduleSync);
      window.visualViewport?.removeEventListener("scroll", scheduleSync);
      window.removeEventListener("resize", scheduleSync);
      window.removeEventListener("orientationchange", scheduleSync);
      root.style.removeProperty("--web-terminal-viewport-height");
    };
  }, []);
}

export default function App() {
  const [authToken, setAuthToken] = useState<string | null>(readAuthToken);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [apiBase, setApiBase] = useState(readApiBase);
  const [apiBaseRevision, setApiBaseRevision] = useState(0);
  const [apiBaseError, setApiBaseError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const authStatusQuery = useQuery({
    queryKey: ["auth-status", apiBase, apiBaseRevision],
    queryFn: fetchAuthStatus,
    retry: false
  });

  const loginMutation = useMutation({
    mutationFn: login,
    onSuccess: (result) => {
      writeAuthToken(result.token);
      setAuthToken(result.token);
      setLoginError(null);
      queryClient.invalidateQueries();
    },
    onError: () => {
      clearAuthToken();
      setAuthToken(null);
      setLoginError("登录密钥不正确");
    }
  });

  const logout = useCallback(() => {
    clearAuthToken();
    setAuthToken(null);
    queryClient.clear();
  }, [queryClient]);

  const saveBackendAddress = useCallback((value: string) => {
    try {
      writeConfiguredApiBase(value);
      const nextApiBase = readApiBase();
      clearAuthToken();
      setAuthToken(null);
      setApiBase(nextApiBase);
      setApiBaseRevision((revision) => revision + 1);
      setApiBaseError(null);
      setLoginError(null);
      queryClient.clear();
    } catch {
      setApiBaseError("请输入有效的 HTTP/HTTPS 后端地址");
    }
  }, [queryClient]);

  const submitLogin = useCallback(async (secret: string) => {
    await loginMutation.mutateAsync(secret);
  }, [loginMutation]);

  const authRequired = authStatusQuery.data?.enabled === true;
  const authReady = authStatusQuery.isSuccess && (!authRequired || authToken !== null);

  if (authStatusQuery.isLoading) {
    return (
      <main className="login-shell">
        <p className="muted">Loading...</p>
      </main>
    );
  }

  if (authStatusQuery.isError) {
    return (
      <BackendConnectionGate
        backendAddress={apiBase}
        backendAddressError={apiBaseError}
        connectionError={authStatusQuery.error instanceof Error ? authStatusQuery.error.message : null}
        isCheckingBackend={authStatusQuery.isFetching}
        onSaveBackendAddress={saveBackendAddress}
      />
    );
  }

  if (!authReady) {
    return (
      <LoginGate
        backendAddress={apiBase}
        backendAddressError={apiBaseError}
        error={loginError}
        isCheckingBackend={authStatusQuery.isFetching}
        isSubmitting={loginMutation.isPending}
        onSaveBackendAddress={saveBackendAddress}
        onSubmit={submitLogin}
      />
    );
  }

  return <AuthenticatedApp authEnabled={authRequired} onLogout={logout} />;
}

function AuthenticatedApp({
  authEnabled,
  onLogout
}: {
  authEnabled: boolean;
  onLogout: () => void;
}) {
  useVisualViewportHeightCssVariable();

  const [selectedClientId, setSelectedClientId] = useState<string | null>(null);
  const [selectedWindowId, setSelectedWindowId] = useState<string | null>(null);
  const [routeSelectionRequest, setRouteSelectionRequest] = useState<TerminalRouteSelection | null>(
    readTerminalRouteSelection
  );
  const [addClientModalOpen, setAddClientModalOpen] = useState(false);
  const [addClientInitialMode, setAddClientInitialMode] = useState<AddClientMode>("bootstrap");
  const [bootstrapFailed, setBootstrapFailed] = useState(false);
  const [updateMessage, setUpdateMessage] = useState<string | null>(null);
  const [updateFailed, setUpdateFailed] = useState(false);
  const [terminalSwitcherOpen, setTerminalSwitcherOpen] = useState(false);
  const [terminalSwitcherMode, setTerminalSwitcherMode] = useState<TerminalSwitcherMode>("recent");
  const [terminalSwitcherRecentScope, setTerminalSwitcherRecentScope] = useState<TerminalSwitcherRecentScope>("client");
  const [clientSwitcherOpen, setClientSwitcherOpen] = useState(false);
  const [clientWindowSelections, setClientWindowSelections] = useState<ClientWindowSelections>(
    readClientWindowSelections
  );
  const [recentClientIds, setRecentClientIds] = useState<string[]>(readClientRecency);
  const [projectTerminalPickerOpen, setProjectTerminalPickerOpen] = useState(false);
  const [terminalCreateContext, setTerminalCreateContext] = useState<TerminalCreateContext | null>(null);
  const [mobileTerminalActive, setMobileTerminalActive] = useState(false);
  const [detailPanelOpen, setDetailPanelOpen] = useState(false);
  const [terminalViewportMode, setTerminalViewportMode] = useState<TerminalViewportMode>(readTerminalViewportMode);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsInitialView, setSettingsInitialView] = useState<SettingsView>("general");
  const [summaryOutputLanguage, setSummaryOutputLanguage] = useState<SummaryOutputLanguage>(
    () => readInitialSettings().summaryOutputLanguage
  );
  const [terminalGroupingMode, setTerminalGroupingMode] = useState<TerminalGroupingMode>(
    () => readInitialSettings().terminalGroupingMode
  );
  const [themeSkin, setThemeSkin] = useState<ThemeSkinId>(
    () => readInitialSettings().themeSkin
  );
  const [desktopNotificationsEnabled, setDesktopNotificationsEnabled] = useState(
    () => readInitialSettings().desktopNotificationsEnabled
  );
  const [keyboardShortcutBindings, setKeyboardShortcutBindings] = useState<KeyboardShortcutBindings>(
    () => readKeyboardShortcutBindings()
  );
  const [customQuickKeys, setCustomQuickKeys] = useState<CustomQuickKey[]>([]);
  const [terminalControlsOpen, setTerminalControlsOpen] = useState(false);
  const [virtualKeysVisible, setVirtualKeysVisible] = useState(
    () => readMobileLayout() || terminalViewportMode === "phone"
  );
  const [terminalQuickInputOpen, setTerminalQuickInputOpen] = useState(false);
  const [terminalQuickInputDraft, setTerminalQuickInputDraft] = useState("");
  const [terminalConnectionStatus, setTerminalConnectionStatus] = useState<TerminalConnectionStatus>("connecting");
  const [terminalImmersive, setTerminalImmersive] = useState(false);
  const [notificationCenterOpen, setNotificationCenterOpen] = useState(false);
  const [gitDiffBrowserOpen, setGitDiffBrowserOpen] = useState(false);
  const [agentRecordModalOpen, setAgentRecordModalOpen] = useState(false);
  const [pendingTerminalCreate, setPendingTerminalCreate] = useState<PendingTerminalCreate | null>(null);
  const [deferredTreeSelection, setDeferredTreeSelection] = useState<TerminalRouteSelection | null>(null);
  const [terminalListLocateSignal, setTerminalListLocateSignal] = useState(0);
  const [terminalNotifications, setTerminalNotifications] = useState<TerminalNotification[]>([]);
  const notificationPreviousRef = useRef<TerminalNotification[]>([]);
  const notificationHydratedClientRef = useRef<string | null>(null);
  const virtualKeysPreferenceTouchedRef = useRef(false);
  const terminalControlsRef = useRef<HTMLDivElement | null>(null);
  const terminalPaneRef = useRef<TerminalPaneHandle | null>(null);
  const terminalCreateWaitRef = useRef(0);
  const queryClient = useQueryClient();

  const registrationKeyMutation = useMutation({
    mutationFn: (label?: string | null) => createClientRegistrationKey(label),
  });

  const agentRecordModal = useAgentRecordData({
    clientId: selectedClientId,
    windowId: selectedWindowId,
    enabled: agentRecordModalOpen
  });

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    const delayedRefreshTimers = new Map<string, number>();
    const lastActivityRefetchAt = new Map<string, number>();
    let reconnectAttempt = 0;
    let closed = false;

    const clearReconnectTimer = () => {
      if (reconnectTimer === null) {
        return;
      }
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    };

    const clearDelayedRefreshTimers = () => {
      for (const timer of delayedRefreshTimers.values()) {
        window.clearTimeout(timer);
      }
      delayedRefreshTimers.clear();
    };

    const scheduleReconnect = () => {
      if (closed || reconnectTimer !== null) {
        return;
      }
      reconnectAttempt += 1;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, nextUiEventReconnectDelay(reconnectAttempt));
    };

    const trackDelayedRefresh = (clientId: string | null, timer: number | null) => {
      if (clientId === null || timer === null) {
        return;
      }
      const previousTimer = delayedRefreshTimers.get(clientId);
      if (previousTimer !== undefined) {
        window.clearTimeout(previousTimer);
      }
      delayedRefreshTimers.set(clientId, timer);
    };

    const applyWindowActivityInvalidation = (event: UiInvalidateEvent) => {
      if (event.client_id === null || !event.resources.includes("window")) {
        return;
      }
      if (!reserveWindowActivityRefresh(event, lastActivityRefetchAt)) {
        return;
      }
      void queryClient.invalidateQueries({ queryKey: ["window-activity", event.client_id] });
      trackDelayedRefresh(event.client_id, scheduleWindowActivityRefresh(queryClient, event, (timer) => {
        if (event.client_id !== null && delayedRefreshTimers.get(event.client_id) === timer) {
          delayedRefreshTimers.delete(event.client_id);
        }
      }));
    };

    const connect = () => {
      if (closed) {
        return;
      }
      if (socket !== null) {
        socket.onclose = null;
        socket.onerror = null;
        socket.onmessage = null;
        socket.close();
      }
      const nextSocket = new WebSocket(uiEventsWebSocketUrl());
      socket = nextSocket;
      nextSocket.onopen = () => {
        if (socket !== nextSocket) {
          return;
        }
        reconnectAttempt = 0;
      };
      nextSocket.onmessage = (message) => {
        if (socket !== nextSocket) {
          return;
        }
        if (typeof message.data !== "string") {
          return;
        }
        const event = parseUiEvent(message.data);
        if (event?.type !== "invalidate") {
          return;
        }
        applyUiInvalidation(queryClient, event);
        if (isActivityOnlyWindowInvalidation(event)) {
          applyWindowActivityInvalidation(event);
        } else {
          trackDelayedRefresh(event.client_id, scheduleWindowActivityRefresh(queryClient, event, (timer) => {
            if (event.client_id !== null && delayedRefreshTimers.get(event.client_id) === timer) {
              delayedRefreshTimers.delete(event.client_id);
            }
          }));
        }
      };
      nextSocket.onclose = () => {
        if (socket !== nextSocket) {
          return;
        }
        scheduleReconnect();
      };
      nextSocket.onerror = () => {
        nextSocket.close();
      };
    };

    connect();
    return () => {
      closed = true;
      clearReconnectTimer();
      clearDelayedRefreshTimers();
      socket?.close();
    };
  }, [queryClient]);

  const persistTerminalRecent = useCallback((clientId: string, windowId: string, title: string) => {
    void recordTerminalRecent(clientId, { window_id: windowId, title })
      .then(() => {
        queryClient.invalidateQueries({ queryKey: ["terminal-recents", clientId] });
        queryClient.invalidateQueries({ queryKey: ["terminal-recents", "global"] });
      })
      .catch(() => {});
  }, [queryClient]);

  const rememberClientWindowSelection = useCallback((clientId: string, windowId: string | null) => {
    setClientWindowSelections((currentSelections) => {
      const nextSelections = {
        ...currentSelections,
        [clientId]: { windowId, usedAt: Date.now() }
      };
      writeClientWindowSelections(nextSelections);
      return nextSelections;
    });
  }, []);

  const rememberClientUse = useCallback((clientId: string) => {
    setRecentClientIds((currentClientIds) => {
      const nextClientIds = rememberClientRecency(currentClientIds, clientId);
      writeClientRecency(nextClientIds);
      return nextClientIds;
    });
  }, []);

  const focusSelectedTerminal = useCallback(() => {
    requestAnimationFrame(() => {
      terminalPaneRef.current?.refit();
      terminalPaneRef.current?.focus();
    });
  }, []);

  const submitAgentPreviewQuickInput = useCallback((draft: string) => {
    if (draft.length === 0) {
      return false;
    }

    return terminalPaneRef.current?.submitQuickInput(`${draft}${TERMINAL_ENTER_INPUT}`) ?? false;
  }, []);

  const customQuickKeysQuery = useQuery({
    queryKey: ["custom-quick-keys"],
    queryFn: fetchCustomQuickKeys
  });
  const {
    mutate: mutateCustomQuickKeys,
    isPending: customQuickKeysUpdatePending
  } = useMutation({
    mutationFn: updateCustomQuickKeys,
    onSuccess: (result) => {
      setCustomQuickKeys(normalizeCustomQuickKeys(result.quick_keys));
      queryClient.setQueryData(["custom-quick-keys"], result);
      clearLegacyCustomQuickKeys();
    }
  });

  useEffect(() => {
    if (!customQuickKeysQuery.isSuccess) {
      return;
    }

    const serverQuickKeys = normalizeCustomQuickKeys(customQuickKeysQuery.data.quick_keys);
    if (customQuickKeysUpdatePending) {
      return;
    }
    setCustomQuickKeys(serverQuickKeys);
    if (serverQuickKeys.length > 0) {
      return;
    }

    const legacyQuickKeys = readLegacyCustomQuickKeys();
    if (legacyQuickKeys.length === 0) {
      return;
    }
    setCustomQuickKeys(legacyQuickKeys);
    mutateCustomQuickKeys(legacyQuickKeys);
  }, [
    customQuickKeysQuery.data,
    customQuickKeysQuery.isSuccess,
    customQuickKeysUpdatePending,
    mutateCustomQuickKeys
  ]);

  const handleCustomQuickKeysChange = useCallback((quickKeys: CustomQuickKey[]) => {
    const normalizedQuickKeys = normalizeCustomQuickKeys(quickKeys);
    setCustomQuickKeys(normalizedQuickKeys);
    mutateCustomQuickKeys(normalizedQuickKeys);
  }, [mutateCustomQuickKeys]);

  const submitCustomQuickKey = useCallback((quickKey: CustomQuickKey) => {
    return terminalPaneRef.current?.submitQuickInput(decodeQuickKeyInput(quickKey.input)) ?? false;
  }, []);

  const handleKeyboardShortcutBindingsChange = useCallback((bindings: KeyboardShortcutBindings) => {
    setKeyboardShortcutBindings(bindings);
    writeKeyboardShortcutBindings(bindings);
  }, []);

  const submitMobileShortcutDirection = useCallback((direction: MobileShortcutDirection) => {
    return terminalPaneRef.current?.submitQuickInput(MOBILE_SHORTCUT_DIRECTION_INPUT[direction]) ?? false;
  }, []);

  const closeTerminalSwitcher = useCallback(() => {
    setTerminalSwitcherOpen(false);
    focusSelectedTerminal();
  }, [focusSelectedTerminal]);
  const toggleTerminalSwitcherMode = useCallback(() => {
    setTerminalSwitcherMode((currentMode) => (currentMode === "recent" ? "tree" : "recent"));
  }, []);
  const closeClientSwitcher = useCallback(() => {
    setClientSwitcherOpen(false);
    focusSelectedTerminal();
  }, [focusSelectedTerminal]);
  const isMobileLayout = useMobileLayout();

  useEffect(() => {
    if (virtualKeysPreferenceTouchedRef.current) {
      return;
    }

    setVirtualKeysVisible(isMobileLayout || terminalViewportMode === "phone");
  }, [isMobileLayout, terminalViewportMode]);

  const toggleVirtualKeysVisibility = useCallback(() => {
    virtualKeysPreferenceTouchedRef.current = true;
    setVirtualKeysVisible((isVisible) => !isVisible);
  }, []);

  const clientsQuery = useQuery({ queryKey: ["clients"], queryFn: fetchClients, refetchInterval: 10000 });
  const treeQuery = useQuery({
    queryKey: ["tree", selectedClientId],
    queryFn: () => fetchTree(selectedClientId as string),
    enabled: selectedClientId !== null,
    refetchInterval: 10000
  });
  const windowActivityQuery = useQuery({
    queryKey: ["window-activity", selectedClientId],
    queryFn: () => fetchWindowActivity(selectedClientId as string, { includeRuntimeTags: true }),
    enabled: selectedClientId !== null && treeQuery.isSuccess,
    refetchInterval: (query) => (activityHasWorkingTerminal(query.state.data) ? 3000 : 10000)
  });
  const lastNotificationActivitySignature = useMemo(() => {
    if (!windowActivityQuery.data) {
      return "";
    }

    return windowActivityQuery.data.windows
      .map((window) => [
        window.window_id,
        window.last_agent_task_status ?? "",
        window.last_agent_task_status_at ?? window.last_agent_task_completed_at ?? ""
      ].join(":"))
      .sort()
      .join("|");
  }, [windowActivityQuery.data]);
  const terminalNotificationsQuery = useQuery({
    queryKey: ["terminal-notifications", selectedClientId, lastNotificationActivitySignature],
    queryFn: () => fetchTerminalNotifications(selectedClientId as string),
    enabled: selectedClientId !== null && windowActivityQuery.isSuccess,
    refetchInterval: 10000
  });
  const treeFolders = useMemo(
    () => mergeTreeWithActivity(treeQuery.data, windowActivityMap(windowActivityQuery.data)),
    [treeQuery.data, windowActivityQuery.data]
  );
  const selectedClient = clientsQuery.data?.find((client) => client.id === selectedClientId) ?? null;
  const selectedClientOffline = isRemoteClientOffline(selectedClient);
  const projectPaths = useMemo(
    () => collectCreatableProjectPaths(treeFolders ?? []),
    [treeFolders]
  );
  const terminalTheme = useMemo(() => terminalThemeForSkin(themeSkin), [themeSkin]);
  const projectSummariesQuery = useQuery({
    queryKey: ["project-summaries", selectedClientId],
    queryFn: () => fetchProjectSummaries(selectedClientId as string),
    enabled: selectedClientId !== null && projectTerminalPickerOpen
  });
  const selectedTreeWindow = findTreeWindow(treeFolders, selectedWindowId);
  const selectedWindowTitle = selectedTreeWindow?.title ?? null;
  const selectCreatedTerminal = useCallback((window: VirtualWindow, mode: "push" | "replace" = "push") => {
    setSelectedClientId(window.client_id);
    setSelectedWindowId(window.id);
    rememberClientUse(window.client_id);
    rememberClientWindowSelection(window.client_id, window.id);
    setRouteSelectionRequest(null);
    setDeferredTreeSelection({ clientId: window.client_id, windowId: window.id });
    setMobileTerminalActive(true);
    setAgentRecordModalOpen(false);
    writeTerminalRoute(window.client_id, window.id, mode);
    persistTerminalRecent(window.client_id, window.id, window.title);
    focusSelectedTerminal();
  }, [focusSelectedTerminal, persistTerminalRecent, rememberClientUse, rememberClientWindowSelection]);
  const createMutation = useMutation({
    mutationFn: (variables: CreateWindowVariables) =>
      createWindow(variables.clientId, {
        cwd: variables.cwd,
        folder_path: variables.folder_path,
        agent_launch: variables.agent_launch
      }),
    onSuccess: async (window, variables) => {
      const waitId = terminalCreateWaitRef.current + 1;
      terminalCreateWaitRef.current = waitId;
      queryClient.invalidateQueries({ queryKey: ["tree", window.client_id] });
      queryClient.invalidateQueries({ queryKey: ["window-activity", window.client_id] });
      queryClient.setQueryData(["window", window.client_id, window.id], window);

      let createSurfacesClosed = false;
      const closeCreateSurfaces = () => {
        if (createSurfacesClosed) {
          return;
        }
        createSurfacesClosed = true;
        setProjectTerminalPickerOpen(false);
        setTerminalCreateContext(null);
        variables.afterCreate?.();
      };

      try {
        const shouldWaitForRuntime = terminalRuntimeReadiness(window) === "pending";
        if (shouldWaitForRuntime) {
          setPendingTerminalCreate({
            clientId: window.client_id,
            windowId: window.id,
            title: window.title
          });
          setMobileTerminalActive(true);
          closeCreateSurfaces();
        }
        const readyWindow = shouldWaitForRuntime
          ? await waitForTerminalRuntime(window, fetchWindow)
          : window;
        if (terminalCreateWaitRef.current !== waitId) {
          return;
        }

        queryClient.setQueryData(["window", readyWindow.client_id, readyWindow.id], readyWindow);
        queryClient.invalidateQueries({ queryKey: ["tree", readyWindow.client_id] });
        queryClient.invalidateQueries({ queryKey: ["window-activity", readyWindow.client_id] });
        selectCreatedTerminal(readyWindow);
        closeCreateSurfaces();
      } catch {
        if (terminalCreateWaitRef.current === waitId) {
          selectCreatedTerminal(window);
          closeCreateSurfaces();
        }
      } finally {
        if (terminalCreateWaitRef.current === waitId) {
          setPendingTerminalCreate(null);
        }
      }
    },
    onError: () => {
      terminalCreateWaitRef.current += 1;
      setPendingTerminalCreate(null);
    }
  });
  const terminalCreateBusy = createMutation.isPending || pendingTerminalCreate !== null;
  const deleteMutation = useMutation({
    mutationFn: ({ clientId, windowId }: DeleteWindowVariables) => deleteWindow(clientId, windowId),
    onSuccess: (_result, { clientId, windowId, nextWindowId }) => {
      queryClient.invalidateQueries({ queryKey: ["tree", clientId] });
      queryClient.invalidateQueries({ queryKey: ["window-activity", clientId] });
      queryClient.invalidateQueries({ queryKey: ["terminal-recents", "global"] });
      setTerminalNotifications((current) => current.filter((notification) => notification.windowId !== windowId));
      setSelectedWindowId((currentWindowId) => {
        if (currentWindowId !== windowId) {
          return currentWindowId;
        }

        setDeferredTreeSelection(null);
        setRouteSelectionRequest(null);
        rememberClientWindowSelection(clientId, nextWindowId);
        writeTerminalRoute(clientId, nextWindowId, "replace");
        if (nextWindowId === null) {
          setMobileTerminalActive(false);
          setDetailPanelOpen(false);
          setTerminalImmersive(false);
          setAgentRecordModalOpen(false);
        }
        return nextWindowId;
      });
    }
  });
  const deleteClientMutation = useMutation({
    mutationFn: ({ clientId }: DeleteClientVariables) => deleteClient(clientId),
    onSuccess: (_result, { clientId }) => {
      queryClient.removeQueries({ queryKey: ["tree", clientId] });
      queryClient.removeQueries({ queryKey: ["window-activity", clientId] });
      queryClient.removeQueries({ queryKey: ["terminal-notifications", clientId] });
      queryClient.removeQueries({ queryKey: ["terminal-recents", clientId] });
      queryClient.invalidateQueries({ queryKey: ["terminal-recents", "global"] });
      queryClient.removeQueries({ queryKey: ["project-summaries", clientId] });
      queryClient.invalidateQueries({ queryKey: ["clients"] });
      setTerminalNotifications((current) => current.filter((notification) => notification.clientId !== clientId));
      notificationPreviousRef.current = notificationPreviousRef.current.filter(
        (notification) => notification.clientId !== clientId
      );
      setClientWindowSelections((currentSelections) => {
        const nextSelections = { ...currentSelections };
        delete nextSelections[clientId];
        writeClientWindowSelections(nextSelections);
        return nextSelections;
      });
      setRecentClientIds((currentClientIds) => {
        const nextClientIds = currentClientIds.filter((candidate) => candidate !== clientId);
        writeClientRecency(nextClientIds);
        return nextClientIds;
      });
      if (selectedClientId !== clientId) {
        return;
      }

      setRouteSelectionRequest(null);
      setDeferredTreeSelection(null);
      setSelectedClientId(null);
      setSelectedWindowId(null);
      setMobileTerminalActive(false);
      setDetailPanelOpen(false);
      setTerminalImmersive(false);
      setAgentRecordModalOpen(false);
      writeTerminalRoute(null, null, "replace");
    }
  });
  const bootstrapMutation = useMutation({
    mutationFn: bootstrapClient,
    onMutate: () => {
      setBootstrapFailed(false);
    },
    onSuccess: () => {
      setAddClientModalOpen(false);
      queryClient.invalidateQueries({ queryKey: ["clients"] });
    },
    onError: () => {
      setBootstrapFailed(true);
    },
    onSettled: () => {
      bootstrapMutation.reset();
    }
  });
  const updateMutation = useMutation({
    mutationFn: updateClient,
    onMutate: () => {
      setUpdateFailed(false);
      setUpdateMessage(null);
    },
    onSuccess: (result) => {
      setUpdateMessage(`Client update started (${result.method}).`);
      queryClient.invalidateQueries({ queryKey: ["clients"] });
    },
    onError: () => {
      setUpdateFailed(true);
    }
  });

  useEffect(() => {
    const handlePopState = () => {
      setRouteSelectionRequest(readTerminalRouteSelection());
    };

    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    const clients = clientsQuery.data;
    if (!clients) {
      return;
    }

    if (clients.length === 0) {
      if (selectedClientId !== null) {
        setSelectedClientId(null);
        setSelectedWindowId(null);
        setDeferredTreeSelection(null);
        setAgentRecordModalOpen(false);
        writeTerminalRoute(null, null, "replace");
      }
      return;
    }

    if (routeSelectionRequest !== null) {
      const requestedClient = routeSelectionRequest.clientId === null
        ? null
        : clients.find((client) => client.id === routeSelectionRequest.clientId) ?? null;
      const currentClient = selectedClientId === null
        ? null
        : clients.find((client) => client.id === selectedClientId) ?? null;
      const nextClient = requestedClient ?? currentClient ?? clients.find((client) => client.runtime === "local") ?? clients[0];
      setSelectedClientId(nextClient.id);
      rememberClientUse(nextClient.id);
      const nextWindowId = requestedClient
        ? routeSelectionRequest.windowId
        : clientWindowSelections[nextClient.id]?.windowId ?? null;
      setSelectedWindowId(nextWindowId);
      setDeferredTreeSelection(null);
      setAgentRecordModalOpen(false);
      if (requestedClient === null && routeSelectionRequest.clientId !== null) {
        writeTerminalRoute(nextClient.id, nextWindowId, "replace");
      }
      setRouteSelectionRequest(null);
      return;
    }

    if (selectedClientId !== null && clients.some((client) => client.id === selectedClientId)) {
      return;
    }

    const preferredClient = clients.find((client) => client.runtime === "local") ?? clients[0];
    setSelectedClientId(preferredClient.id);
    rememberClientUse(preferredClient.id);
    setSelectedWindowId(clientWindowSelections[preferredClient.id]?.windowId ?? null);
    setDeferredTreeSelection(null);
    setAgentRecordModalOpen(false);
  }, [clientWindowSelections, clientsQuery.data, rememberClientUse, routeSelectionRequest, selectedClientId]);

  const triggerTerminalSwitcherShortcut = useCallback((scope: TerminalSwitcherRecentScope = "client") => {
    if (terminalSwitcherOpen) {
      setTerminalSwitcherRecentScope(scope);
      if (terminalSwitcherRecentScope !== scope) {
        setTerminalSwitcherMode("recent");
      } else {
        toggleTerminalSwitcherMode();
      }
      return;
    }

    setTerminalSwitcherMode("recent");
    setTerminalSwitcherRecentScope(scope);
    setTerminalSwitcherOpen(true);
  }, [terminalSwitcherOpen, terminalSwitcherRecentScope, toggleTerminalSwitcherMode]);

  const triggerGlobalTerminalSwitcherShortcut = useCallback(() => {
    triggerTerminalSwitcherShortcut("global");
  }, [triggerTerminalSwitcherShortcut]);

  const triggerClientSwitcherShortcut = useCallback(() => {
    setTerminalSwitcherOpen(false);
    setProjectTerminalPickerOpen(false);
    setNotificationCenterOpen(false);
    setGitDiffBrowserOpen(false);
    setTerminalControlsOpen(false);
    setTerminalCreateContext(null);
    setClientSwitcherOpen(true);
  }, []);

  const triggerNewTerminalShortcut = useCallback(() => {
    if (selectedClientId === null || terminalCreateBusy) {
      return;
    }

    const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
    if (isRemoteClientOffline(client)) {
      return;
    }

    createMutation.mutate({
      clientId: selectedClientId,
      agent_launch: null
    });
  }, [clientsQuery.data, createMutation, selectedClientId, terminalCreateBusy]);

  const triggerNewTerminalByProjectShortcut = useCallback(() => {
    if (selectedClientId === null || terminalCreateBusy) {
      return;
    }

    const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
    if (isRemoteClientOffline(client)) {
      return;
    }

    setProjectTerminalPickerOpen(true);
    setTerminalSwitcherOpen(false);
  }, [clientsQuery.data, selectedClientId, terminalCreateBusy]);

  const handleCreateTerminalAtProjectPath = useCallback((projectPath: string, agentLaunch: AgentLaunchConfig | null) => {
    if (selectedClientId === null || terminalCreateBusy) {
      return;
    }

    const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
    if (isRemoteClientOffline(client)) {
      return;
    }

    createMutation.mutate({
      clientId: selectedClientId,
      cwd: projectPath,
      agent_launch: agentLaunch,
      afterCreate: () => setProjectTerminalPickerOpen(false)
    });
  }, [clientsQuery.data, createMutation, selectedClientId, terminalCreateBusy]);

  const handleConfigureTerminalAtProjectPath = useCallback((projectPath: string, agent: AgentLaunchKind) => {
    if (selectedClientId === null || terminalCreateBusy) {
      return;
    }

    const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
    if (isRemoteClientOffline(client)) {
      return;
    }

    setProjectTerminalPickerOpen(false);
    setTerminalCreateContext({
      title: "New terminal by project path",
      description: projectPath,
      cwd: projectPath,
      initialAgent: agent,
      showConfigInitially: true
    });
  }, [clientsQuery.data, selectedClientId, terminalCreateBusy]);

  const handleCreateTerminalAtGroup = useCallback((node: SwitcherGroupNode) => {
    if (selectedClientId === null || terminalCreateBusy) {
      return;
    }

    const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
    if (isRemoteClientOffline(client)) {
      return;
    }

    const input = createWindowInputForGroupNode(node);
    createMutation.mutate({
      clientId: selectedClientId,
      cwd: input.cwd,
      folder_path: input.folder_path,
      agent_launch: null,
      afterCreate: () => {
        setTerminalSwitcherOpen(false);
      }
    });
  }, [clientsQuery.data, createMutation, selectedClientId, terminalCreateBusy]);

  const handleConfigureTerminalAtGroup = useCallback((node: SwitcherGroupNode) => {
    if (selectedClientId === null || terminalCreateBusy) {
      return;
    }

    const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
    if (isRemoteClientOffline(client)) {
      return;
    }

    const input = createWindowInputForGroupNode(node);
    setTerminalCreateContext({
      title: "New terminal",
      description: node.projectPath ?? node.topicPath ?? node.label,
      cwd: input.cwd,
      folder_path: input.folder_path,
      afterCreate: () => {
        setTerminalSwitcherOpen(false);
      }
    });
  }, [clientsQuery.data, selectedClientId, terminalCreateBusy]);

  const handleCreateTerminalSubmit = useCallback((payload: TerminalCreateSubmit) => {
    if (selectedClientId === null || terminalCreateBusy) {
      return;
    }
    createMutation.mutate({
      clientId: selectedClientId,
      cwd: payload.cwd,
      folder_path: payload.folder_path,
      agent_launch: payload.agent_launch,
      afterCreate: terminalCreateContext?.afterCreate
    });
  }, [createMutation, selectedClientId, terminalCreateBusy, terminalCreateContext]);

  const triggerAgentRecordExpand = useCallback(() => {
    if (selectedClientId === null || selectedWindowId === null) {
      return;
    }

    setTerminalImmersive(false);
    setTerminalControlsOpen(false);
    setTerminalSwitcherOpen(false);
    setNotificationCenterOpen(false);
    agentRecordModal.setExpanded(true);
    setAgentRecordModalOpen(true);
  }, [agentRecordModal, selectedClientId, selectedWindowId]);

  const triggerLocateSelectedTerminal = useCallback(() => {
    if (selectedClientId === null || selectedWindowId === null) {
      return;
    }

    setMobileTerminalActive(false);
    setTerminalImmersive(false);
    setTerminalControlsOpen(false);
    setTerminalListLocateSignal((signal) => signal + 1);
  }, [selectedClientId, selectedWindowId]);

  const triggerGitDiffBrowser = useCallback(() => {
    if (selectedClientId === null || selectedWindowId === null) {
      return;
    }

    setTerminalImmersive(false);
    setTerminalControlsOpen(false);
    setTerminalSwitcherOpen(false);
    setProjectTerminalPickerOpen(false);
    setNotificationCenterOpen(false);
    setAgentRecordModalOpen(false);
    setDetailPanelOpen(false);
    setGitDiffBrowserOpen(true);
  }, [selectedClientId, selectedWindowId]);

  const triggerQuickInput = useCallback(() => {
    if (selectedClientId === null || selectedWindowId === null) {
      return;
    }

    setMobileTerminalActive(true);
    requestAnimationFrame(() => {
      terminalPaneRef.current?.openQuickInput();
    });
  }, [selectedClientId, selectedWindowId]);

  const toggleSettings = useCallback(() => {
    setSettingsInitialView("general");
    setSettingsOpen((open) => !open);
  }, []);

  const toggleNotificationCenter = useCallback(() => {
    setNotificationCenterOpen((open) => !open);
  }, []);

  const triggerClientScopedTerminalSwitcherShortcut = useCallback(() => {
    triggerTerminalSwitcherShortcut("client");
  }, [triggerTerminalSwitcherShortcut]);

  const runKeyboardShortcutAction = useCallback((id: KeyboardShortcutId) => {
    switch (id) {
      case "switch-terminal":
        triggerClientScopedTerminalSwitcherShortcut();
        return true;
      case "switch-terminal-global":
        triggerGlobalTerminalSwitcherShortcut();
        return true;
      case "switch-client":
        triggerClientSwitcherShortcut();
        return true;
      case "new-terminal":
        triggerNewTerminalShortcut();
        return true;
      case "new-terminal-project":
        triggerNewTerminalByProjectShortcut();
        return true;
      case "quick-input":
        triggerQuickInput();
        return true;
      case "expand-record":
        triggerAgentRecordExpand();
        return true;
      case "locate-terminal":
        triggerLocateSelectedTerminal();
        return true;
      case "git-diff":
        triggerGitDiffBrowser();
        return true;
      case "settings":
        toggleSettings();
        return true;
    }
  }, [
    toggleSettings,
    triggerAgentRecordExpand,
    triggerClientSwitcherShortcut,
    triggerClientScopedTerminalSwitcherShortcut,
    triggerGitDiffBrowser,
    triggerGlobalTerminalSwitcherShortcut,
    triggerLocateSelectedTerminal,
    triggerNewTerminalByProjectShortcut,
    triggerNewTerminalShortcut,
    triggerQuickInput,
  ]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (settingsOpen) {
        return;
      }

      if (isGlobalShortcutTextTarget(event.target)) {
        return;
      }

      for (const definition of [
        "new-terminal-project",
        "switch-terminal-global",
        "switch-client",
        "switch-terminal",
        "new-terminal",
        "quick-input",
        "expand-record",
        "locate-terminal",
        "git-diff",
        "settings"
      ] as KeyboardShortcutId[]) {
        const shortcut = effectiveKeyboardShortcut(definition, keyboardShortcutBindings);
        if (!keyboardShortcutMatches(event, shortcut)) {
          continue;
        }

        event.preventDefault();
        event.stopPropagation();
        if (event.repeat && definition === "locate-terminal") {
          return;
        }
        runKeyboardShortcutAction(definition);
        return;
      }

      for (const quickKey of customQuickKeys) {
        if (!keyboardShortcutMatches(event, quickKey.shortcut ?? null)) {
          continue;
        }

        event.preventDefault();
        event.stopPropagation();
        submitCustomQuickKey(quickKey);
        return;
      }
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [customQuickKeys, keyboardShortcutBindings, runKeyboardShortcutAction, settingsOpen, submitCustomQuickKey]);

  useEffect(() => {
    if (!terminalSwitcherOpen) {
      setTerminalSwitcherMode("recent");
      setTerminalSwitcherRecentScope("client");
    }
  }, [terminalSwitcherOpen]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) {
        return;
      }

      if (
        terminalSwitcherOpen
        || clientSwitcherOpen
        || projectTerminalPickerOpen
        || terminalCreateContext !== null
        || notificationCenterOpen
        || settingsOpen
        || addClientModalOpen
        || terminalControlsOpen
        || gitDiffBrowserOpen
        || agentRecordModalOpen
      ) {
        return;
      }

      const target = event.target;
      const activeElement = document.activeElement;
      const focusedInXterm = isXtermInput(target) || isXtermInput(activeElement);
      if (focusedInXterm) {
        return;
      }

      if (isBlockingTextInput(target) || isBlockingTextInput(activeElement)) {
        return;
      }

      if (selectedClientId !== null && selectedWindowId !== null) {
        event.preventDefault();
        focusSelectedTerminal();
      }
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [
    focusSelectedTerminal,
    gitDiffBrowserOpen,
    clientSwitcherOpen,
    notificationCenterOpen,
    projectTerminalPickerOpen,
    selectedClientId,
    selectedWindowId,
    settingsOpen,
    addClientModalOpen,
    agentRecordModalOpen,
    terminalCreateContext,
    terminalControlsOpen,
    terminalSwitcherOpen
  ]);

  useEffect(() => {
    setTerminalSwitcherOpen(false);
    setClientSwitcherOpen(false);
    setProjectTerminalPickerOpen(false);
    setTerminalControlsOpen(false);
    setTerminalImmersive(false);
    setNotificationCenterOpen(false);
    setGitDiffBrowserOpen(false);
    setAgentRecordModalOpen(false);
    setTerminalCreateContext(null);
  }, [selectedClientId]);

  useEffect(() => {
    if (selectedClientId === null) {
      setTerminalNotifications([]);
      notificationPreviousRef.current = [];
      notificationHydratedClientRef.current = null;
      return;
    }
    if (!terminalNotificationsQuery.isSuccess) {
      if (notificationHydratedClientRef.current !== selectedClientId) {
        setTerminalNotifications([]);
        notificationPreviousRef.current = [];
      }
      return;
    }

    const next = normalizeTerminalNotifications(terminalNotificationsQuery.data?.notifications);
    const previous = notificationPreviousRef.current;
    const hydrated = notificationHydratedClientRef.current === selectedClientId;
    const newlyUnread = hydrated ? findNewUnreadNotifications(previous, next) : [];
    notificationHydratedClientRef.current = selectedClientId;
    notificationPreviousRef.current = next;
    setTerminalNotifications(next);

    if (!desktopNotificationsEnabled || newlyUnread.length === 0) {
      return;
    }

    void ensureDesktopNotificationPermission().then((permission) => {
      if (permission !== "granted") {
        return;
      }

      for (const notification of newlyUnread) {
        showAgentTaskDesktopNotification(notification);
      }
    });
  }, [
    desktopNotificationsEnabled,
    selectedClientId,
    terminalNotificationsQuery.data,
    terminalNotificationsQuery.isSuccess
  ]);

  useEffect(() => {
    if (selectedClientId === null || selectedWindowId === null) {
      return;
    }

    if (typeof document !== "undefined" && document.visibilityState !== "visible") {
      return;
    }

    const notification = terminalNotifications.find(
      (item) => item.windowId === selectedWindowId && !item.read
    );
    if (notification === undefined) {
      return;
    }

    void markTerminalNotificationRead(
      selectedClientId,
      notification.windowId,
      notification.completedAt
    ).then((result) => {
      const next = normalizeTerminalNotifications(result.notifications);
      notificationPreviousRef.current = next;
      setTerminalNotifications(next);
      queryClient.setQueriesData({ queryKey: ["terminal-notifications", selectedClientId] }, () => result);
    }).catch(() => {});
  }, [queryClient, selectedClientId, selectedWindowId, terminalNotifications]);

  useEffect(() => {
    if (
      deferredTreeSelection !== null &&
      deferredTreeSelection.clientId !== null &&
      deferredTreeSelection.windowId !== null &&
      selectedClientId === deferredTreeSelection.clientId &&
      selectedWindowId === deferredTreeSelection.windowId
    ) {
      if (!treeContainsWindow(treeFolders, selectedWindowId)) {
        return;
      }
      rememberClientWindowSelection(deferredTreeSelection.clientId, deferredTreeSelection.windowId);
      setDeferredTreeSelection(null);
    }

    if (
      routeSelectionRequest !== null ||
      selectedClientId === null ||
      selectedWindowId === null ||
      treeFolders === undefined ||
      treeQuery.isFetching ||
      treeContainsWindow(treeFolders, selectedWindowId)
    ) {
      return;
    }

    setSelectedWindowId(null);
    setDeferredTreeSelection(null);
    setAgentRecordModalOpen(false);
    rememberClientWindowSelection(selectedClientId, null);
    writeTerminalRoute(selectedClientId, null, "replace");
  }, [
    deferredTreeSelection,
    rememberClientWindowSelection,
    routeSelectionRequest,
    selectedClientId,
    selectedWindowId,
    treeFolders,
    treeQuery.isFetching
  ]);

  useEffect(() => {
    window.localStorage.setItem(TERMINAL_VIEWPORT_STORAGE_KEY, terminalViewportMode);
  }, [terminalViewportMode]);

  useEffect(() => {
    if (!isMobileLayout || selectedClientId === null || selectedWindowId === null) {
      return;
    }

    const routeSelection = readTerminalRouteSelection();
    if (routeSelection.clientId === selectedClientId && routeSelection.windowId === selectedWindowId) {
      setMobileTerminalActive(true);
    }
  }, [isMobileLayout, selectedClientId, selectedWindowId]);

  useEffect(() => {
    if (selectedWindowId === null || !treeQuery.isSuccess) {
      return;
    }

    const frame = window.requestAnimationFrame(() => {
      terminalPaneRef.current?.refit();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [selectedWindowId, treeQuery.isSuccess]);

  useEffect(() => {
    if (!terminalControlsOpen) {
      return;
    }

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node) || !terminalControlsRef.current?.contains(target)) {
        setTerminalControlsOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setTerminalControlsOpen(false);
        focusSelectedTerminal();
      }
    };

    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [terminalControlsOpen]);

  const selectClient = (clientId: string) => {
    const rememberedWindowId = clientWindowSelections[clientId]?.windowId ?? null;
    setRouteSelectionRequest(null);
    setDeferredTreeSelection(null);
    setSelectedClientId(clientId);
    setSelectedWindowId(rememberedWindowId);
    rememberClientUse(clientId);
    setMobileTerminalActive(false);
    setDetailPanelOpen(false);
    setTerminalImmersive(false);
    setAgentRecordModalOpen(false);
    writeTerminalRoute(clientId, rememberedWindowId, "push");
  };

  const selectWindow = (windowId: string, clientId = selectedClientId) => {
    if (clientId === null) {
      return;
    }
    const selectingCurrentClient = clientId === selectedClientId;
    const selectedTitle = selectingCurrentClient && windowId === selectedWindowId
      ? findWindowTitle(treeFolders, windowId)
      : null;
    if (selectedTitle !== null && selectingCurrentClient) {
      persistTerminalRecent(clientId, windowId, selectedTitle);
    }

    setRouteSelectionRequest(null);
    setDeferredTreeSelection(null);
    setSelectedClientId(clientId);
    setSelectedWindowId(windowId);
    rememberClientUse(clientId);
    rememberClientWindowSelection(clientId, windowId);
    setDetailPanelOpen(false);
    setAgentRecordModalOpen(false);
    writeTerminalRoute(clientId, windowId, "push");
    focusSelectedTerminal();
  };

  const handleTerminalPaneSelection = useCallback((windowId: string) => {
    if (selectedClientId === null) {
      return;
    }

    setRouteSelectionRequest(null);
    setDeferredTreeSelection(null);
    setSelectedWindowId(windowId);
    rememberClientUse(selectedClientId);
    rememberClientWindowSelection(selectedClientId, windowId);
    setAgentRecordModalOpen(false);
    writeTerminalRoute(selectedClientId, windowId, "replace");
  }, [rememberClientUse, rememberClientWindowSelection, selectedClientId]);

  useEffect(() => {
    if (selectedClientId === null || selectedWindowId === null || selectedWindowTitle === null) {
      return;
    }

    rememberClientUse(selectedClientId);
    rememberClientWindowSelection(selectedClientId, selectedWindowId);
    persistTerminalRecent(selectedClientId, selectedWindowId, selectedWindowTitle);
  }, [
    persistTerminalRecent,
    rememberClientUse,
    rememberClientWindowSelection,
    selectedClientId,
    selectedWindowId,
    selectedWindowTitle
  ]);

  const submitBootstrap = (payload: BootstrapClientInput) => {
    bootstrapMutation.mutate(payload);
  };

  const openAddClientModal = (mode: AddClientMode) => {
    setAddClientInitialMode(mode);
    setBootstrapFailed(false);
    setAddClientModalOpen(true);
  };

  const closeAddClientModal = () => {
    setAddClientModalOpen(false);
    setBootstrapFailed(false);
    bootstrapMutation.reset();
  };

  const unreadNotificationCount = terminalNotifications.filter((notification) => !notification.read).length;
  const hasUnreadNotification = (windowId: string) =>
    terminalNotifications.some((notification) => notification.windowId === windowId && !notification.read);
  const agentPreviewCanSendQuickInput = terminalConnectionStatus === "connected";
  const createErrorMessage = createMutation.error instanceof Error
    ? createMutation.error.message
    : "Failed to create terminal.";
  const deleteErrorMessage = deleteMutation.error instanceof Error
    ? deleteMutation.error.message
    : "Failed to delete terminal.";
  const deleteClientErrorMessage = deleteClientMutation.error instanceof Error
    ? deleteClientMutation.error.message
    : "Failed to delete client.";
  const switchTerminalShortcut = effectiveKeyboardShortcut("switch-terminal", keyboardShortcutBindings);
  const switchGlobalTerminalShortcut = effectiveKeyboardShortcut("switch-terminal-global", keyboardShortcutBindings);
  const activeTerminalSwitcherShortcut = terminalSwitcherRecentScope === "global"
    ? switchGlobalTerminalShortcut
    : switchTerminalShortcut;
  const newTerminalShortcutLabel = keyboardShortcutLabel(effectiveKeyboardShortcut("new-terminal", keyboardShortcutBindings));
  const quickInputShortcutLabel = keyboardShortcutLabel(effectiveKeyboardShortcut("quick-input", keyboardShortcutBindings));
  const gitDiffShortcutLabel = keyboardShortcutLabel(effectiveKeyboardShortcut("git-diff", keyboardShortcutBindings));
  const settingsShortcutLabel = keyboardShortcutLabel(effectiveKeyboardShortcut("settings", keyboardShortcutBindings));
  const onboardingSteps = useMemo(
    () => buildOnboardingSteps({
      settings: settingsShortcutLabel,
      switchTerminal: keyboardShortcutLabel(effectiveKeyboardShortcut("switch-terminal", keyboardShortcutBindings)),
      newTerminal: newTerminalShortcutLabel,
      newTerminalProject: keyboardShortcutLabel(effectiveKeyboardShortcut("new-terminal-project", keyboardShortcutBindings)),
      quickInput: quickInputShortcutLabel
    }),
    [
      keyboardShortcutBindings,
      newTerminalShortcutLabel,
      quickInputShortcutLabel,
      settingsShortcutLabel
    ]
  );

  const deletingWindowId = deleteMutation.isPending ? deleteMutation.variables?.windowId ?? null : null;
  const deletingClientId = deleteClientMutation.isPending ? deleteClientMutation.variables?.clientId ?? null : null;

  const requestDeleteClient = (client: Client) => {
    if (client.runtime !== "remote" || deleteClientMutation.isPending) {
      return;
    }

    if (!window.confirm(`Delete client "${client.name}"? This removes its terminals, history, and saved metadata.`)) {
      return;
    }

    setUpdateFailed(false);
    setUpdateMessage(null);
    deleteClientMutation.mutate({ clientId: client.id });
  };

  const requestDeleteWindow = (windowId: string, title: string) => {
    if (selectedClientId === null || deleteMutation.isPending) {
      return;
    }

    if (!window.confirm(`Delete "${title}"? This closes the tmux window and removes it from the list.`)) {
      return;
    }

    setTerminalControlsOpen(false);
    deleteMutation.mutate({
      clientId: selectedClientId,
      windowId,
      nextWindowId: pickWindowAfterDelete(treeFolders, windowId)
    });
  };

  const confirmDeleteTerminal = () => {
    if (selectedWindowId === null) {
      return;
    }

    requestDeleteWindow(selectedWindowId, selectedWindowTitle ?? "this terminal");
  };

  const handleSelectNotification = useCallback((notification: TerminalNotification) => {
    void markTerminalNotificationRead(
      notification.clientId,
      notification.windowId,
      notification.completedAt
    ).then((result) => {
      const next = normalizeTerminalNotifications(result.notifications);
      notificationPreviousRef.current = next;
      setTerminalNotifications(next);
      queryClient.setQueriesData({ queryKey: ["terminal-notifications", notification.clientId] }, () => result);
    }).catch(() => {});
    setNotificationCenterOpen(false);
    if (notification.clientId !== selectedClientId) {
      setRouteSelectionRequest(null);
      setDeferredTreeSelection(null);
      setSelectedClientId(notification.clientId);
    }
    setDeferredTreeSelection(null);
    setSelectedWindowId(notification.windowId);
    rememberClientUse(notification.clientId);
    rememberClientWindowSelection(notification.clientId, notification.windowId);
    setDetailPanelOpen(false);
    setAgentRecordModalOpen(false);
    setMobileTerminalActive(true);
    writeTerminalRoute(notification.clientId, notification.windowId, "push");
    focusSelectedTerminal();
  }, [
    focusSelectedTerminal,
    queryClient,
    rememberClientUse,
    rememberClientWindowSelection,
    selectedClientId
  ]);

  const handleDeleteNotification = useCallback((notification: TerminalNotification) => {
    void dismissTerminalNotification(
      notification.clientId,
      notification.windowId,
      notification.completedAt
    ).then((result) => {
      const next = normalizeTerminalNotifications(result.notifications);
      notificationPreviousRef.current = next;
      setTerminalNotifications(next);
      queryClient.setQueriesData({ queryKey: ["terminal-notifications", notification.clientId] }, () => result);
    }).catch(() => {});
  }, [queryClient]);

  const handleClearNotifications = useCallback(() => {
    if (selectedClientId === null) {
      return;
    }

    void clearTerminalNotifications(selectedClientId)
      .then((result) => {
        const next = normalizeTerminalNotifications(result.notifications);
        notificationPreviousRef.current = next;
        setTerminalNotifications(next);
        queryClient.setQueriesData({ queryKey: ["terminal-notifications", selectedClientId] }, () => result);
      })
      .catch(() => {});
  }, [queryClient, selectedClientId]);

  const startOnboardingFromSettings = useCallback(() => {
    setSettingsOpen(false);
    setSettingsInitialView("general");
    window.requestAnimationFrame(() => {
      window.dispatchEvent(new Event("web-terminal-acp:start-onboarding"));
    });
  }, []);

  const runOnboardingAction = useCallback((action: OnboardingAction) => {
    switch (action) {
      case "remote-bootstrap":
        setSettingsOpen(false);
        setSettingsInitialView("general");
        setTerminalCreateContext(null);
        setTerminalSwitcherOpen(false);
        setProjectTerminalPickerOpen(false);
        setNotificationCenterOpen(false);
        setGitDiffBrowserOpen(false);
        setDetailPanelOpen(false);
        setTerminalControlsOpen(false);
        setTerminalQuickInputOpen(false);
        setBootstrapFailed(false);
        setAddClientInitialMode("bootstrap");
        setAddClientModalOpen(true);
        return;
      case "remote-registration-menu":
        setTerminalCreateContext(null);
        setTerminalSwitcherOpen(false);
        setProjectTerminalPickerOpen(false);
        setNotificationCenterOpen(false);
        setGitDiffBrowserOpen(false);
        setTerminalControlsOpen(false);
        setTerminalQuickInputOpen(false);
        setSettingsInitialView("general");
        setSettingsOpen(false);
        setAddClientInitialMode("registration");
        setAddClientModalOpen(true);
        return;
      case "remote-registration":
        setTerminalCreateContext(null);
        setTerminalSwitcherOpen(false);
        setProjectTerminalPickerOpen(false);
        setNotificationCenterOpen(false);
        setGitDiffBrowserOpen(false);
        setTerminalControlsOpen(false);
        setTerminalQuickInputOpen(false);
        setSettingsInitialView("general");
        setSettingsOpen(false);
        setAddClientInitialMode("registration");
        setAddClientModalOpen(true);
        return;
      case "new-terminal":
        setAddClientModalOpen(false);
        setSettingsOpen(false);
        setSettingsInitialView("general");
        setTerminalCreateContext(null);
        setTerminalSwitcherOpen(false);
        setProjectTerminalPickerOpen(false);
        setNotificationCenterOpen(false);
        setGitDiffBrowserOpen(false);
        setTerminalControlsOpen(false);
        setTerminalQuickInputOpen(false);
        return;
      case "quick-input":
        setAddClientModalOpen(false);
        setSettingsOpen(false);
        setSettingsInitialView("general");
        setTerminalCreateContext(null);
        setTerminalSwitcherOpen(false);
        setProjectTerminalPickerOpen(false);
        setNotificationCenterOpen(false);
        setGitDiffBrowserOpen(false);
        setTerminalControlsOpen(false);
        if (selectedClientId !== null && selectedWindowId !== null) {
          setMobileTerminalActive(true);
          requestAnimationFrame(() => terminalPaneRef.current?.openQuickInput());
        }
        return;
      case "switch-terminal":
        setAddClientModalOpen(false);
        setSettingsOpen(false);
        setSettingsInitialView("general");
        setTerminalCreateContext(null);
        setProjectTerminalPickerOpen(false);
        setNotificationCenterOpen(false);
        setGitDiffBrowserOpen(false);
        setTerminalControlsOpen(false);
        setTerminalQuickInputOpen(false);
        setTerminalSwitcherMode("recent");
        setTerminalSwitcherRecentScope("client");
        setTerminalSwitcherOpen(true);
        return;
      case "settings":
        setAddClientModalOpen(false);
        setTerminalCreateContext(null);
        setTerminalSwitcherOpen(false);
        setProjectTerminalPickerOpen(false);
        setNotificationCenterOpen(false);
        setGitDiffBrowserOpen(false);
        setTerminalControlsOpen(false);
        setTerminalQuickInputOpen(false);
        setSettingsInitialView("general");
        setSettingsOpen(true);
        return;
      case "details":
        setAddClientModalOpen(false);
        setSettingsOpen(false);
        setSettingsInitialView("general");
        setTerminalCreateContext(null);
        setTerminalSwitcherOpen(false);
        setProjectTerminalPickerOpen(false);
        setNotificationCenterOpen(false);
        setGitDiffBrowserOpen(false);
        setTerminalControlsOpen(false);
        setTerminalQuickInputOpen(false);
        return;
    }
  }, [
    selectedClient,
    selectedClientId,
    selectedClientOffline,
    selectedWindowId
  ]);

  const mobileShortcutActions = useMemo(
    () => [
      {
        id: "switch-terminal",
        label: "切换终端",
        hint: keyboardShortcutLabel(effectiveKeyboardShortcut("switch-terminal", keyboardShortcutBindings)),
        onPress: triggerClientScopedTerminalSwitcherShortcut
      },
      {
        id: "new-terminal",
        label: "新建终端",
        hint: keyboardShortcutLabel(effectiveKeyboardShortcut("new-terminal", keyboardShortcutBindings)),
        disabled: selectedClientId === null || terminalCreateBusy || selectedClientOffline,
        onPress: triggerNewTerminalShortcut
      },
      {
        id: "new-terminal-project",
        label: "按项目新建",
        hint: keyboardShortcutLabel(effectiveKeyboardShortcut("new-terminal-project", keyboardShortcutBindings)),
        disabled: selectedClientId === null || terminalCreateBusy || selectedClientOffline,
        onPress: triggerNewTerminalByProjectShortcut
      },
      {
        id: "quick-input",
        label: "快速输入",
        hint: keyboardShortcutLabel(effectiveKeyboardShortcut("quick-input", keyboardShortcutBindings)),
        disabled: selectedClientId === null || selectedWindowId === null,
        onPress: triggerQuickInput
      },
      {
        id: "expand-record",
        label: "展开 Agent 记录",
        hint: keyboardShortcutLabel(effectiveKeyboardShortcut("expand-record", keyboardShortcutBindings)),
        disabled: selectedClientId === null || selectedWindowId === null,
        onPress: triggerAgentRecordExpand
      },
      {
        id: "locate-terminal",
        label: "定位当前终端",
        hint: keyboardShortcutLabel(effectiveKeyboardShortcut("locate-terminal", keyboardShortcutBindings)),
        disabled: selectedClientId === null || selectedWindowId === null,
        onPress: triggerLocateSelectedTerminal
      },
      {
        id: "git-diff",
        label: "Git diff",
        hint: keyboardShortcutLabel(effectiveKeyboardShortcut("git-diff", keyboardShortcutBindings)),
        disabled: selectedClientId === null || selectedWindowId === null,
        onPress: triggerGitDiffBrowser
      },
      {
        id: "notifications",
        label: "通知中心",
        badge: unreadNotificationCount,
        onPress: toggleNotificationCenter
      },
      ...customQuickKeys.map((quickKey) => ({
        id: `quick-key-${quickKey.id}`,
        label: quickKey.label,
        hint: quickKey.shortcut ? keyboardShortcutLabel(quickKey.shortcut) : "快捷按键",
        disabled: selectedClientId === null || selectedWindowId === null || terminalConnectionStatus !== "connected",
        onPress: () => {
          submitCustomQuickKey(quickKey);
        }
      })),
      {
        id: "settings",
        label: "设置",
        hint: keyboardShortcutLabel(effectiveKeyboardShortcut("settings", keyboardShortcutBindings)),
        onPress: toggleSettings
      }
    ],
    [
      keyboardShortcutBindings,
      selectedClientId,
      selectedClientOffline,
      selectedWindowId,
      customQuickKeys,
      submitCustomQuickKey,
      terminalCreateBusy,
      terminalConnectionStatus,
      triggerAgentRecordExpand,
      triggerClientScopedTerminalSwitcherShortcut,
      triggerGitDiffBrowser,
      triggerLocateSelectedTerminal,
      triggerNewTerminalByProjectShortcut,
      triggerNewTerminalShortcut,
      triggerQuickInput,
      toggleNotificationCenter,
      toggleSettings,
      unreadNotificationCount
    ]
  );

  return (
    <main
      data-onboarding-id="app-layout"
      className={[
        "app-shell",
        themeSkinClassName(themeSkin),
        mobileTerminalActive ? "mobile-terminal-active" : "",
        detailPanelOpen ? "detail-panel-open" : "",
        terminalImmersive ? "terminal-immersive" : "",
      ].filter(Boolean).join(" ")}
    >
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="sidebar-title-row">
            <h1>Web Terminal ACP</h1>
            <div className="notification-bell-anchor">
              <NotificationBellButton
                unreadCount={unreadNotificationCount}
                isOpen={notificationCenterOpen}
                onClick={() => setNotificationCenterOpen((isOpen) => !isOpen)}
              />
            </div>
          </div>
        </div>
        {clientsQuery.isLoading && <p className="muted">Loading clients...</p>}
        {clientsQuery.isError && <p className="error" role="alert">Failed to load clients.</p>}
        {clientsQuery.data && (
          <section className="client-list" aria-labelledby="client-list-heading" data-onboarding-id="client-list">
            <div className="section-header">
              <h2 id="client-list-heading">Clients</h2>
              <button
                type="button"
                className="section-header-action"
                data-onboarding-id="add-client-button"
                disabled={bootstrapMutation.isPending}
                onClick={() => openAddClientModal("bootstrap")}
              >
                Add client
              </button>
            </div>
            <ClientList
              clients={clientsQuery.data}
              selectedClientId={selectedClientId}
              updatingClientId={updateMutation.isPending ? updateMutation.variables ?? null : null}
              deletingClientId={deletingClientId}
              onSelectClient={selectClient}
              onUpdateClient={(clientId) => updateMutation.mutate(clientId)}
              onDeleteClient={requestDeleteClient}
            />
          </section>
        )}
        {updateMessage && <p className="muted">{updateMessage}</p>}
        {updateFailed && <p className="error" role="alert">Client update failed to start.</p>}
        {deleteClientMutation.isError && <p className="error" role="alert">{deleteClientErrorMessage}</p>}
        {selectedClientOffline && <p className="muted">Client agent offline, waiting for reconnect.</p>}
        {createMutation.isError && <p className="error" role="alert">{createErrorMessage}</p>}
        {deleteMutation.isError && <p className="error" role="alert">{deleteErrorMessage}</p>}
        {selectedClientId !== null && treeQuery.isLoading && <p className="muted">Loading tree...</p>}
        {selectedClientId !== null && treeQuery.isError && <p className="error" role="alert">Failed to load tree.</p>}
        {selectedClientId !== null && treeFolders && (
          <FolderTree
            clientId={selectedClientId}
            folders={treeFolders}
            groupingMode={terminalGroupingMode}
            summaryOutputLanguage={summaryOutputLanguage}
            selectedWindowId={selectedWindowId}
            locateSelectedWindowSignal={terminalListLocateSignal}
            deletingWindowId={deletingWindowId}
            hasUnreadNotification={hasUnreadNotification}
            onSelectWindow={(window) => selectWindow(window.id)}
            onDeleteWindow={(window) => requestDeleteWindow(window.id, window.title)}
            onCreateTerminalAtGroup={handleCreateTerminalAtGroup}
            onConfigureTerminalAtGroup={handleConfigureTerminalAtGroup}
            renderHeaderAction={() => (
              <button
                type="button"
                className="section-header-action"
                data-onboarding-id="new-terminal-button"
                title={newTerminalShortcutLabel}
                disabled={selectedClientId === null || terminalCreateBusy || selectedClientOffline}
                onClick={triggerNewTerminalShortcut}
              >
                New terminal
              </button>
            )}
            creatingTerminal={terminalCreateBusy}
            createTerminalDisabled={selectedClientOffline}
          />
        )}
        <div className="mobile-enter-terminal">
          <button
            type="button"
            disabled={selectedClientId === null || selectedWindowId === null}
            onClick={() => setMobileTerminalActive(true)}
          >
            Enter terminal
          </button>
        </div>
      </aside>
      <section className="workspace">
        <div className="toolbar" aria-live="polite">
          <button type="button" className="mobile-back-button" onClick={() => setMobileTerminalActive(false)}>
            Terminals
          </button>
          <div className="terminal-actions" ref={terminalControlsRef}>
            <button
              type="button"
              className="terminal-menu-button"
              aria-expanded={terminalControlsOpen}
              aria-haspopup="menu"
              onClick={() => setTerminalControlsOpen((isOpen) => !isOpen)}
            >
              Controls
            </button>
            {terminalControlsOpen && (
              <div className="terminal-controls-menu" role="menu" data-onboarding-id="terminal-controls-menu">
                <div className="terminal-controls-section">
                  <span>View</span>
                  <div className="terminal-mode-toggle" role="group" aria-label="Terminal viewport mode">
                    <button
                      type="button"
                      aria-pressed={terminalViewportMode === "desktop"}
                      className={terminalViewportMode === "desktop" ? "active" : ""}
                      onClick={() => setTerminalViewportMode("desktop")}
                    >
                      Desktop
                    </button>
                    <button
                      type="button"
                      aria-pressed={terminalViewportMode === "phone"}
                      className={terminalViewportMode === "phone" ? "active" : ""}
                      onClick={() => setTerminalViewportMode("phone")}
                    >
                      Phone
                    </button>
                    <button
                      type="button"
                      aria-pressed={terminalViewportMode === "fixed"}
                      className={terminalViewportMode === "fixed" ? "active" : ""}
                      onClick={() => setTerminalViewportMode("fixed")}
                    >
                      1920x1080
                    </button>
                  </div>
                </div>
                <button
                  type="button"
                  role="menuitem"
                  className="terminal-controls-row"
                  onClick={toggleVirtualKeysVisibility}
                >
                  <span>Virtual keys</span>
                  <strong>{virtualKeysVisible ? "On" : "Off"}</strong>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="terminal-controls-row"
                  disabled={selectedClientId === null || selectedWindowId === null}
                  onClick={() => {
                    triggerQuickInput();
                    setTerminalControlsOpen(false);
                  }}
                >
                  <span>Quick input</span>
                  <strong>{terminalQuickInputOpen ? "Open" : quickInputShortcutLabel}</strong>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="terminal-controls-row"
                  disabled={selectedClientId === null || selectedWindowId === null}
                  onClick={() => {
                    setTerminalImmersive(true);
                    setDetailPanelOpen(false);
                    setTerminalControlsOpen(false);
                    setTerminalSwitcherOpen(false);
                  }}
                >
                  <span>Immersive mode</span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="terminal-controls-row"
                  disabled={selectedClientId === null || selectedWindowId === null}
                  onClick={() => {
                    triggerGitDiffBrowser();
                    setTerminalControlsOpen(false);
                  }}
                >
                  <span>Git diff</span>
                  <strong>{gitDiffShortcutLabel}</strong>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="terminal-controls-row"
                  onClick={() => {
                    setSettingsOpen(true);
                    setTerminalControlsOpen(false);
                  }}
                >
                  <span>Settings</span>
                  <strong>{settingsShortcutLabel}</strong>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="terminal-controls-row"
                  onClick={() => {
                    setDetailPanelOpen(true);
                    setTerminalControlsOpen(false);
                  }}
                >
                  <span>Details</span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="terminal-controls-row terminal-controls-row-danger"
                  disabled={selectedClientId === null || selectedWindowId === null || deleteMutation.isPending}
                  onClick={confirmDeleteTerminal}
                >
                  <span>Delete terminal</span>
                </button>
              </div>
            )}
          </div>
        </div>
        <TerminalPane
          ref={terminalPaneRef}
          clientId={selectedClientId}
          windowId={selectedWindowId}
          onTerminalSelection={handleTerminalPaneSelection}
          viewportMode={terminalViewportMode}
          onQuickInputOpenChange={setTerminalQuickInputOpen}
          onQuickInputDraftChange={setTerminalQuickInputDraft}
          onTerminalConnectionStatusChange={setTerminalConnectionStatus}
          customQuickKeys={customQuickKeys}
          onCustomQuickKeySubmit={submitCustomQuickKey}
          theme={terminalTheme}
          layoutVersion={
            (mobileTerminalActive ? 1 : 0)
            + (terminalImmersive ? 2 : 0)
            + (detailPanelOpen ? 4 : 0)
          }
          virtualKeysVisible={virtualKeysVisible}
        />
        {pendingTerminalCreate !== null && (
          <div className="terminal-create-progress" role="status" aria-live="polite">
            <span className="terminal-create-progress-spinner" aria-hidden="true" />
            <span>Creating {pendingTerminalCreate.title}...</span>
          </div>
        )}
      </section>
      <aside className="detail-panel" data-onboarding-id="detail-panel">
        <div className="detail-panel-mobile-header">
          <strong>Details</strong>
          <button type="button" onClick={() => setDetailPanelOpen(false)}>
            Close
          </button>
        </div>
        <WindowDetail
          clientId={selectedClientId}
          windowId={selectedWindowId}
          gitWorktree={selectedTreeWindow?.git_worktree ?? null}
          terminalStatusLabel={terminalStatusLabel(terminalConnectionStatus)}
          terminalStatusTone={terminalConnectionStatus}
          quickInputDraft={terminalQuickInputDraft}
          canSendQuickInput={agentPreviewCanSendQuickInput}
          agentRecordShortcutLabel={keyboardShortcutLabel(effectiveKeyboardShortcut("expand-record", keyboardShortcutBindings))}
          onQuickInputDraftChange={(draft) => terminalPaneRef.current?.setQuickInputDraft(draft)}
          onQuickInputSubmit={submitAgentPreviewQuickInput}
        />
        <SearchPanel clientId={selectedClientId} onSelectWindowId={selectWindow} />
      </aside>
      <AgentRecordModal
        open={agentRecordModalOpen}
        mode={agentRecordModal.mode}
        chatRoleFilter={agentRecordModal.chatRoleFilter}
        chatRecord={agentRecordModal.chatRecord}
        detailRecord={agentRecordModal.detailRecord}
        sessions={agentRecordModal.sessions}
        isLoading={agentRecordModal.isLoading}
        isError={agentRecordModal.isError}
        isFetching={agentRecordModal.isFetching}
        terminalStatusLabel={terminalStatusLabel(terminalConnectionStatus)}
        terminalStatusTone={terminalConnectionStatus}
        quickInputDraft={terminalQuickInputDraft}
        canSendQuickInput={agentPreviewCanSendQuickInput}
        onQuickInputDraftChange={(draft) => terminalPaneRef.current?.setQuickInputDraft(draft)}
        onQuickInputSubmit={submitAgentPreviewQuickInput}
        onModeChange={agentRecordModal.setMode}
        onChatRoleFilterChange={agentRecordModal.setChatRoleFilter}
        onClose={() => {
          agentRecordModal.setExpanded(false);
          setAgentRecordModalOpen(false);
        }}
        onSessionChange={agentRecordModal.resetPages}
        onPreviousPage={agentRecordModal.previousPage}
        onNextPage={agentRecordModal.nextPage}
      />
      {detailPanelOpen && <button type="button" className="detail-backdrop" aria-label="Close details" onClick={() => setDetailPanelOpen(false)} />}
      {terminalImmersive && (
        <button
          type="button"
          className="terminal-immersive-exit"
          onClick={() => setTerminalImmersive(false)}
        >
          Exit immersive mode
        </button>
      )}
      <TerminalSwitcher
        clientId={selectedClientId}
        folders={treeFolders}
        mode={terminalSwitcherMode}
        recentScope={terminalSwitcherRecentScope}
        terminalGroupingMode={terminalGroupingMode}
        summaryOutputLanguage={summaryOutputLanguage}
        isOpen={terminalSwitcherOpen}
        selectedWindowId={selectedWindowId}
        hasUnreadNotification={hasUnreadNotification}
        onClose={closeTerminalSwitcher}
        onSelectWindow={selectWindow}
        onToggleModeShortcut={toggleTerminalSwitcherMode}
        onCreateTerminalAtGroup={handleCreateTerminalAtGroup}
        onConfigureTerminalAtGroup={handleConfigureTerminalAtGroup}
        creatingTerminal={terminalCreateBusy}
        createTerminalDisabled={selectedClientOffline}
        switchShortcut={activeTerminalSwitcherShortcut}
        switchShortcutLabel={keyboardShortcutLabel(activeTerminalSwitcherShortcut)}
      />
      <ClientSwitcher
        clients={clientsQuery.data ?? []}
        selectedClientId={selectedClientId}
        recentClientIds={recentClientIds}
        isOpen={clientSwitcherOpen}
        onClose={closeClientSwitcher}
        onSelectClient={selectClient}
      />
      <ProjectTerminalPicker
        isOpen={projectTerminalPickerOpen}
        projectPaths={projectPaths}
        projectSummaries={projectSummariesQuery.data ?? []}
        loadingProjects={treeQuery.isFetching || windowActivityQuery.isFetching}
        creatingTerminal={terminalCreateBusy}
        createTerminalDisabled={selectedClientOffline}
        onClose={() => {
          setProjectTerminalPickerOpen(false);
          focusSelectedTerminal();
        }}
        onCreateTerminal={handleCreateTerminalAtProjectPath}
        onConfigureTerminal={handleConfigureTerminalAtProjectPath}
      />
      <TerminalCreateModal
        isOpen={terminalCreateContext !== null}
        clientId={selectedClientId}
        context={terminalCreateContext}
        creatingTerminal={terminalCreateBusy}
        createTerminalDisabled={selectedClientOffline}
        onClose={() => {
          setTerminalCreateContext(null);
          focusSelectedTerminal();
        }}
        onSubmit={handleCreateTerminalSubmit}
      />
      <SettingsModal
        isOpen={settingsOpen}
        onClose={() => {
          setSettingsOpen(false);
          focusSelectedTerminal();
        }}
        initialView={settingsInitialView}
        summaryOutputLanguage={summaryOutputLanguage}
        terminalGroupingMode={terminalGroupingMode}
        themeSkin={themeSkin}
        desktopNotificationsEnabled={desktopNotificationsEnabled}
        keyboardShortcutBindings={keyboardShortcutBindings}
        customQuickKeys={customQuickKeys}
        onSummaryOutputLanguageChange={setSummaryOutputLanguage}
        onTerminalGroupingModeChange={setTerminalGroupingMode}
        onThemeSkinChange={setThemeSkin}
        onDesktopNotificationsEnabledChange={setDesktopNotificationsEnabled}
        onKeyboardShortcutBindingsChange={handleKeyboardShortcutBindingsChange}
        onCustomQuickKeysChange={handleCustomQuickKeysChange}
        authEnabled={authEnabled}
        onboardingEnabled={isOnboardingEnabled()}
        onStartOnboarding={startOnboardingFromSettings}
        onLogout={onLogout}
      />
      <AddClientModal
        isOpen={addClientModalOpen}
        initialMode={addClientInitialMode}
        bootstrapFailed={bootstrapFailed}
        bootstrapPending={bootstrapMutation.isPending}
        registrationKey={registrationKeyMutation.data?.key ?? null}
        registrationKeyPending={registrationKeyMutation.isPending}
        registrationKeyError={registrationKeyMutation.isError ? "生成注册 key 失败" : null}
        onClose={closeAddClientModal}
        onBootstrapSubmit={submitBootstrap}
        onGenerateRegistrationKey={(label) => registrationKeyMutation.mutate(label)}
      />
      <NotificationCenter
        isOpen={notificationCenterOpen}
        notifications={terminalNotifications}
        onClose={() => setNotificationCenterOpen(false)}
        onSelectNotification={handleSelectNotification}
        onDeleteNotification={handleDeleteNotification}
        onClearNotifications={handleClearNotifications}
      />
      {gitDiffBrowserOpen && selectedClientId !== null && selectedWindowId !== null && (
        <GitDiffBrowserModal
          clientId={selectedClientId}
          windowId={selectedWindowId}
          isMobileLayout={isMobileLayout}
          shortcutLabel={gitDiffShortcutLabel}
          onClose={() => {
            setGitDiffBrowserOpen(false);
            focusSelectedTerminal();
          }}
        />
      )}
      <MobileShortcutFab
        visible={
          isMobileLayout
          && !terminalSwitcherOpen
          && !clientSwitcherOpen
          && !projectTerminalPickerOpen
          && !notificationCenterOpen
          && !settingsOpen
          && !addClientModalOpen
          && !terminalQuickInputOpen
          && !gitDiffBrowserOpen
        }
        actions={mobileShortcutActions}
        onDirectionInput={submitMobileShortcutDirection}
      />
      <OnboardingTour steps={onboardingSteps} onStepAction={runOnboardingAction} />
    </main>
  );
}
