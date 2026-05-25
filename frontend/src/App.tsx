import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  bootstrapClient,
  createWindow,
  deleteWindow,
  fetchClients,
  fetchTree,
  fetchWindowActivity,
  recordTerminalRecent,
  updateClient
} from "./api";
import { BootstrapClientForm } from "./components/BootstrapClientForm";
import { ClientList } from "./components/ClientList";
import { FolderTree } from "./components/FolderTree";
import { SearchPanel } from "./components/SearchPanel";
import { MobileShortcutFab } from "./components/MobileShortcutFab";
import { NotificationBellButton, NotificationCenter } from "./components/NotificationCenter";
import { TerminalPane, type TerminalPaneHandle } from "./components/TerminalPane";
import { useMobileLayout } from "./hooks/useMobileLayout";
import { readInitialSettings, SettingsModal } from "./components/SettingsModal";
import { TerminalSwitcher, type TerminalSwitcherMode } from "./components/TerminalSwitcher";
import { WindowDetail } from "./components/WindowDetail";
import {
  isSettingsShortcut,
  type SummaryOutputLanguage,
  type TerminalGroupingMode
} from "./userPreferences";
import { ensureDesktopNotificationPermission, showAgentTaskDesktopNotification } from "./desktopNotifications";
import {
  findNewUnreadNotifications,
  flattenTreeWindows,
  markTerminalNotificationRead,
  markTerminalViewed,
  syncTerminalNotifications,
  type TerminalNotification
} from "./terminalNotifications";
import {
  activityHasWorkingTerminal,
  mergeTreeWithActivity,
  windowActivityMap
} from "./terminalTree";
import type { BootstrapClientInput, TreeFolder } from "./types";
type TerminalViewportMode = "desktop" | "phone" | "fixed";

const TERMINAL_VIEWPORT_STORAGE_KEY = "web-terminal-acp:terminal-viewport-mode";

type TerminalRouteSelection = {
  clientId: string | null;
  windowId: string | null;
};

type DeleteWindowVariables = {
  clientId: string;
  windowId: string;
  nextWindowId: string | null;
};

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

