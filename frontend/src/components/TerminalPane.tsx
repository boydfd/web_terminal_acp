import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";

import { terminalWebSocketUrl } from "../api";
import { createTerminalOutputBuffer } from "../terminalOutputBuffer";

type TerminalViewportMode = "desktop" | "phone" | "fixed";
type TerminalConnectionStatus = "connecting" | "connected" | "reconnecting" | "unavailable" | "error";

type TerminalPaneProps = {
  clientId: string | null;
  windowId: string | null;
  viewportMode?: TerminalViewportMode;
  layoutVersion?: number;
  virtualKeysVisible?: boolean;
};

export type TerminalPaneHandle = {
  focus: () => void;
};

type VirtualKey = {
  label: string;
  value: string;
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

type TerminalStatusMessage = {
  type?: unknown;
  status?: unknown;
  retry_after_ms?: unknown;
};

function parseTerminalStatusMessage(data: string): TerminalStatusMessage | null {
  try {
    const message = JSON.parse(data) as TerminalStatusMessage;
    return message.type === "terminal_status" ? message : null;
  } catch {
    return null;
  }
}

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

async function writeClipboardText(text: string, allowDomFallback = false): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }

  if (!allowDomFallback) {
    throw new Error("clipboard API is unavailable");
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    if (!document.execCommand("copy")) {
      throw new Error("copy command failed");
    }
  } finally {
    textarea.remove();
  }
}

