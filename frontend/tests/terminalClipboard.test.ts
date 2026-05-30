import { afterEach, describe, expect, it, vi } from "vitest";

import {
  copyTerminalSelection,
  pasteClipboardEventToTerminal,
  pasteClipboardToTerminal,
  prepareTerminalPasteText,
  readClipboardText,
  terminalClipboardShortcutAction,
  writeClipboardText,
} from "../src/terminalClipboard";

function keyEvent(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

afterEach(() => {
  vi.restoreAllMocks();
  Reflect.deleteProperty(globalThis.navigator, "clipboard");
});

describe("terminalClipboard", () => {
  it("detects Ctrl/Cmd+C and Ctrl/Cmd+V as clipboard shortcuts", () => {
    expect(terminalClipboardShortcutAction(keyEvent({ key: "c", ctrlKey: true }))).toBe("copy");
    expect(terminalClipboardShortcutAction(keyEvent({ key: "v", ctrlKey: true }))).toBe("paste");
    expect(terminalClipboardShortcutAction(keyEvent({ key: "c", metaKey: true }))).toBe("copy");
    expect(terminalClipboardShortcutAction(keyEvent({ key: "v", metaKey: true }))).toBe("paste");
    expect(terminalClipboardShortcutAction(keyEvent({ key: "c", ctrlKey: true, shiftKey: true }))).toBeNull();
    expect(terminalClipboardShortcutAction(keyEvent({ key: "x", ctrlKey: true }))).toBeNull();
  });

  it("keeps pasted line endings terminal-safe", () => {
    expect(prepareTerminalPasteText("one\ntwo\r\nthree")).toBe("one\rtwo\rthree");
  });

  it("preserves bracketed paste framing when the shell enables it", () => {
    expect(prepareTerminalPasteText("one\ntwo", true)).toBe("\x1b[200~one\rtwo\x1b[201~");
  });

  it("copies the active xterm selection and clears it", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const terminal = {
      hasSelection: () => true,
      getSelection: () => "selected text",
      clearSelection: vi.fn(),
    };

    await expect(copyTerminalSelection(terminal as never)).resolves.toBe(true);

    expect(writeText).toHaveBeenCalledWith("selected text");
    expect(terminal.clearSelection).toHaveBeenCalledTimes(1);
  });

  it("does not treat an empty selection as copied", async () => {
    const terminal = {
      hasSelection: () => false,
      getSelection: vi.fn(),
      clearSelection: vi.fn(),
    };

    await expect(copyTerminalSelection(terminal as never)).resolves.toBe(false);

    expect(terminal.getSelection).not.toHaveBeenCalled();
    expect(terminal.clearSelection).not.toHaveBeenCalled();
  });

  it("keeps Ctrl+C available for terminal interrupt when there is no selection", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const terminal = {
      hasSelection: () => false,
      getSelection: vi.fn(),
      clearSelection: vi.fn(),
    };

    await expect(copyTerminalSelection(terminal as never)).resolves.toBe(false);

    expect(writeText).not.toHaveBeenCalled();
  });

  it("pastes system clipboard text through terminal input", async () => {
    const readText = vi.fn().mockResolvedValue("echo hi\n");
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { readText },
    });
    const sendInput = vi.fn();

    await expect(pasteClipboardToTerminal(sendInput)).resolves.toBe(true);

    expect(sendInput).toHaveBeenCalledWith("echo hi\r");
  });

  it("pastes browser clipboard event text without async clipboard permissions", () => {
    const event = {
      clipboardData: { getData: vi.fn(() => "echo event\n") },
      preventDefault: vi.fn(),
      stopPropagation: vi.fn(),
      stopImmediatePropagation: vi.fn(),
    };
    const sendInput = vi.fn();

    expect(pasteClipboardEventToTerminal(event as never, sendInput)).toBe(true);

    expect(event.clipboardData.getData).toHaveBeenCalledWith("text/plain");
    expect(event.preventDefault).toHaveBeenCalledTimes(1);
    expect(event.stopPropagation).toHaveBeenCalledTimes(1);
    expect(event.stopImmediatePropagation).toHaveBeenCalledTimes(1);
    expect(sendInput).toHaveBeenCalledWith("echo event\r");
  });

  it("preserves bracketed paste framing for browser clipboard events", () => {
    const event = {
      clipboardData: { getData: vi.fn(() => "echo event\n") },
      preventDefault: vi.fn(),
      stopPropagation: vi.fn(),
      stopImmediatePropagation: vi.fn(),
    };
    const sendInput = vi.fn();

    expect(pasteClipboardEventToTerminal(event as never, sendInput, true)).toBe(true);

    expect(sendInput).toHaveBeenCalledWith("\x1b[200~echo event\r\x1b[201~");
  });

  it("uses the Electron preload clipboard bridge when browser clipboard APIs are unavailable", async () => {
    const readClipboardTextBridge = vi.fn().mockResolvedValue("electron paste");
    const writeClipboardTextBridge = vi.fn().mockResolvedValue(undefined);
    window.electronAPI = {
      isElectron: true,
      platform: "win32",
      readClipboardText: readClipboardTextBridge,
      writeClipboardText: writeClipboardTextBridge,
    };

    await expect(readClipboardText()).resolves.toBe("electron paste");
    await expect(writeClipboardText("electron copy")).resolves.toBeUndefined();

    expect(readClipboardTextBridge).toHaveBeenCalledTimes(1);
    expect(writeClipboardTextBridge).toHaveBeenCalledWith("electron copy");
    delete window.electronAPI;
  });

  it("falls back to the Electron clipboard bridge when browser clipboard calls reject", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        readText: vi.fn().mockRejectedValue(new Error("denied")),
        writeText: vi.fn().mockRejectedValue(new Error("denied")),
      },
    });
    const readClipboardTextBridge = vi.fn().mockResolvedValue("bridge paste");
    const writeClipboardTextBridge = vi.fn().mockResolvedValue(undefined);
    window.electronAPI = {
      isElectron: true,
      platform: "win32",
      readClipboardText: readClipboardTextBridge,
      writeClipboardText: writeClipboardTextBridge,
    };

    await expect(readClipboardText()).resolves.toBe("bridge paste");
    await expect(writeClipboardText("bridge copy")).resolves.toBeUndefined();

    expect(readClipboardTextBridge).toHaveBeenCalledTimes(1);
    expect(writeClipboardTextBridge).toHaveBeenCalledWith("bridge copy");
    delete window.electronAPI;
  });

  it("preserves bracketed paste framing when pasting through terminal input", async () => {
    const readText = vi.fn().mockResolvedValue("echo hi\n");
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { readText },
    });
    const sendInput = vi.fn();

    await expect(pasteClipboardToTerminal(sendInput, true)).resolves.toBe(true);

    expect(sendInput).toHaveBeenCalledWith("\x1b[200~echo hi\r\x1b[201~");
  });
});