export default function App() {
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
  const [terminalControlsOpen, setTerminalControlsOpen] = useState(false);
  const [virtualKeysVisible, setVirtualKeysVisible] = useState(false);
  const [terminalImmersive, setTerminalImmersive] = useState(false);
  const [notificationCenterOpen, setNotificationCenterOpen] = useState(false);
  const [agentRecordExpandSignal, setAgentRecordExpandSignal] = useState(0);
  const [terminalNotifications, setTerminalNotifications] = useState<TerminalNotification[]>([]);
  const notificationPreviousRef = useRef<TerminalNotification[]>([]);
  const terminalControlsRef = useRef<HTMLDivElement | null>(null);
  const terminalPaneRef = useRef<TerminalPaneHandle | null>(null);
  const queryClient = useQueryClient();

  const persistTerminalRecent = useCallback((clientId: string, windowId: string, title: string) => {
    void recordTerminalRecent(clientId, { window_id: windowId, title })
      .then(() => {
        queryClient.invalidateQueries({ queryKey: ["terminal-recents", clientId] });
      })
      .catch(() => {});
  }, [queryClient]);

  const focusSelectedTerminal = useCallback(() => {
    requestAnimationFrame(() => terminalPaneRef.current?.focus());
  }, []);

  const closeTerminalSwitcher = useCallback(() => {
    setTerminalSwitcherOpen(false);
    focusSelectedTerminal();
  }, [focusSelectedTerminal]);
  const isMobileLayout = useMobileLayout();
  const clientsQuery = useQuery({ queryKey: ["clients"], queryFn: fetchClients, refetchInterval: 10000 });
  const treeQuery = useQuery({
    queryKey: ["tree", selectedClientId],
    queryFn: () => fetchTree(selectedClientId as string),
    enabled: selectedClientId !== null,
    refetchInterval: 10000
  });
  const needsRuntimeTags =
    terminalGroupingMode === "project-topic" || terminalSwitcherOpen;
  const windowActivityQuery = useQuery({
    queryKey: ["window-activity", selectedClientId, false],
    queryFn: () => fetchWindowActivity(selectedClientId as string),
    enabled: selectedClientId !== null && treeQuery.isSuccess,
    refetchInterval: (query) => (activityHasWorkingTerminal(query.state.data) ? 3000 : 10000)
  });
  const windowActivityTagsQuery = useQuery({
    queryKey: ["window-activity", selectedClientId, true],
    queryFn: () => fetchWindowActivity(selectedClientId as string, { includeRuntimeTags: true }),
    enabled: selectedClientId !== null && treeQuery.isSuccess && needsRuntimeTags,
    refetchInterval: (query) => (activityHasWorkingTerminal(query.state.data) ? 3000 : 10000)
  });
  const windowActivityData = needsRuntimeTags
    ? windowActivityTagsQuery.data ?? windowActivityQuery.data
    : windowActivityQuery.data;
  const treeFolders = useMemo(
    () => mergeTreeWithActivity(treeQuery.data, windowActivityMap(windowActivityData)),
    [treeQuery.data, windowActivityData]
  );
  const selectedTreeWindow = findTreeWindow(treeFolders, selectedWindowId);
  const selectedWindowTitle = selectedTreeWindow?.title ?? null;
  const createMutation = useMutation({
    mutationFn: (clientId: string) => createWindow(clientId),
    onSuccess: (window) => {
      queryClient.invalidateQueries({ queryKey: ["tree", window.client_id] });
      queryClient.invalidateQueries({ queryKey: ["window-activity", window.client_id] });
      setSelectedClientId(window.client_id);
      setSelectedWindowId(window.id);
      setRouteSelectionRequest(null);
      writeTerminalRoute(window.client_id, window.id, "push");
      persistTerminalRecent(window.client_id, window.id, window.title);
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
    if (client?.runtime === "remote" && client.status !== "ONLINE") {
      return;
    }

    createMutation.mutate(selectedClientId);
  }, [clientsQuery.data, createMutation, selectedClientId]);

  const triggerAgentRecordExpand = useCallback(() => {
    if (selectedClientId === null || selectedWindowId === null) {
      return;
    }

    setAgentRecordExpandSignal((signal) => signal + 1);
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

      if (terminalSwitcherOpen || notificationCenterOpen || showBootstrapForm || terminalControlsOpen) {
        return;
      }

      const target = event.target;
      const activeElement = document.activeElement;
      const focusedInXterm = isXtermInput(target) || isXtermInput(activeElement);
      if (isBlockingTextInput(target) || isBlockingTextInput(activeElement)) {
        return;
      }

      if (selectedClientId !== null && selectedWindowId !== null) {
        if (!focusedInXterm) {
          event.preventDefault();
        }
        focusSelectedTerminal();
      }
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [
    focusSelectedTerminal,
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
      if (!isNewTerminalShortcut(event)) {
        return;
      }
      if (selectedClientId === null || createMutation.isPending) {
        return;
      }

      const client = clientsQuery.data?.find((candidate) => candidate.id === selectedClientId);
      if (client?.runtime === "remote" && client.status !== "ONLINE") {
        return;
      }

      event.preventDefault();
      event.stopPropagation();
      triggerNewTerminalShortcut();
    };

    window.addEventListener("keydown", handleKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", handleKeyDown, { capture: true });
  }, [clientsQuery.data, createMutation, selectedClientId, triggerNewTerminalShortcut]);

  useEffect(() => {
    setTerminalSwitcherOpen(false);
    setTerminalControlsOpen(false);
    setTerminalImmersive(false);
    setNotificationCenterOpen(false);
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
    writeTerminalRoute(selectedClientId, null, "replace");
  }, [routeSelectionRequest, selectedClientId, selectedWindowId, treeFolders, treeQuery.isFetching]);

  useEffect(() => {
    window.localStorage.setItem(TERMINAL_VIEWPORT_STORAGE_KEY, terminalViewportMode);
  }, [terminalViewportMode]);

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
    writeTerminalRoute(clientId, null, "push");
  };

  const selectWindow = (windowId: string) => {
    if (selectedClientId === null) {
      return;
    }

    setRouteSelectionRequest(null);
    setSelectedWindowId(windowId);
    setDetailPanelOpen(false);
    writeTerminalRoute(selectedClientId, windowId, "push");
    focusSelectedTerminal();
  };

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
  const selectedClientOffline = selectedClient?.runtime === "remote" && selectedClient.status !== "ONLINE";
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
    setTerminalNotifications(
      markTerminalNotificationRead(notification.clientId, notification)
    );
    setNotificationCenterOpen(false);
    if (notification.clientId !== selectedClientId) {
      setRouteSelectionRequest(null);
      setSelectedClientId(notification.clientId);
    }
    setSelectedWindowId(notification.windowId);
    setDetailPanelOpen(false);
    setMobileTerminalActive(true);
    writeTerminalRoute(notification.clientId, notification.windowId, "push");
    focusSelectedTerminal();
  }, [focusSelectedTerminal, selectedClientId]);

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
        id: "expand-record",
        label: "展开 Agent 记录",
        hint: "Alt+R",
        disabled: selectedClientId === null || selectedWindowId === null,
        onPress: triggerAgentRecordExpand
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
      triggerNewTerminalShortcut,
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
      <aside className="sidebar">
        <div className="sidebar-header">
          <div className="sidebar-title-row">
            <h1>Web Terminal ACP</h1>
            <NotificationBellButton
              unreadCount={unreadNotificationCount}
              isOpen={notificationCenterOpen}
              onClick={() => setNotificationCenterOpen((isOpen) => !isOpen)}
            />
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
              onClick={() => selectedClientId !== null && createMutation.mutate(selectedClientId)}
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
            deletingWindowId={deletingWindowId}
            hasUnreadNotification={hasUnreadNotification}
            onSelectWindow={(window) => selectWindow(window.id)}
            onDeleteWindow={(window) => requestDeleteWindow(window.id, window.title)}
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
                  onClick={() => setVirtualKeysVisible((isVisible) => !isVisible)}
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
          viewportMode={terminalViewportMode}
          layoutVersion={(mobileTerminalActive ? 1 : 0) + (terminalImmersive ? 2 : 0)}
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
          agentRecordExpandSignal={agentRecordExpandSignal}
        />
        <SearchPanel clientId={selectedClientId} onSelectWindowId={selectWindow} />
      </aside>
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
      />
      <SettingsModal
        isOpen={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        summaryOutputLanguage={summaryOutputLanguage}
        terminalGroupingMode={terminalGroupingMode}
        desktopNotificationsEnabled={desktopNotificationsEnabled}
        onSummaryOutputLanguageChange={setSummaryOutputLanguage}
        onTerminalGroupingModeChange={setTerminalGroupingMode}
        onDesktopNotificationsEnabledChange={setDesktopNotificationsEnabled}
      />
      <NotificationCenter
        isOpen={notificationCenterOpen}
        notifications={terminalNotifications}
        onClose={() => setNotificationCenterOpen(false)}
        onSelectNotification={handleSelectNotification}
      />
      <MobileShortcutFab
        visible={
          isMobileLayout
          && !terminalSwitcherOpen
          && !notificationCenterOpen
          && !settingsOpen
          && !showBootstrapForm
        }
        actions={mobileShortcutActions}
      />
    </main>
  );
}
