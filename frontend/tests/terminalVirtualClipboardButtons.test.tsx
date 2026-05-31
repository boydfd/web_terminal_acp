import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TerminalPane } from "../src/components/TerminalPane";

let latestTerminal: {
  element: HTMLElement | undefined;
  textarea: HTMLTextAreaElement | undefined;
  options: { theme?: unknown };
  customKeyEventHandler?: (event: KeyboardEvent) => boolean;
  emitData: (data: string) => void;
} | null = null;

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    public cols = 80;
    public rows = 24;
    public element: HTMLElement | undefined;
    public textarea: HTMLTextAreaElement | undefined;
    public options: { theme?: unknown };
    public modes = { bracketedPasteMode: false };
    public customKeyEventHandler?: (event: KeyboardEvent) => boolean;
    public dataHandler?: (data: string) => void;
    public parser = {
      registerOscHandler: vi.fn(() => ({ dispose: vi.fn() }))
    };

    constructor(options: { theme?: unknown } = {}) {
      this.options = options;
    }

    open(host: HTMLElement) {
      this.element = document.createElement("div");
      this.element.className = "xterm";
      this.textarea = document.createElement("textarea");
      this.textarea.className = "xterm-helper-textarea";
      this.element.appendChild(this.textarea);
      host.appendChild(this.element);
      latestTerminal = this;
    }

    focus() {}
    dispose() {}
    write(_data: string | Uint8Array, callback?: () => void) {
      callback?.();
    }
    onWriteParsed() {
      return { dispose: vi.fn() };
    }
    onData(handler: (data: string) => void) {
      this.dataHandler = handler;
      return { dispose: vi.fn() };
    }
    attachCustomKeyEventHandler(handler: (event: KeyboardEvent) => boolean) {
      this.customKeyEventHandler = handler;
    }
    emitData(data: string) {
      this.dataHandler?.(data);
    }
  }
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

class ResizeObserverMock {
  observe() {}
  disconnect() {}
}

class WorkerMock {
  onmessage: ((event: MessageEvent) => void) | null = null;
  messages: Array<{ type?: unknown }> = [];

  postMessage(message: { type?: unknown }) {
    this.messages.push(message);
    if (message.type === "connect") {
      this.onmessage?.({ data: { type: "open" } } as MessageEvent);
      this.onmessage?.({ data: { type: "control", data: JSON.stringify({ type: "terminal_status", status: "connected" }) } } as MessageEvent);
    }
  }

  terminate() {}
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;
let workerInstances: WorkerMock[] = [];

function installTerminalPaneDomMocks() {
  latestTerminal = null;
  workerInstances = [];
  globalThis.ResizeObserver = ResizeObserverMock as never;
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, value: 400 });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, value: 300 });
  Object.defineProperty(document, "fonts", {
    configurable: true,
    value: { ready: Promise.resolve() },
  });
  globalThis.Worker = class extends WorkerMock {
    constructor() {
      super();
      workerInstances.push(this);
    }
  } as never;
  Element.prototype.getBoundingClientRect = vi.fn(() => ({
    width: 400,
    height: 300,
    top: 0,
    right: 400,
    bottom: 300,
    left: 0,
    x: 0,
    y: 0,
    toJSON: () => ({})
  })) as never;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
}

function textInputEvent(data: string): InputEvent {
  const event = new Event("input", { bubbles: true, cancelable: true }) as InputEvent;
  Object.defineProperties(event, {
    data: { configurable: true, value: data },
    inputType: { configurable: true, value: "insertText" },
    isComposing: { configurable: true, value: false },
  });
  return event;
}

async function waitForNativeInputFallback(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  });
}

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  root = null;
  container = null;
  vi.restoreAllMocks();
});

