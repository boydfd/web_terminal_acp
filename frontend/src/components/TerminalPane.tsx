import { Terminal, type ITheme } from "@xterm/xterm";
import {
  forwardRef,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { terminalWebSocketUrl } from "../api";
import { createTerminalOutputBuffer } from "../terminalOutputBuffer";
import {
  copyTerminalSelection,
  pasteClipboardEventToTerminal,
  pasteClipboardToTerminal,
  terminalClipboardShortcutAction,
  writeClipboardText
} from "../terminalClipboard";
import { parseTerminalSocketControlMessage } from "../terminalSocketProtocol";
import {
  fitTerminalToContainer,
  isTerminalViewportFilled,
  readTerminalCanvas,
  terminalViewportNeedsRefit,
} from "../terminalFit";
import {
  claimActiveTerminalView,
  isTerminalViewLowPriority,
  TERMINAL_VIEW_PRIORITY_CHANGED_EVENT,
} from "../terminalViewPriority";
import { terminalTouchScrollSequence } from "../terminalTouchScroll";
import { createBrowserUuid } from "../uuid";
import {
  clearQuickInputDraft,
  quickInputDraftStorageKey,
  readQuickInputDraft,
  TerminalQuickInput,
  writeQuickInputDraft
} from "./TerminalQuickInput";
import type { CustomQuickKey } from "../terminalQuickKeys";

type TerminalViewportMode = "desktop" | "phone" | "fixed";
export type TerminalConnectionStatus = "connecting" | "connected" | "reconnecting" | "unavailable" | "error";

type TerminalPaneProps = {
  clientId: string | null;
  windowId: string | null;
  viewportMode?: TerminalViewportMode;
  layoutVersion?: number;
  virtualKeysVisible?: boolean;
  onTerminalSelection?: (windowId: string) => void;
  onQuickInputOpenChange?: (open: boolean) => void;
  onQuickInputDraftChange?: (draft: string) => void;
  customQuickKeys?: CustomQuickKey[];
  onCustomQuickKeySubmit?: (quickKey: CustomQuickKey) => boolean;
  onTerminalConnectionStatusChange?: (status: TerminalConnectionStatus) => void;
  webSocketUrl?: (clientId: string, windowId: string, viewId: string) => string;
  selectionEnabled?: boolean;
  priorityEnabled?: boolean;
  autoFocus?: boolean;
  theme?: ITheme;
};

type TerminalWriteTestHook = (data: string | Uint8Array, parsedAt: number) => void;
type TerminalInteractiveOutputTestHook = (data: string | Uint8Array, receivedAt: number) => void;
type TerminalDataTestHook = (data: string, onDataAt: number) => void;

declare global {
  interface Window {
    __WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__?: TerminalWriteTestHook;
    __WEB_TERMINAL_TEST_ON_INTERACTIVE_OUTPUT__?: TerminalInteractiveOutputTestHook;
    __WEB_TERMINAL_TEST_ON_TERMINAL_DATA__?: TerminalDataTestHook;
  }
}

export type TerminalPaneHandle = {
  focus: () => void;
  refit: () => void;
  openQuickInput: () => void;
  setQuickInputDraft: (draft: string) => void;
  submitQuickInput: (draft?: string) => boolean;
  connectionStatus: () => TerminalConnectionStatus;
};

type VirtualKey = {
  label: string;
  value: string;
};

type TouchScrollGesture = {
  pointerId: number;
  startX: number;
  startY: number;
  lastY: number;
  accumulatedY: number;
  scrolling: boolean;
};

const VIRTUAL_KEYS: VirtualKey[] = [
  { label: "Esc", value: "\x1b" },
  { label: "Tab", value: "\t" },
  { label: "Ctrl-C", value: "\x03" },
  { label: "Ctrl-D", value: "\x04" },
  { label: "Ctrl-L", value: "\x0c" },
  { label: "Ctrl-A", value: "\x01" },
  { label: "Ctrl-E", value: "\x05" },
  { label: "Ctrl-U", value: "\x15" },
  { label: "↑", value: "\x1b[A" },
  { label: "↓", value: "\x1b[B" },
  { label: "←", value: "\x1b[D" },
  { label: "→", value: "\x1b[C" },
  { label: "Home", value: "\x1b[H" },
  { label: "End", value: "\x1b[F" },
  { label: "PgUp", value: "\x1b[5~" },
  { label: "PgDn", value: "\x1b[6~" },
];

const RECONNECT_DELAYS_MS = [500, 1000, 2000, 5000, 10000];
const FIT_RETRY_DELAYS_MS = [80, 250, 600, 1200, 2000, 3000, 5000, 10000, 15000, 30000] as const;
const FIT_UNTIL_FILLED_INTERVAL_MS = 150;
const FIT_UNTIL_FILLED_MAX_MS = 60000;
const UNDERSIZED_REFIT_INTERVAL_MS = 2000;
const OUTPUT_REFIT_DEBOUNCE_MS = 50;
const WRITE_PARSED_REFIT_DEBOUNCE_MS = 100;
const RESIZE_OBSERVER_DEBOUNCE_MS = 50;
const TERMINAL_OUTPUT_FLUSH_CHARACTERS = 128 * 1024;
const BACKGROUND_OUTPUT_FLUSH_DELAY_MS = 250;
const BACKGROUND_OUTPUT_FLUSH_CHARACTERS = 1024;
const LOW_PRIORITY_SOCKET_CLOSE_DELAY_MS = 1500;
const VIEW_PRIORITY_RECONCILE_INTERVAL_MS = 750;
const INPUT_VIEW_PRIORITY_CLAIM_INTERVAL_MS = 250;
const PENDING_INPUT_QUEUE_MAX_SIZE = 64;
const TOUCH_SCROLL_START_THRESHOLD_PX = 12;
const TOUCH_SCROLL_FALLBACK_STEP_PX = 18;
const TOUCH_SCROLL_MAX_WHEEL_EVENTS_PER_MOVE = 12;
const NATIVE_TEXT_INPUT_FALLBACK_DELAY_MS = 0;
const NATIVE_TEXT_INPUT_DEDUPE_MS = 250;

type RecentNativeFallbackInput = {
  data: string;
  inputEventSerial: number;
  sentAt: number;
};

type RecentXtermInput = {
  data: string;
  seenAt: number;
};

function terminalStatusLabel(status: TerminalConnectionStatus): string {
  switch (status) {
    case "connected":
      return "Connected";
    case "connecting":
      return "Connecting...";
    case "reconnecting":
      return "Reconnecting...";
    case "unavailable":
      return "Client offline, reconnecting...";
    case "error":
      return "Terminal error";
  }
}

function terminalOutputByteLength(data: string | Uint8Array): number {
  return typeof data === "string" ? new TextEncoder().encode(data).byteLength : data.byteLength;
}

function readTerminalCellHeight(terminal: Terminal, host: HTMLElement): number {
  const core = (terminal as unknown as {
    _core?: {
      _renderService?: {
        dimensions?: {
          css?: {
            cell?: {
              height?: number;
            };
          };
        };
      };
    };
  })._core;
  const measuredHeight = core?._renderService?.dimensions?.css?.cell?.height;
  if (typeof measuredHeight === "number" && measuredHeight > 0) {
    return measuredHeight;
  }

  const rowHeight = terminal.rows > 0 ? host.clientHeight / terminal.rows : 0;
  return rowHeight > 0 ? rowHeight : TOUCH_SCROLL_FALLBACK_STEP_PX;
}

function decodeOsc52ClipboardPayload(data: string): string | null {
  const separatorIndex = data.indexOf(";");
  if (separatorIndex < 0) {
    return null;
  }

  const encoded = data.slice(separatorIndex + 1).replace(/\s/g, "");
  if (!encoded || encoded === "?") {
    return null;
  }

  try {
    const binary = window.atob(encoded);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    return new TextDecoder().decode(bytes);
  } catch {
    return null;
  }
}

export const TerminalPane = forwardRef<TerminalPaneHandle, TerminalPaneProps>(function TerminalPane({
  clientId,
  windowId,
  viewportMode = "desktop",
  layoutVersion = 0,
  virtualKeysVisible = false,
  onTerminalSelection,
  onQuickInputOpenChange,
  onQuickInputDraftChange,
  customQuickKeys = [],
  onCustomQuickKeySubmit,
  onTerminalConnectionStatusChange,
  webSocketUrl = terminalWebSocketUrl,
  selectionEnabled = true,
  priorityEnabled = true,
  autoFocus = true,
  theme,
}, ref) {
  const stageRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const xtermHostRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const touchScrollGestureRef = useRef<TouchScrollGesture | null>(null);
  const socketWorkerRef = useRef<Worker | null>(null);
  const socketOpenRef = useRef(false);
  const activeWindowIdRef = useRef<string | null>(null);
  const initialWindowIdRef = useRef<string | null>(null);
  const viewIdRef = useRef<string>(createBrowserUuid());
  const autoFocusRef = useRef(autoFocus);
  const onTerminalSelectionRef = useRef<TerminalPaneProps["onTerminalSelection"]>(onTerminalSelection);
  const onQuickInputOpenChangeRef = useRef<TerminalPaneProps["onQuickInputOpenChange"]>(onQuickInputOpenChange);
  const onQuickInputDraftChangeRef = useRef<TerminalPaneProps["onQuickInputDraftChange"]>(onQuickInputDraftChange);
  const onTerminalConnectionStatusChangeRef = useRef<TerminalPaneProps["onTerminalConnectionStatusChange"]>(onTerminalConnectionStatusChange);
  const fitAndNotifyResizeRef = useRef<(() => void) | null>(null);
  const claimActiveTerminalViewRef = useRef<(() => void) | null>(null);
  const sendTerminalInputRef = useRef<((data: string) => void) | null>(null);
  const scheduledFitFramesRef = useRef<number[]>([]);
  const scheduledFitTimeoutsRef = useRef<number[]>([]);
  const connectionStatusRef = useRef<TerminalConnectionStatus>("connecting");
  const [pendingClipboardText, setPendingClipboardText] = useState<string | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<TerminalConnectionStatus>("connecting");
  const [quickInputOpen, setQuickInputOpen] = useState(false);
  const [quickInputDraft, setQuickInputDraft] = useState("");
  const hasSelectedWindow = windowId !== null;
  const quickInputStorageKey = clientId !== null && windowId !== null
    ? quickInputDraftStorageKey(clientId, windowId)
    : null;
  const canSendQuickInput = connectionStatus === "connected";

  const updateQuickInputDraft = useCallback((draft: string) => {
    setQuickInputDraft(draft);
    onQuickInputDraftChangeRef.current?.(draft);
    if (quickInputStorageKey !== null) {
      writeQuickInputDraft(quickInputStorageKey, draft);
    }
  }, [quickInputStorageKey]);

  const updateConnectionStatus = useCallback((status: TerminalConnectionStatus) => {
    connectionStatusRef.current = status;
    onTerminalConnectionStatusChangeRef.current?.(status);
    setConnectionStatus(status);
  }, []);

  useEffect(() => {
    autoFocusRef.current = autoFocus;
  }, [autoFocus]);

  useEffect(() => {
    onTerminalSelectionRef.current = onTerminalSelection;
  }, [onTerminalSelection]);

  useEffect(() => {
    onQuickInputOpenChangeRef.current = onQuickInputOpenChange;
  }, [onQuickInputOpenChange]);

  useEffect(() => {
    onQuickInputDraftChangeRef.current = onQuickInputDraftChange;
  }, [onQuickInputDraftChange]);

  useEffect(() => {
    onTerminalConnectionStatusChangeRef.current = onTerminalConnectionStatusChange;
    onTerminalConnectionStatusChangeRef.current?.(connectionStatusRef.current);
  }, [onTerminalConnectionStatusChange]);

  useEffect(() => {
    if (theme) {
      const terminal = terminalRef.current;
      if (terminal) {
        terminal.options.theme = theme;
      }
    }
  }, [theme]);

  useEffect(() => {
    onQuickInputOpenChangeRef.current?.(quickInputOpen);
    return () => {
      if (quickInputOpen) {
        onQuickInputOpenChangeRef.current?.(false);
      }
    };
  }, [quickInputOpen]);

  useEffect(() => {
    if (quickInputStorageKey === null) {
      setQuickInputOpen(false);
      updateQuickInputDraft("");
      return;
    }

    const storedDraft = readQuickInputDraft(quickInputStorageKey);
    setQuickInputDraft(storedDraft);
    onQuickInputDraftChangeRef.current?.(storedDraft);
  }, [quickInputStorageKey, updateQuickInputDraft]);

  const clearScheduledFits = useCallback(() => {
    for (const frame of scheduledFitFramesRef.current) {
      window.cancelAnimationFrame(frame);
    }
    for (const timeout of scheduledFitTimeoutsRef.current) {
      window.clearTimeout(timeout);
    }
    scheduledFitFramesRef.current = [];
    scheduledFitTimeoutsRef.current = [];
  }, []);

  const fitAndNotifyResize = useCallback(() => {
    fitAndNotifyResizeRef.current?.();
  }, []);

  const claimTerminalViewPriority = useCallback(() => {
    claimActiveTerminalViewRef.current?.();
  }, []);

  const scheduleFitAndNotifyResize = useCallback(() => {
    clearScheduledFits();
    fitAndNotifyResize();

    const firstFrame = window.requestAnimationFrame(() => {
      fitAndNotifyResize();
      const secondFrame = window.requestAnimationFrame(fitAndNotifyResize);
      scheduledFitFramesRef.current.push(secondFrame);
    });
    scheduledFitFramesRef.current.push(firstFrame);

    for (const delay of FIT_RETRY_DELAYS_MS) {
      scheduledFitTimeoutsRef.current.push(window.setTimeout(fitAndNotifyResize, delay));
    }
  }, [clearScheduledFits, fitAndNotifyResize]);

  const focusTerminal = useCallback(() => {
    claimTerminalViewPriority();
    const stage = stageRef.current;
    const scrollLeft = stage?.scrollLeft ?? 0;
    const scrollTop = stage?.scrollTop ?? 0;
    terminalRef.current?.focus();
    if (stage) {
      stage.scrollLeft = scrollLeft;
      stage.scrollTop = scrollTop;
    }
  }, [claimTerminalViewPriority]);

  const openQuickInput = useCallback(() => {
    if (clientId === null || windowId === null) {
      return;
    }
    claimTerminalViewPriority();
    setQuickInputOpen(true);
  }, [claimTerminalViewPriority, clientId, windowId]);

  const closeQuickInput = useCallback(() => {
    setQuickInputOpen(false);
    focusTerminal();
  }, [focusTerminal]);

  const sendTerminalInput = (data: string, { focusAfterSend = true }: { focusAfterSend?: boolean } = {}) => {
    claimTerminalViewPriority();
    sendTerminalInputRef.current?.(data);
    if (focusAfterSend) {
      focusTerminal();
    }
  };
  const copyTerminalClipboardSelection = useCallback(async () => {
    const terminal = terminalRef.current;
    if (terminal === null) {
      return false;
    }

    try {
      const copied = await copyTerminalSelection(terminal);
      if (copied) {
        setPendingClipboardText(null);
        focusTerminal();
      }
      return copied;
    } catch {
      return false;
    }
  }, [focusTerminal]);
  const pasteTerminalClipboardText = useCallback(async () => {
    if (connectionStatusRef.current !== "connected") {
      return false;
    }

    try {
      const pasted = await pasteClipboardToTerminal(
        (data) => sendTerminalInput(data, { focusAfterSend: false }),
        terminalRef.current?.modes.bracketedPasteMode ?? false
      );
      if (pasted) {
        focusTerminal();
      }
      return pasted;
    } catch {
      return false;
    }
  }, [focusTerminal]);

  const submitQuickInput = useCallback((draftOverride?: string) => {
    claimTerminalViewPriority();
    const draft = draftOverride ?? quickInputDraft;
    if (draft.length === 0 || connectionStatusRef.current !== "connected") {
      return false;
    }

    sendTerminalInputRef.current?.(draft);
    if (quickInputStorageKey !== null) {
      clearQuickInputDraft(quickInputStorageKey);
    }
    updateQuickInputDraft("");
    setQuickInputOpen(false);
    focusTerminal();
    return true;
  }, [claimTerminalViewPriority, focusTerminal, quickInputDraft, quickInputStorageKey, updateQuickInputDraft]);

  const sendTouchScrollWheelEvents = useCallback((
    deltaY: number,
    clientX: number,
    clientY: number,
  ): number => {
    const terminal = terminalRef.current;
    const host = xtermHostRef.current;
    if (terminal === null || host === null || connectionStatusRef.current !== "connected") {
      return 0;
    }

    const cellHeight = readTerminalCellHeight(terminal, host);
    const result = terminalTouchScrollSequence({
      deltaY,
      clientX,
      clientY,
      hostRect: host.getBoundingClientRect(),
      cols: terminal.cols,
      rows: terminal.rows,
      cellHeight,
      maxWheelEvents: TOUCH_SCROLL_MAX_WHEEL_EVENTS_PER_MOVE,
    });
    if (result === null) {
      return 0;
    }

    sendTerminalInputRef.current?.(result.sequence);
    claimTerminalViewPriority();
    return result.consumedY;
  }, [claimTerminalViewPriority]);

  const handleTerminalPointerDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    focusTerminal();
    if (event.pointerType !== "touch" && event.pointerType !== "pen") {
      touchScrollGestureRef.current = null;
      return;
    }
    if (event.button !== 0) {
      return;
    }
    if (event.target instanceof HTMLElement && event.target.closest(".terminal-quick-input-panel") !== null) {
      return;
    }

    touchScrollGestureRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      lastY: event.clientY,
      accumulatedY: 0,
      scrolling: false,
    };
  }, [focusTerminal]);

  const handleTerminalPointerMove = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const gesture = touchScrollGestureRef.current;
    if (gesture === null || gesture.pointerId !== event.pointerId) {
      return;
    }

    const movedX = event.clientX - gesture.startX;
    const movedY = event.clientY - gesture.startY;
    if (!gesture.scrolling) {
      if (
        Math.abs(movedY) < TOUCH_SCROLL_START_THRESHOLD_PX
        || Math.abs(movedY) <= Math.abs(movedX)
      ) {
        return;
      }
      gesture.scrolling = true;
    }

    event.preventDefault();
    event.stopPropagation();
    const deltaY = gesture.lastY - event.clientY;
    gesture.lastY = event.clientY;
    gesture.accumulatedY += deltaY;
    const consumedY = sendTouchScrollWheelEvents(
      gesture.accumulatedY,
      event.clientX,
      event.clientY,
    );
    if (consumedY !== 0) {
      gesture.accumulatedY -= consumedY;
    }
  }, [sendTouchScrollWheelEvents]);

  const handleTerminalPointerEnd = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const gesture = touchScrollGestureRef.current;
    if (gesture !== null && gesture.pointerId === event.pointerId) {
      touchScrollGestureRef.current = null;
    }
  }, []);

  useImperativeHandle(ref, () => ({
    focus: focusTerminal,
    refit: scheduleFitAndNotifyResize,
    openQuickInput,
    setQuickInputDraft: updateQuickInputDraft,
    submitQuickInput,
    connectionStatus: () => connectionStatusRef.current,
  }), [focusTerminal, openQuickInput, scheduleFitAndNotifyResize, submitQuickInput, updateQuickInputDraft]);

  const sendSelectWindow = useCallback((nextWindowId: string) => {
    if (!selectionEnabled) {
      return;
    }

    const worker = socketWorkerRef.current;
    if (worker === null || !socketOpenRef.current) {
      return;
    }

    worker.postMessage({ type: "json", data: JSON.stringify({ type: "select_window", window_id: nextWindowId }) });
  }, [selectionEnabled]);

  useEffect(() => {
    if (clientId === null || windowId === null) {
      return;
    }

    const initialWindowId = windowId;
    initialWindowIdRef.current = initialWindowId;
    activeWindowIdRef.current = initialWindowId;

    const terminal = new Terminal({
      cursorBlink: true,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
      theme,
    });
    let closedByCleanup = false;
    let disposed = false;
    let openFrame: number | null = null;
    let escapeFocusFrame: number | null = null;
    let canvasResizeObserver: ResizeObserver | null = null;
    const isActive = () => !closedByCleanup && !disposed;

    const resolveFitContainer = (): HTMLElement | null => {
      return xtermHostRef.current ?? containerRef.current;
    };

    let fitUntilFilledTimer: number | null = null;
    let outputRefitTimer: number | null = null;
    let writeParsedRefitTimer: number | null = null;
    let fitUntilFilledStartedAt = Date.now();
    let reconnectAttempt = 0;
    let reconnectTimer: number | null = null;
    let lowPriorityCloseTimer: number | null = null;
    let inputPriorityClaimTimer: number | null = null;
    let lastSentResize: { cols: number; rows: number } | null = null;
    let lastInputPriorityClaimedAt = 0;
    let xtermInputSerial = 0;
    let nativeTextInputEventSerial = 0;
    let activeNativeTextInputEventSerial: number | null = null;
    let nativeTextInputEventClearTimer: number | null = null;
    let nativeInputFallbackTimer: number | null = null;
    let nativeInputFallbackCompositionActive = false;
    let nativeInputCompositionStartValue = "";
    let nativeInputCompositionLatestData = "";
    const recentNativeFallbackInputs: RecentNativeFallbackInput[] = [];
    const recentXtermInputs: RecentXtermInput[] = [];
    const pendingInputs: string[] = [];

    const terminalViewLease = { viewId: viewIdRef.current, clientId, windowId: initialWindowId };
    const isCurrentViewLowPriority = () => {
      return document.hidden || (priorityEnabled && isTerminalViewLowPriority(viewIdRef.current));
    };
    const claimCurrentTerminalView = () => {
      if (!priorityEnabled) {
        return;
      }
      claimActiveTerminalView(terminalViewLease);
    };
    const claimVisibleCurrentTerminalView = () => {
      if (!document.hidden) {
        claimCurrentTerminalView();
      }
    };
    claimActiveTerminalViewRef.current = claimCurrentTerminalView;
    claimVisibleCurrentTerminalView();

    const scheduleInputPriorityClaim = () => {
      if (inputPriorityClaimTimer !== null) {
        return;
      }
      inputPriorityClaimTimer = window.setTimeout(() => {
        inputPriorityClaimTimer = null;
        if (!isActive()) {
          return;
        }
        const now = Date.now();
        if (now - lastInputPriorityClaimedAt < INPUT_VIEW_PRIORITY_CLAIM_INTERVAL_MS) {
          return;
        }
        lastInputPriorityClaimedAt = now;
        claimCurrentTerminalView();
      }, INPUT_VIEW_PRIORITY_CLAIM_INTERVAL_MS);
    };

    const focusCurrentTerminal = () => {
      const stage = stageRef.current;
      const scrollLeft = stage?.scrollLeft ?? 0;
      const scrollTop = stage?.scrollTop ?? 0;
      claimVisibleCurrentTerminalView();
      terminal.focus();
      if (stage) {
        stage.scrollLeft = scrollLeft;
        stage.scrollTop = scrollTop;
      }
    };

    const restoreTerminalFocusAfterEscape = () => {
      if (escapeFocusFrame !== null) {
        window.cancelAnimationFrame(escapeFocusFrame);
      }

      escapeFocusFrame = window.requestAnimationFrame(() => {
        escapeFocusFrame = null;
        if (!isActive()) {
          return;
        }

        focusCurrentTerminal();
      });
    };

    const clearFitUntilFilled = () => {
      if (fitUntilFilledTimer !== null) {
        window.clearTimeout(fitUntilFilledTimer);
        fitUntilFilledTimer = null;
      }
    };

    const outputBuffer = createTerminalOutputBuffer({
      write: (data, onWrite) => {
        const writeHook = window.__WEB_TERMINAL_TEST_ON_TERMINAL_WRITE__;
        if (writeHook !== undefined) {
          terminal.write(data, () => {
            writeHook(data, performance.now());
            onWrite?.();
          });
        } else {
          terminal.write(data, onWrite);
        }
        if (outputRefitTimer !== null) {
          return;
        }
        outputRefitTimer = window.setTimeout(() => {
          outputRefitTimer = null;
          if (!isActive()) {
            return;
          }
          fitAndNotifyResizeRef.current?.();
        }, OUTPUT_REFIT_DEBOUNCE_MS);
      },
      maxFlushCharacters: TERMINAL_OUTPUT_FLUSH_CHARACTERS,
      isLowPriority: isCurrentViewLowPriority,
      lowPriorityFlushDelayMs: BACKGROUND_OUTPUT_FLUSH_DELAY_MS,
      lowPriorityMaxFlushCharacters: BACKGROUND_OUTPUT_FLUSH_CHARACTERS,
    });
    terminalRef.current = terminal;
    setPendingClipboardText(null);
    updateConnectionStatus("connecting");

    const osc52Disposable = terminal.parser.registerOscHandler(52, async (data) => {
      if (!isActive()) {
        return true;
      }

      const text = decodeOsc52ClipboardPayload(data);
      if (text === null) {
        return true;
      }

      try {
        await writeClipboardText(text);
        if (isActive()) {
          setPendingClipboardText(null);
        }
      } catch {
        if (isActive()) {
          setPendingClipboardText(text);
        }
      }
      return true;
    });

    const sendSocketJson = (payload: unknown) => {
      const worker = socketWorkerRef.current;
      if (worker === null || !socketOpenRef.current) {
        return;
      }
      worker.postMessage({ type: "json", data: JSON.stringify(payload) });
    };

    const inputEncoder = new TextEncoder();
    const sendInputNow = (data: string) => {
      const worker = socketWorkerRef.current;
      if (worker === null || !socketOpenRef.current || connectionStatusRef.current !== "connected") {
        return false;
      }

      const encoded = inputEncoder.encode(data);
      worker.postMessage({ type: "input", data: encoded }, [encoded.buffer]);
      return true;
    };

    const flushPendingInputs = () => {
      while (pendingInputs.length > 0) {
        const nextInput = pendingInputs[0];
        if (!sendInputNow(nextInput)) {
          return;
        }
        pendingInputs.shift();
      }
    };

    const sendOrQueueInput = (data: string) => {
      if (sendInputNow(data)) {
        return;
      }
      pendingInputs.push(data);
      while (pendingInputs.length > PENDING_INPUT_QUEUE_MAX_SIZE) {
        pendingInputs.shift();
      }
      claimCurrentTerminalView();
      reconcileViewPriority();
    };
    sendTerminalInputRef.current = sendOrQueueInput;

    terminal.attachCustomKeyEventHandler((event) => {
      const clipboardAction = terminalClipboardShortcutAction(event);
      if (clipboardAction === "copy") {
        if (terminal.hasSelection()) {
          event.preventDefault();
          event.stopPropagation();
          void copyTerminalSelection(terminal).then((copied) => {
            if (copied && isActive()) {
              setPendingClipboardText(null);
              focusCurrentTerminal();
            }
          }).catch(() => {});
          return false;
        }
        return true;
      }

      if (clipboardAction === "paste") {
        return false;
      }

      if (event.key !== "Escape") {
        return true;
      }

      event.preventDefault();
      event.stopPropagation();
      restoreTerminalFocusAfterEscape();
      return true;
    });

    const sendResize = () => {
      const nextResize = { cols: terminal.cols, rows: terminal.rows };
      if (lastSentResize?.cols === nextResize.cols && lastSentResize.rows === nextResize.rows) {
        return;
      }

      sendSocketJson({ type: "resize", ...nextResize });
      lastSentResize = nextResize;
    };

    const fitAndNotifyResize = () => {
      if (terminal.element === undefined) {
        return;
      }

      const container = resolveFitContainer();
      if (container === null) {
        return;
      }

      const rect = container.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) {
        return;
      }

      if (!fitTerminalToContainer(terminal, container)) {
        return;
      }

      sendResize();
    };
    fitAndNotifyResizeRef.current = fitAndNotifyResize;

    const scheduleFitUntilFilled = () => {
      clearFitUntilFilled();
      fitUntilFilledStartedAt = Date.now();
      const tick = () => {
        if (!isActive()) {
          clearFitUntilFilled();
          return;
        }

        const container = resolveFitContainer();
        if (container !== null) {
          fitAndNotifyResize();
          if (isTerminalViewportFilled(terminal, container)) {
            clearFitUntilFilled();
            return;
          }
        }

        if (Date.now() - fitUntilFilledStartedAt >= FIT_UNTIL_FILLED_MAX_MS) {
          clearFitUntilFilled();
          return;
        }

        fitUntilFilledTimer = window.setTimeout(tick, FIT_UNTIL_FILLED_INTERVAL_MS);
      };
      tick();
    };

    void document.fonts.ready.then(() => {
      if (!isActive()) {
        return;
      }
      fitAndNotifyResize();
      scheduleFitUntilFilled();
    });

    const writeParsedDisposable = terminal.onWriteParsed(() => {
      if (!isActive()) {
        return;
      }
      if (writeParsedRefitTimer !== null) {
        return;
      }
      writeParsedRefitTimer = window.setTimeout(() => {
        writeParsedRefitTimer = null;
        if (!isActive()) {
          return;
        }
        const container = resolveFitContainer();
        if (container === null || !terminalViewportNeedsRefit(terminal, container)) {
          return;
        }
        fitAndNotifyResize();
      }, WRITE_PARSED_REFIT_DEBOUNCE_MS);
    });

    const clearReconnectTimer = () => {
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };

    const clearLowPriorityCloseTimer = () => {
      if (lowPriorityCloseTimer !== null) {
        window.clearTimeout(lowPriorityCloseTimer);
        lowPriorityCloseTimer = null;
      }
    };

    const closeSocketWorker = (worker: Worker) => {
      if (socketWorkerRef.current === worker) {
        socketWorkerRef.current = null;
        socketOpenRef.current = false;
      }
      worker.postMessage({ type: "close" });
      worker.terminate();
    };

    const scheduleReconnect = (retryAfterMs?: number) => {
      if (!isActive() || reconnectTimer !== null || isCurrentViewLowPriority()) {
        return;
      }

      const fallbackDelay = RECONNECT_DELAYS_MS[Math.min(reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)];
      reconnectAttempt += 1;
      if (connectionStatusRef.current !== "unavailable") {
        updateConnectionStatus("reconnecting");
      }
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connectSocketWorker();
      }, retryAfterMs ?? fallbackDelay);
    };

    const connectSocketWorker = () => {
      if (!isActive()) {
        return;
      }

      if (isCurrentViewLowPriority()) {
        return;
      }

      if (socketWorkerRef.current !== null) {
        return;
      }

      if (initialWindowId === null) {
        return;
      }

      const worker = new Worker(new URL("../terminalSocketWorker.ts", import.meta.url), { type: "module" });
      socketWorkerRef.current = worker;
      socketOpenRef.current = false;
      updateConnectionStatus(reconnectAttempt === 0 ? "connecting" : "reconnecting");

      worker.onmessage = (event: MessageEvent<{
        type?: unknown;
        data?: unknown;
        closedByCommand?: unknown;
      }>) => {
        if (!isActive() || socketWorkerRef.current !== worker) {
          return;
        }

        if (event.data.type === "open") {
          socketOpenRef.current = true;
          lastSentResize = null;
          scheduleFitAndNotifyResize();
          scheduleFitUntilFilled();
          const pendingWindowId = activeWindowIdRef.current;
          if (selectionEnabled && pendingWindowId !== null && pendingWindowId !== initialWindowId) {
            sendSocketJson({ type: "select_window", window_id: pendingWindowId });
          }
          return;
        }
        const handleControlMessage = (data: string) => {
          const statusMessage = parseTerminalSocketControlMessage(data);
          if (
            statusMessage?.type === "terminal_selection"
            && typeof statusMessage.window_id === "string"
            && statusMessage.view_id === viewIdRef.current
          ) {
            activeWindowIdRef.current = statusMessage.window_id;
            onTerminalSelectionRef.current?.(statusMessage.window_id);
            worker.postMessage({ type: "output-ack" });
            return true;
          }
          if (statusMessage?.status === "connected") {
            reconnectAttempt = 0;
            updateConnectionStatus("connected");
            scheduleFitUntilFilled();
            flushPendingInputs();
          } else if (statusMessage?.status === "unavailable") {
            updateConnectionStatus("unavailable");
            const retryAfterMs = typeof statusMessage.retry_after_ms === "number"
              ? statusMessage.retry_after_ms
              : undefined;
            closeSocketWorker(worker);
            scheduleReconnect(retryAfterMs);
          } else if (statusMessage?.status === "error") {
            updateConnectionStatus("error");
          } else if (statusMessage?.status === "reconnecting") {
            updateConnectionStatus("reconnecting");
            const retryAfterMs = typeof statusMessage.retry_after_ms === "number"
              ? statusMessage.retry_after_ms
              : undefined;
            closeSocketWorker(worker);
            scheduleReconnect(retryAfterMs);
          }
          worker.postMessage({ type: "output-ack" });
          return true;
        };

        if (event.data.type === "control" && typeof event.data.data === "string") {
          handleControlMessage(event.data.data);
          return;
        }
        if (
          (event.data.type === "output" || event.data.type === "interactive-output")
          && event.data.data instanceof Uint8Array
        ) {
          if (connectionStatusRef.current !== "connected") {
            reconnectAttempt = 0;
            updateConnectionStatus("connected");
            scheduleFitUntilFilled();
          }
          flushPendingInputs();
          const serverOutputBytes = event.data.data.byteLength;
          const acknowledgeServerOutput = () => {
            worker.postMessage({ type: "server-output-ack", bytes: serverOutputBytes });
          };
          if (event.data.type === "interactive-output") {
            window.__WEB_TERMINAL_TEST_ON_INTERACTIVE_OUTPUT__?.(event.data.data, performance.now());
            let outputAcknowledged = false;
            const acknowledgeOutput = () => {
              if (outputAcknowledged) {
                return;
              }
              outputAcknowledged = true;
              worker.postMessage({ type: "output-ack" });
            };
            const wroteImmediately = outputBuffer.enqueueInteractive(event.data.data, {
              onWrite: () => {
                acknowledgeServerOutput();
                acknowledgeOutput();
              },
            });
            if (!wroteImmediately) {
              acknowledgeOutput();
            }
          } else {
            outputBuffer.enqueue(event.data.data, { onWrite: acknowledgeServerOutput });
            worker.postMessage({ type: "output-ack" });
          }
          return;
        }
        if (
          (event.data.type === "output" || event.data.type === "interactive-output")
          && typeof event.data.data === "string"
        ) {
          const data = event.data.data;
          if (parseTerminalSocketControlMessage(data) !== null) {
            handleControlMessage(data);
            return;
          }

          if (connectionStatusRef.current !== "connected") {
            reconnectAttempt = 0;
            updateConnectionStatus("connected");
            scheduleFitUntilFilled();
          }
          flushPendingInputs();
          const serverOutputBytes = terminalOutputByteLength(data);
          const acknowledgeServerOutput = () => {
            worker.postMessage({ type: "server-output-ack", bytes: serverOutputBytes });
          };
          if (event.data.type === "interactive-output") {
            window.__WEB_TERMINAL_TEST_ON_INTERACTIVE_OUTPUT__?.(data, performance.now());
            let outputAcknowledged = false;
            const acknowledgeOutput = () => {
              if (outputAcknowledged) {
                return;
              }
              outputAcknowledged = true;
              worker.postMessage({ type: "output-ack" });
            };
            const wroteImmediately = outputBuffer.enqueueInteractive(data, {
              onWrite: () => {
                acknowledgeServerOutput();
                acknowledgeOutput();
              },
            });
            if (!wroteImmediately) {
              acknowledgeOutput();
            }
          } else {
            outputBuffer.enqueue(data, { onWrite: acknowledgeServerOutput });
            worker.postMessage({ type: "output-ack" });
          }
          return;
        }
        if (event.data.type === "error") {
          closeSocketWorker(worker);
          scheduleReconnect();
          return;
        }
        if (event.data.type === "close") {
          if (socketWorkerRef.current !== worker) {
            return;
          }
          closeSocketWorker(worker);
          if (event.data.closedByCommand !== true) {
            scheduleReconnect();
          }
        }
      };

      worker.postMessage({ type: "connect", url: webSocketUrl(clientId, initialWindowId, viewIdRef.current) });
    };

    const reconcileViewPriority = () => {
      if (!isActive()) {
        return;
      }

      if (isCurrentViewLowPriority()) {
        clearReconnectTimer();
        outputBuffer.clear();
        if (lowPriorityCloseTimer === null && socketWorkerRef.current !== null) {
          lowPriorityCloseTimer = window.setTimeout(() => {
            lowPriorityCloseTimer = null;
            if (!isActive() || !isCurrentViewLowPriority()) {
              return;
            }
            const worker = socketWorkerRef.current;
            if (worker !== null) {
              closeSocketWorker(worker);
            }
          }, LOW_PRIORITY_SOCKET_CLOSE_DELAY_MS);
        }
        return;
      }

      clearLowPriorityCloseTimer();
      scheduleFitAndNotifyResize();
      scheduleFitUntilFilled();
      connectSocketWorker();
      flushPendingInputs();
    };

    const handleTerminalVisibilityChange = () => {
      if (autoFocusRef.current && !document.hidden) {
        focusCurrentTerminal();
      }
      reconcileViewPriority();
    };
    const handleTerminalViewPriorityChange = () => {
      reconcileViewPriority();
    };
    document.addEventListener("visibilitychange", handleTerminalVisibilityChange);
    window.addEventListener("storage", handleTerminalViewPriorityChange);
    window.addEventListener(TERMINAL_VIEW_PRIORITY_CHANGED_EVENT, handleTerminalViewPriorityChange);
    const viewPriorityReconcileInterval = window.setInterval(
      reconcileViewPriority,
      VIEW_PRIORITY_RECONCILE_INTERVAL_MS,
    );

    const pruneRecentNativeFallbackInputs = (now = performance.now()) => {
      while (
        recentNativeFallbackInputs.length > 0
        && now - recentNativeFallbackInputs[0].sentAt > NATIVE_TEXT_INPUT_DEDUPE_MS
      ) {
        recentNativeFallbackInputs.shift();
      }
    };

    const pruneRecentXtermInputs = (now = performance.now()) => {
      while (
        recentXtermInputs.length > 0
        && now - recentXtermInputs[0].seenAt > NATIVE_TEXT_INPUT_DEDUPE_MS
      ) {
        recentXtermInputs.shift();
      }
    };

    const consumeMatchingNativeFallbackInput = (data: string): boolean => {
      const now = performance.now();
      pruneRecentNativeFallbackInputs(now);
      const activeInputEventSerial = activeNativeTextInputEventSerial;
      const index = recentNativeFallbackInputs.findIndex((entry) => (
        entry.data === data
        && (
          activeInputEventSerial === entry.inputEventSerial
          || (activeInputEventSerial === null && nativeTextInputEventSerial === entry.inputEventSerial)
        )
      ));
      if (index < 0) {
        return false;
      }
      recentNativeFallbackInputs.splice(index, 1);
      return true;
    };

    const rememberNativeFallbackInput = (data: string, inputEventSerial: number) => {
      const now = performance.now();
      pruneRecentNativeFallbackInputs(now);
      recentNativeFallbackInputs.push({ data, inputEventSerial, sentAt: now });
    };

    const rememberXtermInput = (data: string) => {
      const now = performance.now();
      pruneRecentXtermInputs(now);
      recentXtermInputs.push({ data, seenAt: now });
    };

    const hasRecentXtermInput = (data: string): boolean => {
      const now = performance.now();
      pruneRecentXtermInputs(now);
      return recentXtermInputs.some((entry) => entry.data === data);
    };

    const markNativeTextInputEventSerial = () => {
      nativeTextInputEventSerial += 1;
      activeNativeTextInputEventSerial = nativeTextInputEventSerial;
      if (nativeTextInputEventClearTimer !== null) {
        window.clearTimeout(nativeTextInputEventClearTimer);
      }
      const markedInputEventSerial = nativeTextInputEventSerial;
      nativeTextInputEventClearTimer = window.setTimeout(() => {
        nativeTextInputEventClearTimer = null;
        if (activeNativeTextInputEventSerial === markedInputEventSerial) {
          activeNativeTextInputEventSerial = null;
        }
      }, 0);
      return markedInputEventSerial;
    };

    const sendNativeTextInputFallback = (
      data: string,
      inputEventSerial: number,
      xtermSerialBeforeFallback: number,
      { allowAfterComposition = false }: { allowAfterComposition?: boolean } = {},
    ) => {
      if (
        data.length === 0
        || !isActive()
        || connectionStatusRef.current !== "connected"
        || (!allowAfterComposition && nativeInputFallbackCompositionActive)
        || xtermInputSerial !== xtermSerialBeforeFallback
        || hasRecentXtermInput(data)
      ) {
        return;
      }

      rememberNativeFallbackInput(data, inputEventSerial);
      sendOrQueueInput(data);
      scheduleInputPriorityClaim();
    };

    const disposable = terminal.onData((data) => {
      xtermInputSerial += 1;
      if (consumeMatchingNativeFallbackInput(data)) {
        return;
      }
      rememberXtermInput(data);
      window.__WEB_TERMINAL_TEST_ON_TERMINAL_DATA__?.(data, performance.now());
      sendOrQueueInput(data);
      scheduleInputPriorityClaim();
    });

    let resizeDebounceTimer: number | null = null;
    const onResizeObserved = () => {
      if (resizeDebounceTimer !== null) {
        window.clearTimeout(resizeDebounceTimer);
      }
      resizeDebounceTimer = window.setTimeout(() => {
        resizeDebounceTimer = null;
        fitAndNotifyResize();
      }, RESIZE_OBSERVER_DEBOUNCE_MS);
    };

    const resizeObserver = new ResizeObserver(onResizeObserved);
    const pane = containerRef.current;
    const host = xtermHostRef.current;
    if (pane !== null) {
      resizeObserver.observe(pane);
    }
    if (host !== null && host !== pane) {
      resizeObserver.observe(host);
    }
    const stage = stageRef.current;
    if (stage !== null && stage !== pane) {
      resizeObserver.observe(stage);
    }
    const workspace = stage?.closest(".workspace");
    if (workspace instanceof HTMLElement) {
      resizeObserver.observe(workspace);
    }

    const attachRenderResizeObserver = () => {
      const canvas = readTerminalCanvas(terminal);
      const rowsElement = terminal.element?.querySelector(".xterm-rows");
      if (canvas === null && !(rowsElement instanceof HTMLElement)) {
        return false;
      }

      if (canvasResizeObserver === null) {
        canvasResizeObserver = new ResizeObserver(() => {
          if (!isActive()) {
            return;
          }
          fitAndNotifyResize();
        });
      }

      if (canvas !== null) {
        canvasResizeObserver.observe(canvas);
      }
      if (rowsElement instanceof HTMLElement) {
        canvasResizeObserver.observe(rowsElement);
      }
      return true;
    };

    const canvasObserver = new MutationObserver(() => {
      if (!isActive()) {
        return;
      }
      if (attachRenderResizeObserver()) {
        fitAndNotifyResize();
      }
    });
    if (pane !== null) {
      canvasObserver.observe(pane, { childList: true, subtree: true });
    }

    let nativePasteElement: HTMLElement | undefined;
    let nativePasteTextarea: HTMLTextAreaElement | undefined;
    let nativeInputTextarea: HTMLTextAreaElement | undefined;
    const handleNativePaste = (event: ClipboardEvent) => {
      if (connectionStatusRef.current !== "connected") {
        return;
      }

      const pasted = pasteClipboardEventToTerminal(
        event,
        sendOrQueueInput,
        terminal.modes.bracketedPasteMode,
      );
      if (pasted) {
        focusCurrentTerminal();
      }
    };

    const handleNativeInputCompositionStart = () => {
      nativeInputFallbackCompositionActive = true;
      nativeInputCompositionStartValue = nativeInputTextarea?.value ?? "";
      nativeInputCompositionLatestData = "";
      if (nativeInputFallbackTimer !== null) {
        window.clearTimeout(nativeInputFallbackTimer);
        nativeInputFallbackTimer = null;
      }
    };
    const handleNativeInputCompositionUpdate = (event: CompositionEvent) => {
      nativeInputCompositionLatestData = event.data;
    };
    const handleNativeInputCompositionEnd = (event: CompositionEvent) => {
      nativeInputFallbackCompositionActive = false;
      nativeInputCompositionLatestData = event.data || nativeInputCompositionLatestData;
      const compositionStartValue = nativeInputCompositionStartValue;
      const compositionEventData = event.data;
      const fallbackInputEventSerial = markNativeTextInputEventSerial();
      const xtermSerialBeforeFallback = xtermInputSerial;
      if (nativeInputFallbackTimer !== null) {
        window.clearTimeout(nativeInputFallbackTimer);
      }
      nativeInputFallbackTimer = window.setTimeout(() => {
        nativeInputFallbackTimer = null;
        const textareaValue = nativeInputTextarea?.value ?? "";
        const insertedText = textareaValue.startsWith(compositionStartValue)
          ? textareaValue.slice(compositionStartValue.length)
          : "";
        const data = insertedText || compositionEventData || nativeInputCompositionLatestData;
        nativeInputCompositionStartValue = "";
        nativeInputCompositionLatestData = "";
        sendNativeTextInputFallback(data, fallbackInputEventSerial, xtermSerialBeforeFallback, {
          allowAfterComposition: true,
        });
      }, NATIVE_TEXT_INPUT_FALLBACK_DELAY_MS);
    };
    const markNativeTextInputEvent = (event: InputEvent) => {
      if (
        (event.inputType === "insertText" || event.inputType === "insertCompositionText")
        && event.data !== null
        && event.data.length > 1
      ) {
        markNativeTextInputEventSerial();
      }
    };
    const handleNativeTextInputFallback = (event: InputEvent) => {
      if (event.inputType === "insertCompositionText" && event.data !== null) {
        nativeInputCompositionLatestData = event.data;
      }
      if (
        connectionStatusRef.current !== "connected"
        || nativeInputFallbackCompositionActive
        || event.isComposing
        || event.inputType !== "insertText"
        || event.data === null
        || event.data.length <= 1
        || event.defaultPrevented
      ) {
        return;
      }

      const data = event.data;
      const xtermSerialBeforeFallback = xtermInputSerial;
      const inputEventSerial = nativeTextInputEventSerial;
      if (nativeInputFallbackTimer !== null) {
        window.clearTimeout(nativeInputFallbackTimer);
      }
      nativeInputFallbackTimer = window.setTimeout(() => {
        nativeInputFallbackTimer = null;
        sendNativeTextInputFallback(data, inputEventSerial, xtermSerialBeforeFallback);
      }, NATIVE_TEXT_INPUT_FALLBACK_DELAY_MS);
    };

    const attachNativePasteHandlers = () => {
      const element = terminal.element;
      const textarea = terminal.textarea;
      if (element !== undefined && nativePasteElement !== element) {
        nativePasteElement?.removeEventListener("paste", handleNativePaste, true);
        nativePasteElement?.removeEventListener("input", markNativeTextInputEvent as EventListener, true);
        element.addEventListener("paste", handleNativePaste, true);
        element.addEventListener("input", markNativeTextInputEvent as EventListener, true);
        nativePasteElement = element;
      }
      if (textarea !== undefined && nativePasteTextarea !== textarea) {
        nativePasteTextarea?.removeEventListener("paste", handleNativePaste, true);
        textarea.addEventListener("paste", handleNativePaste, true);
        nativePasteTextarea = textarea;
      }
      if (textarea !== undefined && nativeInputTextarea !== textarea) {
        nativeInputTextarea?.removeEventListener("compositionstart", handleNativeInputCompositionStart);
        nativeInputTextarea?.removeEventListener("compositionupdate", handleNativeInputCompositionUpdate);
        nativeInputTextarea?.removeEventListener("compositionend", handleNativeInputCompositionEnd);
        nativeInputTextarea?.removeEventListener("input", handleNativeTextInputFallback as EventListener, true);
        textarea.addEventListener("compositionstart", handleNativeInputCompositionStart);
        textarea.addEventListener("compositionupdate", handleNativeInputCompositionUpdate);
        textarea.addEventListener("compositionend", handleNativeInputCompositionEnd);
        textarea.addEventListener("input", handleNativeTextInputFallback as EventListener, true);
        nativeInputTextarea = textarea;
      }
    };

    const undersizedCheckInterval = window.setInterval(() => {
      if (!isActive() || document.hidden) {
        return;
      }

      const container = resolveFitContainer();
      if (container === null || !terminalViewportNeedsRefit(terminal, container)) {
        return;
      }

      fitAndNotifyResize();
    }, UNDERSIZED_REFIT_INTERVAL_MS);

    const openTerminalWhenReady = () => {
      if (!isActive()) {
        return;
      }

      const xtermHost = xtermHostRef.current;
      if (xtermHost === null) {
        openFrame = window.requestAnimationFrame(openTerminalWhenReady);
        return;
      }

      if (xtermHost.clientWidth <= 0 || xtermHost.clientHeight <= 0) {
        openFrame = window.requestAnimationFrame(openTerminalWhenReady);
        return;
      }

      terminal.open(xtermHost);
      attachNativePasteHandlers();
      attachRenderResizeObserver();
      scheduleFitAndNotifyResize();
      scheduleFitUntilFilled();
      if (autoFocusRef.current) {
        focusCurrentTerminal();
      }
      connectSocketWorker();
    };

    openTerminalWhenReady();

    return () => {
      closedByCleanup = true;
      disposed = true;
      if (openFrame !== null) {
        window.cancelAnimationFrame(openFrame);
      }
      if (escapeFocusFrame !== null) {
        window.cancelAnimationFrame(escapeFocusFrame);
      }
      canvasResizeObserver?.disconnect();
      clearReconnectTimer();
      clearLowPriorityCloseTimer();
      clearFitUntilFilled();
      if (inputPriorityClaimTimer !== null) {
        window.clearTimeout(inputPriorityClaimTimer);
      }
      if (outputRefitTimer !== null) {
        window.clearTimeout(outputRefitTimer);
      }
      if (writeParsedRefitTimer !== null) {
        window.clearTimeout(writeParsedRefitTimer);
      }
      if (resizeDebounceTimer !== null) {
        window.clearTimeout(resizeDebounceTimer);
      }
      if (nativeInputFallbackTimer !== null) {
        window.clearTimeout(nativeInputFallbackTimer);
      }
      if (nativeTextInputEventClearTimer !== null) {
        window.clearTimeout(nativeTextInputEventClearTimer);
      }
      window.clearInterval(viewPriorityReconcileInterval);
      window.clearInterval(undersizedCheckInterval);
      resizeObserver.disconnect();
      canvasObserver.disconnect();
      nativePasteElement?.removeEventListener("paste", handleNativePaste, true);
      nativePasteElement?.removeEventListener("input", markNativeTextInputEvent as EventListener, true);
      nativePasteTextarea?.removeEventListener("paste", handleNativePaste, true);
      nativeInputTextarea?.removeEventListener("compositionstart", handleNativeInputCompositionStart);
      nativeInputTextarea?.removeEventListener("compositionupdate", handleNativeInputCompositionUpdate);
      nativeInputTextarea?.removeEventListener("compositionend", handleNativeInputCompositionEnd);
      nativeInputTextarea?.removeEventListener("input", handleNativeTextInputFallback as EventListener, true);
      document.removeEventListener("visibilitychange", handleTerminalVisibilityChange);
      window.removeEventListener("storage", handleTerminalViewPriorityChange);
      window.removeEventListener(TERMINAL_VIEW_PRIORITY_CHANGED_EVENT, handleTerminalViewPriorityChange);
      disposable.dispose();
      writeParsedDisposable.dispose();
      osc52Disposable.dispose();
      outputBuffer.dispose();
      const worker = socketWorkerRef.current;
      worker?.postMessage({ type: "close" });
      worker?.terminate();
      terminal.dispose();
      clearScheduledFits();
      socketWorkerRef.current = null;
      socketOpenRef.current = false;
      if (terminalRef.current === terminal) {
        terminalRef.current = null;
      }
      if (fitAndNotifyResizeRef.current === fitAndNotifyResize) {
        fitAndNotifyResizeRef.current = null;
      }
      if (claimActiveTerminalViewRef.current === claimCurrentTerminalView) {
        claimActiveTerminalViewRef.current = null;
      }
      if (sendTerminalInputRef.current === sendOrQueueInput) {
        sendTerminalInputRef.current = null;
      }
    };
  }, [
    claimTerminalViewPriority,
    clearScheduledFits,
    clientId,
    priorityEnabled,
    scheduleFitAndNotifyResize,
    selectionEnabled,
    updateConnectionStatus,
    hasSelectedWindow,
    webSocketUrl,
  ]);

  useEffect(() => {
    if (clientId === null || windowId === null) {
      return;
    }

    if (activeWindowIdRef.current === windowId) {
      return;
    }

    activeWindowIdRef.current = windowId;
    sendSelectWindow(windowId);
  }, [clientId, sendSelectWindow, windowId]);

  useLayoutEffect(() => {
    const stage = stageRef.current;
    if (stage !== null) {
      stage.scrollLeft = 0;
      stage.scrollTop = 0;
    }

    scheduleFitAndNotifyResize();
  }, [layoutVersion, scheduleFitAndNotifyResize, viewportMode, virtualKeysVisible]);

  useEffect(() => {
    const handleResizeCue = () => scheduleFitAndNotifyResize();
    const handleVisibilityChange = () => {
      if (!document.hidden) {
        scheduleFitAndNotifyResize();
      }
    };

    window.addEventListener("resize", handleResizeCue);
    window.addEventListener("orientationchange", handleResizeCue);
    window.addEventListener("focus", handleResizeCue);
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      window.removeEventListener("resize", handleResizeCue);
      window.removeEventListener("orientationchange", handleResizeCue);
      window.removeEventListener("focus", handleResizeCue);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      clearScheduledFits();
    };
  }, [clearScheduledFits, scheduleFitAndNotifyResize]);

  if (clientId === null || windowId === null) {
    return <div className="empty-terminal" data-onboarding-id="terminal-pane">Create or select a terminal.</div>;
  }

  const connectionOverlay = connectionStatus === "connected" ? null : (
    <div className={`terminal-connection-status ${connectionStatus}`} role="status">
      {terminalStatusLabel(connectionStatus)}
    </div>
  );
  const quickInputOverlay = quickInputOpen ? (
    <TerminalQuickInput
      value={quickInputDraft}
      canSend={canSendQuickInput}
      onValueChange={updateQuickInputDraft}
      onSubmit={submitQuickInput}
      onCancel={closeQuickInput}
      customQuickKeys={customQuickKeys}
      onCustomQuickKeySubmit={onCustomQuickKeySubmit}
      autoFocus
    />
  ) : null;

  return (
    <div
      ref={stageRef}
      data-onboarding-id="terminal-pane"
      className={[
        "terminal-stage",
        `terminal-stage-${viewportMode}`,
        virtualKeysVisible ? "terminal-stage-with-virtual-keys" : "",
        quickInputOpen ? "terminal-stage-quick-input-open" : "",
      ].filter(Boolean).join(" ")}
      onPointerDown={handleTerminalPointerDown}
      onPointerMove={handleTerminalPointerMove}
      onPointerUp={handleTerminalPointerEnd}
      onPointerCancel={handleTerminalPointerEnd}
    >
      {virtualKeysVisible && (
        <div className="terminal-virtual-keys" aria-label="Virtual terminal keys">
          {VIRTUAL_KEYS.map((key) => (
            <button
              type="button"
              key={key.label}
              disabled={connectionStatus !== "connected"}
              onMouseDown={(event) => {
                event.preventDefault();
                event.stopPropagation();
              }}
              onTouchStart={(event) => event.stopPropagation()}
              onClick={(event) => {
                event.stopPropagation();
                sendTerminalInput(key.value, { focusAfterSend: false });
              }}
            >
              {key.label}
            </button>
          ))}
          <button
            type="button"
            disabled={connectionStatus !== "connected"}
            onMouseDown={(event) => {
              event.preventDefault();
              event.stopPropagation();
            }}
            onTouchStart={(event) => event.stopPropagation()}
            onClick={(event) => {
              event.stopPropagation();
              void pasteTerminalClipboardText();
            }}
          >
            Paste
          </button>
          <button
            type="button"
            onMouseDown={(event) => {
              event.preventDefault();
              event.stopPropagation();
            }}
            onTouchStart={(event) => event.stopPropagation()}
            onClick={(event) => {
              event.stopPropagation();
              void copyTerminalClipboardSelection();
            }}
          >
            Copy
          </button>
        </div>
      )}
      <div ref={containerRef} className={`terminal-pane terminal-pane-${viewportMode}`}>
        <div ref={xtermHostRef} className="terminal-xterm-host" />
      </div>
      {quickInputOverlay}
      {connectionOverlay}
      {pendingClipboardText !== null && (
        <button
          type="button"
          className="terminal-clipboard-copy"
          onMouseDown={(event) => event.stopPropagation()}
          onTouchStart={(event) => event.stopPropagation()}
          onClick={async (event) => {
            event.stopPropagation();
            try {
              await writeClipboardText(pendingClipboardText, true);
              setPendingClipboardText(null);
              focusTerminal();
            } catch {
              return;
            }
          }}
        >
          Copy pending clipboard
        </button>
      )}
    </div>
  );
});
