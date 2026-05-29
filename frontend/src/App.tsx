import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  bootstrapClient,
  createWindow,
  deleteWindow,
  fetchClients,
  fetchProjectSummaries,
  fetchTree,
  fetchWindowActivity,
  recordTerminalRecent,
  uiEventsWebSocketUrl,
  updateClient
} from "./api";
import { BootstrapClientForm } from "./components/BootstrapClientForm";
import { AgentRecordModal } from "./components/AgentRecordViewer";
import { ClientList } from "./components/ClientList";
import { FolderTree } from "./components/FolderTree";
import { GitDiffBrowserModal } from "./components/GitDiffBrowserModal";
import { ProjectTerminalPicker } from "./components/ProjectTerminalPicker";
import { SearchPanel } from "./components/SearchPanel";
import { MobileShortcutFab } from "./components/MobileShortcutFab";
import { NotificationBellButton, NotificationCenter } from "./components/NotificationCenter";
import { TerminalPane, type TerminalConnectionStatus, type TerminalPaneHandle } from "./components/TerminalPane";
import { useAgentRecordData } from "./hooks/useAgentRecordData";
import { readMobileLayout, useMobileLayout } from "./hooks/useMobileLayout";
import { readInitialSettings, SettingsModal } from "./components/SettingsModal";
import { TerminalSwitcher, type TerminalSwitcherMode } from "./components/TerminalSwitcher";
import { WindowDetail } from "./components/WindowDetail";
import {
  isSettingsShortcut,
  type SummaryOutputLanguage,
  type TerminalGroupingMode
} from "./userPreferences";
import {
  decodeQuickKeyInput,
  readCustomQuickKeys,
  writeCustomQuickKeys,
  type CustomQuickKey
} from "./terminalQuickKeys";
import { ensureDesktopNotificationPermission, showAgentTaskDesktopNotification } from "./desktopNotifications";
import {
  clearTerminalNotifications,
  deleteTerminalNotification,
  findNewUnreadNotifications,
  flattenTreeWindows,
  markTerminalNotificationRead,
  markTerminalViewed,
  syncTerminalNotifications,
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
import {
  applyUiInvalidation,
  nextUiEventReconnectDelay,
  parseUiEvent,
  scheduleWindowActivityRefresh
} from "./uiEvents";
import type { BootstrapClientInput, Client, TreeFolder } from "./types";
type TerminalViewportMode = "desktop" | "phone" | "fixed";

const TERMINAL_VIEWPORT_STORAGE_KEY = "web-terminal-acp:terminal-viewport-mode";
const TERMINAL_ENTER_INPUT = "\r";

type TerminalRouteSelection = {
  clientId: string | null;
  windowId: string | null;
};

type DeleteWindowVariables = {
  clientId: string;
  windowId: string;
  nextWindowId: string | null;
};

type CreateWindowVariables = {
  clientId: string;
  cwd?: string | null;
  folder_path?: string | null;
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

function isTerminalSwitcherShortcut(event: KeyboardEvent): boolean {
  const key = event.key.toLocaleLowerCase();
  return event.altKey && !event.ctrlKey && !event.metaKey && (event.code === "KeyW" || key === "w" || event.keyCode === 87);
}

function isAgentRecordShortcut(event: KeyboardEvent): boolean {
  const key = event.key.toLocaleLowerCase();
  return event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && (event.code === "KeyR" || key === "r" || event.keyCode === 82);
}

function isNewTerminalShortcut(event: KeyboardEvent): boolean {
  const key = event.key.toLocaleLowerCase();
  return event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && (event.code === "KeyN" || key === "n" || event.keyCode === 78);
}

function isNewTerminalByProjectShortcut(event: KeyboardEvent): boolean {
  const key = event.key.toLocaleLowerCase();
  return event.altKey && event.shiftKey && !event.ctrlKey && !event.metaKey && (event.code === "KeyN" || key === "n" || event.keyCode === 78);
}

function isLocateSelectedTerminalShortcut(event: KeyboardEvent): boolean {
  const key = event.key.toLocaleLowerCase();
  return event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && (event.code === "KeyL" || key === "l" || event.keyCode === 76);
}

function isGitDiffShortcut(event: KeyboardEvent): boolean {
  const key = event.key.toLocaleLowerCase();
  return event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && (event.code === "KeyG" || key === "g" || event.keyCode === 71);
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
  useVisualViewportHeightCssVariable();

  const [selectedClientId, setSelectedClientId] = useState<string | null>(null);
  const [selectedWindowId, setSelectedWindowId] = useState<string | null>(null);
  const [routeSelectionRequest, setRouteSelectionRequest] = useState<TerminalRouteSelection | null>(
    readTerminalRouteSelection
  );
  const [showBootstrapForm, setShowBootstrapForm] = useState(false);
  const [bootstrapFailed, setBootstrapFailed] = useState(false);
  const [updateMessage, setUpdateMessage] = useState<string | null>(null);
  const [updateFailed, setUpdateFailed] = useState(false);
  const [terminalSwitcherOpen, setTerminalSwitcherOpen] = useState(false);
  const [terminalSwitcherMode, setTerminalSwitcherMode] = useState<TerminalSwitcherMode>("recent");
  const [projectTerminalPickerOpen, setProjectTerminalPickerOpen] = useState(false);
  const [mobileTerminalActive, setMobileTerminalActive] = useState(false);
  const [detailPanelOpen, setDetailPanelOpen] = useState(false);
  const [terminalViewportMode, setTerminalViewportMode] = useState<TerminalViewportMode>(readTerminalViewportMode);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [summaryOutputLanguage, setSummaryOutputLanguage] = useState<SummaryOutputLanguage>(
    () => readInitialSettings().summaryOutputLanguage
  );
  const [terminalGroupingMode, setTerminalGroupingMode] = useState<TerminalGroupingMode>(
    () => readInitialSettings().terminalGroupingMode
  );
  const [desktopNotificationsEnabled, setDesktopNotificationsEnabled] = useState(
    () => readInitialSettings().desktopNotificationsEnabled
  );
  const [customQuickKeys, setCustomQuickKeys] = useState<CustomQuickKey[]>(readCustomQuickKeys);
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
  const [terminalListLocateSignal, setTerminalListLocateSignal] = useState(0);
  const [terminalNotifications, setTerminalNotifications] = useState<TerminalNotification[]>([]);
  const notificationPreviousRef = useRef<TerminalNotification[]>([]);
  const virtualKeysPreferenceTouchedRef = useRef(false);
  const terminalControlsRef = useRef<HTMLDivElement | null>(null);
  const terminalPaneRef = useRef<TerminalPaneHandle | null>(null);
  const queryClient = useQueryClient();
  const agentRecordModal = useAgentRecordData({
    clientId: selectedClientId,
    windowId: selectedWindowId,
    enabled: agentRecordModalOpen
  });

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    const delayedRefreshTimers = new Map<string, number>();
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
        trackDelayedRefresh(event.client_id, scheduleWindowActivityRefresh(queryClient, event, (timer) => {
          if (event.client_id !== null && delayedRefreshTimers.get(event.client_id) === timer) {
            delayedRefreshTimers.delete(event.client_id);
          }
        }));
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
      })
      .catch(() => {});
  }, [queryClient]);

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

  const handleCustomQuickKeysChange = useCallback((quickKeys: CustomQuickKey[]) => {
    writeCustomQuickKeys(quickKeys);
    setCustomQuickKeys(quickKeys);
  }, []);

  const submitCustomQuickKey = useCallback((quickKey: CustomQuickKey) => {
    return terminalPaneRef.current?.submitQuickInput(decodeQuickKeyInput(quickKey.input)) ?? false;
  }, []);

  const closeTerminalSwitcher = useCallback(() => {
    setTerminalSwitcherOpen(false);
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
  const treeFolders = useMemo(
    () => mergeTreeWithActivity(treeQuery.data, windowActivityMap(windowActivityQuery.data)),
    [treeQuery.data, windowActivityQuery.data]
  );
  const projectPaths = useMemo(
    () => collectCreatableProjectPaths(treeFolders ?? []),
    [treeFolders]
  );
  const projectSummariesQuery = useQuery({
    queryKey: ["project-summaries", selectedClientId],
    queryFn: () => fetchProjectSummaries(selectedClientId as string),
    enabled: selectedClientId !== null && projectTerminalPickerOpen
  });
  const selectedTreeWindow = findTreeWindow(treeFolders, selectedWindowId);
  const selectedWindowTitle = selectedTreeWindow?.title ?? null;
  const createMutation = useMutation({
    mutationFn: (variables: CreateWindowVariables) =>
      createWindow(variables.clientId, {
        cwd: variables.cwd,
        folder_path: variables.folder_path
      }),
    onSuccess: (window) => {
      queryClient.invalidateQueries({ queryKey: ["tree", window.client_id] });
      queryClient.invalidateQueries({ queryKey: ["window-activity", window.client_id] });
      setSelectedClientId(window.client_id);
      setSelectedWindowId(window.id);
      setRouteSelectionRequest(null);
      setAgentRecordModalOpen(false);
      writeTerminalRoute(window.client_id, window.id, "push");
      persistTerminalRecent(window.client_id, window.id, window.title);
      setProjectTerminalPickerOpen(false);
    }
  });
  const deleteMutation = useMutation({
    mutationFn: ({ clientId, windowId }: DeleteWindowVariables) => deleteWindow(clientId, windowId),
    onSuccess: (_result, { clientId, windowId, nextWindowId }) => {
      queryClient.invalidateQueries({ queryKey: ["tree", clientId] });
      queryClient.invalidateQueries({ queryKey: ["window-activity", clientId] });
      setTerminalNotifications((current) => current.filter((notification) => notification.windowId !== windowId));
      setSelectedWindowId((currentWindowId) => {
        if (currentWindowId !== windowId) {
          return currentWindowId;
        }

        setRouteSelectionRequest(null);
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
  const bootstrapMutation = useMutation({
    mutationFn: bootstrapClient,
    onMutate: () => {
      setBootstrapFailed(false);
    },
    onSuccess: () => {
      setShowBootstrapForm(false);
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
        setAgentRecordModalOpen(false);
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
      setSelectedWindowId(requestedClient ? routeSelectionRequest.windowId : null);
      setAgentRecordModalOpen(false);
      if (requestedClient === null && routeSelectionRequest.clientId !== null) {
        writeTerminalRoute(nextClient.id, null, "replace");
      }
      setRouteSelectionRequest(null);
      return;
    }

    if (selectedClientId !== null && clients.some((client) => client.id === selectedClientId)) {
      return;
    }

    const preferredClient = clients.find((client) => client.runtime === "local") ?? clients[0];
    setSelectedClientId(preferredClient.id);
    setSelectedWindowId(null);
    setAgentRecordModalOpen(false);
  }, [clientsQuery.data, routeSelectionRequest, selectedClientId]);

  const triggerTerminalSwitcherShortcut = useCallback(() => {
    if (terminalSwitcherOpen) {
      setTerminalSwitcherMode((currentMode) => (currentMode === "recent" ? "tree" : "recent"));
      return;
    }

    setTerminalSwitcherMode("recent");
    setTerminalSwitcherOpen(true);
  }, [terminalSwitcherOpen]);

  const triggerNewTerminalShortcut = useCallback(() => {
    if (selectedClientId === null || createMutation.isPending) {
      return;
    }

    const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
    if (isRemoteClientOffline(client)) {
      return;
    }

    createMutation.mutate({ clientId: selectedClientId });
  }, [clientsQuery.data, createMutation, selectedClientId]);

  const triggerNewTerminalByProjectShortcut = useCallback(() => {
    if (selectedClientId === null || createMutation.isPending) {
      return;
    }

    const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
    if (isRemoteClientOffline(client)) {
      return;
    }

    setProjectTerminalPickerOpen(true);
    setTerminalSwitcherOpen(false);
  }, [clientsQuery.data, createMutation.isPending, selectedClientId]);

  const handleCreateTerminalAtProjectPath = useCallback((projectPath: string) => {
    if (selectedClientId === null || createMutation.isPending) {
      return;
    }

    const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
    if (isRemoteClientOffline(client)) {
      return;
    }

    createMutation.mutate({
      clientId: selectedClientId,
      cwd: projectPath
    });
  }, [clientsQuery.data, createMutation, selectedClientId]);

  const handleCreateTerminalAtGroup = useCallback((node: SwitcherGroupNode) => {
    if (selectedClientId === null || createMutation.isPending) {
      return;
    }

    const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
    if (isRemoteClientOffline(client)) {
      return;
    }

    createMutation.mutate({
      clientId: selectedClientId,
      ...createWindowInputForGroupNode(node)
    });
  }, [clientsQuery.data, createMutation, selectedClientId]);

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
    setSettingsOpen((open) => !open);
  }, []);

  const toggleNotificationCenter = useCallback(() => {
    setNotificationCenterOpen((open) => !open);
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isTerminalSwitcherShortcut(event)) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      triggerTerminalSwitcherShortcut();
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [triggerTerminalSwitcherShortcut]);

  useEffect(() => {
    if (!terminalSwitcherOpen) {
      setTerminalSwitcherMode("recent");
    }
  }, [terminalSwitcherOpen]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) {
        return;
      }

      if (terminalSwitcherOpen || notificationCenterOpen || showBootstrapForm || terminalControlsOpen || gitDiffBrowserOpen) {
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
    notificationCenterOpen,
    selectedClientId,
    selectedWindowId,
    showBootstrapForm,
    terminalControlsOpen,
    terminalSwitcherOpen
  ]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isAgentRecordShortcut(event)) {
        return;
      }
      if (selectedClientId === null || selectedWindowId === null) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      triggerAgentRecordExpand();
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [selectedClientId, selectedWindowId, triggerAgentRecordExpand]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isLocateSelectedTerminalShortcut(event)) {
        return;
      }
      if (selectedClientId === null || selectedWindowId === null) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      if (event.repeat) {
        return;
      }
      triggerLocateSelectedTerminal();
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [selectedClientId, selectedWindowId, triggerLocateSelectedTerminal]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isGitDiffShortcut(event)) {
        return;
      }
      if (selectedClientId === null || selectedWindowId === null) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      triggerGitDiffBrowser();
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [selectedClientId, selectedWindowId, triggerGitDiffBrowser]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isSettingsShortcut(event)) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      toggleSettings();
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [toggleSettings]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (isNewTerminalByProjectShortcut(event)) {
        event.preventDefault();
        event.stopPropagation();
        triggerNewTerminalByProjectShortcut();
        return;
      }

      if (!isNewTerminalShortcut(event)) {
        return;
      }
      if (selectedClientId === null || createMutation.isPending) {
        return;
      }

      const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
      if (isRemoteClientOffline(client)) {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      triggerNewTerminalShortcut();
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [
    clientsQuery.data,
    createMutation,
    selectedClientId,
    triggerNewTerminalByProjectShortcut,
    triggerNewTerminalShortcut
  ]);

  useEffect(() => {
    setTerminalSwitcherOpen(false);
    setProjectTerminalPickerOpen(false);
    setTerminalControlsOpen(false);
    setTerminalImmersive(false);
    setNotificationCenterOpen(false);
    setGitDiffBrowserOpen(false);
    setAgentRecordModalOpen(false);
  }, [selectedClientId]);

  useEffect(() => {
    if (selectedClientId === null) {
      setTerminalNotifications([]);
      notificationPreviousRef.current = [];
      return;
    }

    const next = syncTerminalNotifications(selectedClientId, treeFolders);
    const newlyUnread = findNewUnreadNotifications(notificationPreviousRef.current, next);
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
  }, [desktopNotificationsEnabled, selectedClientId, treeFolders]);

  useEffect(() => {
    if (selectedClientId === null || selectedWindowId === null || treeFolders === undefined) {
      return;
    }

    if (typeof document !== "undefined" && document.visibilityState !== "visible") {
      return;
    }

    const treeWindow = flattenTreeWindows(treeFolders).find((window) => window.id === selectedWindowId);
    if (!treeWindow?.last_agent_task_completed_at) {
      return;
    }

    const next = markTerminalViewed(
      selectedClientId,
      selectedWindowId,
      treeWindow.last_agent_task_completed_at
    );
    notificationPreviousRef.current = next;
    setTerminalNotifications(next);
  }, [selectedClientId, selectedWindowId, treeFolders]);

  useEffect(() => {
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
    setAgentRecordModalOpen(false);
    writeTerminalRoute(selectedClientId, null, "replace");
  }, [routeSelectionRequest, selectedClientId, selectedWindowId, treeFolders, treeQuery.isFetching]);

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
    setRouteSelectionRequest(null);
    setSelectedClientId(clientId);
    setSelectedWindowId(null);
    setMobileTerminalActive(false);
    setDetailPanelOpen(false);
    setTerminalImmersive(false);
    setAgentRecordModalOpen(false);
    writeTerminalRoute(clientId, null, "push");
  };

  const selectWindow = (windowId: string) => {
    if (selectedClientId === null) {
      return;
    }

    setRouteSelectionRequest(null);
    setSelectedWindowId(windowId);
    setDetailPanelOpen(false);
    setAgentRecordModalOpen(false);
    writeTerminalRoute(selectedClientId, windowId, "push");
    focusSelectedTerminal();
  };

  const handleTerminalPaneSelection = useCallback((windowId: string) => {
    if (selectedClientId === null) {
      return;
    }

    setRouteSelectionRequest(null);
    setSelectedWindowId(windowId);
    setAgentRecordModalOpen(false);
    writeTerminalRoute(selectedClientId, windowId, "replace");
  }, [selectedClientId]);

  useEffect(() => {
    if (selectedClientId === null || selectedWindowId === null || selectedWindowTitle === null) {
      return;
    }

    persistTerminalRecent(selectedClientId, selectedWindowId, selectedWindowTitle);
  }, [persistTerminalRecent, selectedClientId, selectedWindowId, selectedWindowTitle]);

  const submitBootstrap = (payload: BootstrapClientInput) => {
    bootstrapMutation.mutate(payload);
  };

  const cancelBootstrap = () => {
    setShowBootstrapForm(false);
    setBootstrapFailed(false);
    bootstrapMutation.reset();
  };

  const unreadNotificationCount = terminalNotifications.filter((notification) => !notification.read).length;
  const hasUnreadNotification = (windowId: string) =>
    terminalNotifications.some((notification) => notification.windowId === windowId && !notification.read);
  const selectedClient = clientsQuery.data?.find((client) => client.id === selectedClientId) ?? null;
  const selectedClientOffline = isRemoteClientOffline(selectedClient);
  const agentPreviewCanSendQuickInput = terminalConnectionStatus === "connected";
  const createErrorMessage = createMutation.error instanceof Error
    ? createMutation.error.message
    : "Failed to create terminal.";
  const deleteErrorMessage = deleteMutation.error instanceof Error
    ? deleteMutation.error.message
    : "Failed to delete terminal.";

  const deletingWindowId = deleteMutation.isPending ? deleteMutation.variables?.windowId ?? null : null;

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
    const next = markTerminalNotificationRead(notification.clientId, notification);
    notificationPreviousRef.current = next;
    setTerminalNotifications(next);
    setNotificationCenterOpen(false);
    if (notification.clientId !== selectedClientId) {
      setRouteSelectionRequest(null);
      setSelectedClientId(notification.clientId);
    }
    setSelectedWindowId(notification.windowId);
    setDetailPanelOpen(false);
    setAgentRecordModalOpen(false);
    setMobileTerminalActive(true);
    writeTerminalRoute(notification.clientId, notification.windowId, "push");
    focusSelectedTerminal();
  }, [focusSelectedTerminal, selectedClientId]);

  const handleDeleteNotification = useCallback((notification: TerminalNotification) => {
    const next = deleteTerminalNotification(notification.clientId, notification);
    notificationPreviousRef.current = next;
    setTerminalNotifications(next);
  }, []);

  const handleClearNotifications = useCallback(() => {
    if (selectedClientId === null) {
      return;
    }

    const next = clearTerminalNotifications(selectedClientId);
    notificationPreviousRef.current = next;
    setTerminalNotifications(next);
  }, [selectedClientId]);

  const mobileShortcutActions = useMemo(
    () => [
      {
        id: "switch-terminal",
        label: "切换终端",
        hint: "Alt+W",
        onPress: triggerTerminalSwitcherShortcut
      },
      {
        id: "new-terminal",
        label: "新建终端",
        hint: "Alt+N",
        disabled: selectedClientId === null || createMutation.isPending || selectedClientOffline,
        onPress: triggerNewTerminalShortcut
      },
      {
        id: "new-terminal-project",
        label: "按项目新建",
        hint: "Shift+Alt+N",
        disabled: selectedClientId === null || createMutation.isPending || selectedClientOffline,
        onPress: triggerNewTerminalByProjectShortcut
      },
      {
        id: "quick-input",
        label: "快速输入",
        hint: "Alt+I",
        disabled: selectedClientId === null || selectedWindowId === null,
        onPress: triggerQuickInput
      },
      {
        id: "expand-record",
        label: "展开 Agent 记录",
        hint: "Alt+R",
        disabled: selectedClientId === null || selectedWindowId === null,
        onPress: triggerAgentRecordExpand
      },
      {
        id: "locate-terminal",
        label: "定位当前终端",
        hint: "Alt+L",
        disabled: selectedClientId === null || selectedWindowId === null,
        onPress: triggerLocateSelectedTerminal
      },
      {
        id: "git-diff",
        label: "Git diff",
        hint: "Alt+G",
        disabled: selectedClientId === null || selectedWindowId === null,
        onPress: triggerGitDiffBrowser
      },
      {
        id: "notifications",
        label: "通知中心",
        badge: unreadNotificationCount,
        onPress: toggleNotificationCenter
      },
      {
        id: "settings",
        label: "设置",
        hint: "Alt+,",
        onPress: toggleSettings
      }
    ],
    [
      createMutation.isPending,
      selectedClientId,
      selectedClientOffline,
      selectedWindowId,
      triggerAgentRecordExpand,
      triggerGitDiffBrowser,
      triggerLocateSelectedTerminal,
      triggerNewTerminalByProjectShortcut,
      triggerNewTerminalShortcut,
      triggerQuickInput,
      triggerTerminalSwitcherShortcut,
      toggleNotificationCenter,
      toggleSettings,
      unreadNotificationCount
    ]
  );

  return (
    <main
      className={[
        "app-shell",
        mobileTerminalActive ? "mobile-terminal-active" : "",
        detailPanelOpen ? "detail-panel-open" : "",
        terminalImmersive ? "terminal-immersive" : "",
      ].filter(Boolean).join(" ")}
    >
      <div className="notification-bell-anchor">
        <NotificationBellButton
          unreadCount={unreadNotificationCount}
          isOpen={notificationCenterOpen}
          onClick={() => setNotificationCenterOpen((isOpen) => !isOpen)}
        />
      </div>
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="sidebar-title-row">
            <h1>Web Terminal ACP</h1>
          </div>
          <div className="sidebar-actions">
            <button
              type="button"
              disabled={bootstrapMutation.isPending}
              onClick={() => {
                setBootstrapFailed(false);
                setShowBootstrapForm(true);
              }}
            >
              Add client
            </button>
            <button
              type="button"
              title="Alt+N"
              disabled={selectedClientId === null || createMutation.isPending || selectedClientOffline}
              onClick={() => selectedClientId !== null && createMutation.mutate({ clientId: selectedClientId })}
            >
              New terminal
            </button>
          </div>
        </div>
        {showBootstrapForm && (
          <BootstrapClientForm
            isSubmitting={bootstrapMutation.isPending}
            onCancel={cancelBootstrap}
            onSubmit={submitBootstrap}
          />
        )}
        {bootstrapFailed && (
          <p className="error" role="alert">
            Bootstrap failed. Check host, key, dependencies, and server URL.
          </p>
        )}
        {clientsQuery.isLoading && <p className="muted">Loading clients...</p>}
        {clientsQuery.isError && <p className="error" role="alert">Failed to load clients.</p>}
        {clientsQuery.data && (
          <ClientList
            clients={clientsQuery.data}
            selectedClientId={selectedClientId}
            updatingClientId={updateMutation.isPending ? updateMutation.variables ?? null : null}
            onSelectClient={selectClient}
            onUpdateClient={(clientId) => updateMutation.mutate(clientId)}
          />
        )}
        {updateMessage && <p className="muted">{updateMessage}</p>}
        {updateFailed && <p className="error" role="alert">Client update failed to start.</p>}
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
            creatingTerminal={createMutation.isPending}
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
              <div className="terminal-controls-menu" role="menu">
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
                  <strong>{terminalQuickInputOpen ? "Open" : "Alt+I"}</strong>
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
                  <strong>Alt+G</strong>
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
                  <strong>Alt+,</strong>
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
          layoutVersion={
            (mobileTerminalActive ? 1 : 0)
            + (terminalImmersive ? 2 : 0)
            + (detailPanelOpen ? 4 : 0)
          }
          virtualKeysVisible={virtualKeysVisible}
        />
      </section>
      <aside className="detail-panel">
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
        terminalGroupingMode={terminalGroupingMode}
        summaryOutputLanguage={summaryOutputLanguage}
        isOpen={terminalSwitcherOpen}
        selectedWindowId={selectedWindowId}
        hasUnreadNotification={hasUnreadNotification}
        onClose={closeTerminalSwitcher}
        onSelectWindow={selectWindow}
        onCreateTerminalAtGroup={handleCreateTerminalAtGroup}
        creatingTerminal={createMutation.isPending}
        createTerminalDisabled={selectedClientOffline}
      />
      <ProjectTerminalPicker
        isOpen={projectTerminalPickerOpen}
        projectPaths={projectPaths}
        projectSummaries={projectSummariesQuery.data ?? []}
        loadingProjects={treeQuery.isFetching || windowActivityQuery.isFetching}
        creatingTerminal={createMutation.isPending}
        createTerminalDisabled={selectedClientOffline}
        onClose={() => {
          setProjectTerminalPickerOpen(false);
          focusSelectedTerminal();
        }}
        onCreateTerminal={handleCreateTerminalAtProjectPath}
      />
      <SettingsModal
        isOpen={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        summaryOutputLanguage={summaryOutputLanguage}
        terminalGroupingMode={terminalGroupingMode}
        desktopNotificationsEnabled={desktopNotificationsEnabled}
        customQuickKeys={customQuickKeys}
        onSummaryOutputLanguageChange={setSummaryOutputLanguage}
        onTerminalGroupingModeChange={setTerminalGroupingMode}
        onDesktopNotificationsEnabledChange={setDesktopNotificationsEnabled}
        onCustomQuickKeysChange={handleCustomQuickKeysChange}
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
          && !projectTerminalPickerOpen
          && !notificationCenterOpen
          && !settingsOpen
          && !showBootstrapForm
          && !terminalQuickInputOpen
          && !gitDiffBrowserOpen
        }
        actions={mobileShortcutActions}
      />
    </main>
  );
}