export const TerminalPane = forwardRef<TerminalPaneHandle, TerminalPaneProps>(function TerminalPane({
  clientId,
  windowId,
  viewportMode = "desktop",
  layoutVersion = 0,
  virtualKeysVisible = false,
}, ref) {
  const stageRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const fitAndNotifyResizeRef = useRef<(() => void) | null>(null);
  const scheduledFitFramesRef = useRef<number[]>([]);
  const scheduledFitTimeoutsRef = useRef<number[]>([]);
  const connectionStatusRef = useRef<TerminalConnectionStatus>("connecting");
  const [pendingClipboardText, setPendingClipboardText] = useState<string | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<TerminalConnectionStatus>("connecting");

  const updateConnectionStatus = useCallback((status: TerminalConnectionStatus) => {
    connectionStatusRef.current = status;
    setConnectionStatus(status);
  }, []);

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

  const scheduleFitAndNotifyResize = useCallback(() => {
    clearScheduledFits();
    fitAndNotifyResize();

    const firstFrame = window.requestAnimationFrame(() => {
      fitAndNotifyResize();
      const secondFrame = window.requestAnimationFrame(fitAndNotifyResize);
      scheduledFitFramesRef.current.push(secondFrame);
    });
    scheduledFitFramesRef.current.push(firstFrame);

    for (const delay of [80, 250, 600, 1200]) {
      scheduledFitTimeoutsRef.current.push(window.setTimeout(fitAndNotifyResize, delay));
    }
  }, [clearScheduledFits, fitAndNotifyResize]);

  const focusTerminal = useCallback(() => {
    const stage = stageRef.current;
    const scrollLeft = stage?.scrollLeft ?? 0;
    const scrollTop = stage?.scrollTop ?? 0;
    terminalRef.current?.focus();
    if (stage) {
      stage.scrollLeft = scrollLeft;
      stage.scrollTop = scrollTop;
    }
  }, []);

  useImperativeHandle(ref, () => ({
    focus: focusTerminal
  }), [focusTerminal]);

  const sendTerminalInput = (data: string) => {
    const socket = socketRef.current;
    if (socket?.readyState !== WebSocket.OPEN || connectionStatusRef.current !== "connected") {
      return;
    }

    socket.send(new TextEncoder().encode(data));
    focusTerminal();
  };

  useEffect(() => {
    if (clientId === null || windowId === null || containerRef.current === null) {
      return;
    }

    const terminal = new Terminal({
      cursorBlink: true,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    });
    const fitAddon = new FitAddon();
    const textEncoder = new TextEncoder();
    let closedByCleanup = false;
    let disposed = false;
    const isActive = () => !closedByCleanup && !disposed;

    terminal.loadAddon(fitAddon);
    terminal.open(containerRef.current);
    const outputBuffer = createTerminalOutputBuffer({
      write: (data) => terminal.write(data),
    });
    terminalRef.current = terminal;
    setPendingClipboardText(null);
    updateConnectionStatus("connecting");
    let reconnectAttempt = 0;
    let reconnectTimer: number | null = null;
    let lastSentResize: { cols: number; rows: number } | null = null;

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

    const sendResize = () => {
      const socket = socketRef.current;
      if (socket === null) {
        return;
      }

      if (socket.readyState !== WebSocket.OPEN) {
        return;
      }

      const nextResize = { cols: terminal.cols, rows: terminal.rows };
      if (lastSentResize?.cols === nextResize.cols && lastSentResize.rows === nextResize.rows) {
        return;
      }

      socket.send(JSON.stringify({ type: "resize", ...nextResize }));
      lastSentResize = nextResize;
    };

    const fitAndNotifyResize = () => {
      const container = containerRef.current;
      if (container === null) {
        return;
      }

      const rect = container.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) {
        return;
      }

      fitAddon.fit();
      sendResize();
    };
    fitAndNotifyResizeRef.current = fitAndNotifyResize;

    scheduleFitAndNotifyResize();

    const clearReconnectTimer = () => {
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };

    const scheduleReconnect = (retryAfterMs?: number) => {
      if (!isActive() || reconnectTimer !== null) {
        return;
      }

      const fallbackDelay = RECONNECT_DELAYS_MS[Math.min(reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)];
      reconnectAttempt += 1;
      if (connectionStatusRef.current !== "unavailable") {
        updateConnectionStatus("reconnecting");
      }
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connectSocket();
      }, retryAfterMs ?? fallbackDelay);
    };

    const connectSocket = () => {
      if (!isActive()) {
        return;
      }

      const socket = new WebSocket(terminalWebSocketUrl(clientId, windowId));
      socket.binaryType = "arraybuffer";
      socketRef.current = socket;
      updateConnectionStatus(reconnectAttempt === 0 ? "connecting" : "reconnecting");

      socket.onopen = () => {
        if (!isActive() || socketRef.current !== socket) {
          return;
        }

        lastSentResize = null;
        scheduleFitAndNotifyResize();
      };
      socket.onmessage = (event) => {
        if (!isActive() || socketRef.current !== socket) {
          return;
        }

        if (event.data instanceof ArrayBuffer) {
          if (connectionStatusRef.current !== "connected") {
            reconnectAttempt = 0;
            updateConnectionStatus("connected");
          }
          outputBuffer.enqueue(new Uint8Array(event.data));
          return;
        }

        const data = String(event.data);
        const statusMessage = parseTerminalStatusMessage(data);
        if (statusMessage !== null) {
          if (statusMessage.status === "connected") {
            reconnectAttempt = 0;
            updateConnectionStatus("connected");
          } else if (statusMessage.status === "unavailable") {
            updateConnectionStatus("unavailable");
            const retryAfterMs = typeof statusMessage.retry_after_ms === "number"
              ? statusMessage.retry_after_ms
              : undefined;
            socket.close();
            scheduleReconnect(retryAfterMs);
          } else if (statusMessage.status === "error") {
            updateConnectionStatus("error");
          } else if (statusMessage.status === "reconnecting") {
            updateConnectionStatus("reconnecting");
          }
          return;
        }

        if (connectionStatusRef.current !== "connected") {
          reconnectAttempt = 0;
          updateConnectionStatus("connected");
        }
        outputBuffer.enqueue(data);
      };
      socket.onerror = () => {
        if (!isActive() || socketRef.current !== socket) {
          return;
        }
        socket.close();
      };
      socket.onclose = () => {
        if (!isActive() || socketRef.current !== socket) {
          return;
        }

        socketRef.current = null;
        scheduleReconnect();
      };
    };

    connectSocket();

    const disposable = terminal.onData((data) => {
      const socket = socketRef.current;
      if (socket === null || connectionStatusRef.current !== "connected") {
        return;
      }

      if (socket.readyState === WebSocket.OPEN) {
        socket.send(textEncoder.encode(data));
      }
    });

    const resizeObserver = new ResizeObserver(() => scheduleFitAndNotifyResize());
    resizeObserver.observe(containerRef.current);

    return () => {
      closedByCleanup = true;
      disposed = true;
      clearReconnectTimer();
      resizeObserver.disconnect();
      disposable.dispose();
      osc52Disposable.dispose();
      outputBuffer.dispose();
      const socket = socketRef.current;
      if (socket !== null) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      }
      terminal.dispose();
      clearScheduledFits();
      socketRef.current = null;
      if (terminalRef.current === terminal) {
        terminalRef.current = null;
      }
      if (fitAndNotifyResizeRef.current === fitAndNotifyResize) {
        fitAndNotifyResizeRef.current = null;
      }
    };
  }, [
    clearScheduledFits,
    clientId,
    scheduleFitAndNotifyResize,
    updateConnectionStatus,
    windowId,
  ]);

  useEffect(() => {
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
    return <div className="empty-terminal">Create or select a terminal.</div>;
  }

  const connectionOverlay = connectionStatus === "connected" ? null : (
    <div className={`terminal-connection-status ${connectionStatus}`} role="status">
      {terminalStatusLabel(connectionStatus)}
    </div>
  );

  return (
    <div
      ref={stageRef}
      className={[
        "terminal-stage",
        `terminal-stage-${viewportMode}`,
        virtualKeysVisible ? "terminal-stage-with-virtual-keys" : "",
      ].filter(Boolean).join(" ")}
      onMouseDown={focusTerminal}
      onTouchStart={focusTerminal}
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
                sendTerminalInput(key.value);
              }}
            >
              {key.label}
            </button>
          ))}
        </div>
      )}
      <div ref={containerRef} className={`terminal-pane terminal-pane-${viewportMode}`} />
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