describe("TerminalPane virtual clipboard controls", () => {
  it("renders copy and paste buttons in the mobile virtual key strip", () => {
    installTerminalPaneDomMocks();

    act(() => {
      root?.render(
        <TerminalPane
          clientId="client-1"
          windowId="window-1"
          viewportMode="phone"
          virtualKeysVisible
        />
      );
    });

    const virtualKeys = container.querySelector(".terminal-virtual-keys");
    expect(virtualKeys?.textContent).toContain("Paste");
    expect(virtualKeys?.textContent).toContain("Copy");
  });

  it("updates theme without rebuilding the terminal websocket", () => {
    installTerminalPaneDomMocks();
    const initialTheme = { background: "#000000" };
    const nextTheme = { background: "#ffffff" };

    act(() => {
      root?.render(
        <TerminalPane
          clientId="client-1"
          windowId="window-1"
          theme={initialTheme}
        />
      );
    });

    expect(workerInstances).toHaveLength(1);
    expect(latestTerminal?.options.theme).toBe(initialTheme);

    act(() => {
      root?.render(
        <TerminalPane
          clientId="client-1"
          windowId="window-1"
          theme={nextTheme}
        />
      );
    });

    expect(workerInstances).toHaveLength(1);
    expect(workerInstances[0].messages.filter((message) => message.type === "connect")).toHaveLength(1);
    expect(latestTerminal?.options.theme).toBe(nextTheme);
  });

  it("keeps native paste events available for Ctrl/Cmd+V and prevents xterm from sending Ctrl-V", () => {
    installTerminalPaneDomMocks();

    act(() => {
      root?.render(
        <TerminalPane
          clientId="client-1"
          windowId="window-1"
        />
      );
    });

    const keyEvent = new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      ctrlKey: true,
      key: "v",
    });

    expect(latestTerminal?.customKeyEventHandler?.(keyEvent)).toBe(false);
    expect(keyEvent.defaultPrevented).toBe(false);
  });

  it("sends browser native paste event text to the terminal input stream once", () => {
    installTerminalPaneDomMocks();

    act(() => {
      root?.render(
        <TerminalPane
          clientId="client-1"
          windowId="window-1"
        />
      );
    });

    const terminalElement = latestTerminal?.element;
    if (!(terminalElement instanceof HTMLElement)) {
      throw new Error("Terminal element was not rendered");
    }

    const pasteEvent = new Event("paste", { bubbles: true, cancelable: true }) as ClipboardEvent;
    Object.defineProperty(pasteEvent, "clipboardData", {
      configurable: true,
      value: { getData: vi.fn(() => "echo native\n") },
    });
    pasteEvent.stopImmediatePropagation = vi.fn();

    act(() => {
      terminalElement.dispatchEvent(pasteEvent);
    });

    const inputMessages = workerInstances[0].messages.filter((message) => message.type === "input");
    expect(inputMessages).toHaveLength(1);
    expect(new TextDecoder().decode((inputMessages[0] as { data: Uint8Array }).data)).toBe("echo native\r");
    expect(pasteEvent.defaultPrevented).toBe(true);
    expect(pasteEvent.stopImmediatePropagation).toHaveBeenCalledTimes(1);
  });

  it("falls back to native multi-character text input when xterm misses Android IME data", async () => {
    installTerminalPaneDomMocks();

    act(() => {
      root?.render(
        <TerminalPane
          clientId="client-1"
          windowId="window-1"
        />
      );
    });

    const textarea = latestTerminal?.textarea;
    if (!(textarea instanceof HTMLTextAreaElement)) {
      throw new Error("Terminal helper textarea was not rendered");
    }

    act(() => {
      textarea.dispatchEvent(textInputEvent("12345"));
    });
    await waitForNativeInputFallback();

    const inputMessages = workerInstances[0].messages.filter((message) => message.type === "input");
    expect(inputMessages).toHaveLength(1);
    expect(new TextDecoder().decode((inputMessages[0] as { data: Uint8Array }).data)).toBe("12345");
  });

  it("does not duplicate native text input when xterm emits the same Android IME data", async () => {
    installTerminalPaneDomMocks();

    act(() => {
      root?.render(
        <TerminalPane
          clientId="client-1"
          windowId="window-1"
        />
      );
    });

    const textarea = latestTerminal?.textarea;
    if (!(textarea instanceof HTMLTextAreaElement)) {
      throw new Error("Terminal helper textarea was not rendered");
    }

    textarea.addEventListener("input", () => latestTerminal?.emitData("67890"));

    act(() => {
      textarea.dispatchEvent(textInputEvent("67890"));
    });
    await waitForNativeInputFallback();

    const inputMessages = workerInstances[0].messages.filter((message) => message.type === "input");
    expect(inputMessages).toHaveLength(1);
    expect(new TextDecoder().decode((inputMessages[0] as { data: Uint8Array }).data)).toBe("67890");
  });

  it("keeps repeated identical Android IME batches as separate terminal inputs", async () => {
    installTerminalPaneDomMocks();

    act(() => {
      root?.render(
        <TerminalPane
          clientId="client-1"
          windowId="window-1"
        />
      );
    });

    const textarea = latestTerminal?.textarea;
    if (!(textarea instanceof HTMLTextAreaElement)) {
      throw new Error("Terminal helper textarea was not rendered");
    }

    act(() => {
      textarea.dispatchEvent(textInputEvent("11"));
    });
    await waitForNativeInputFallback();
    act(() => {
      textarea.dispatchEvent(textInputEvent("11"));
    });
    await waitForNativeInputFallback();

    const inputMessages = workerInstances[0].messages.filter((message) => message.type === "input");
    expect(inputMessages).toHaveLength(2);
    expect(inputMessages.map((message) => (
      new TextDecoder().decode((message as { data: Uint8Array }).data)
    ))).toEqual(["11", "11"]);
  });

  it("does not swallow the next identical Android IME batch when xterm handles it", async () => {
    installTerminalPaneDomMocks();

    act(() => {
      root?.render(
        <TerminalPane
          clientId="client-1"
          windowId="window-1"
        />
      );
    });

    const textarea = latestTerminal?.textarea;
    if (!(textarea instanceof HTMLTextAreaElement)) {
      throw new Error("Terminal helper textarea was not rendered");
    }

    act(() => {
      textarea.dispatchEvent(textInputEvent("22"));
    });
    await waitForNativeInputFallback();

    const emitXtermInput = () => latestTerminal?.emitData("22");
    textarea.addEventListener("input", emitXtermInput, { once: true });
    act(() => {
      textarea.dispatchEvent(textInputEvent("22"));
    });
    await waitForNativeInputFallback();

    const inputMessages = workerInstances[0].messages.filter((message) => message.type === "input");
    expect(inputMessages).toHaveLength(2);
    expect(inputMessages.map((message) => (
      new TextDecoder().decode((message as { data: Uint8Array }).data)
    ))).toEqual(["22", "22"]);
  });
});
