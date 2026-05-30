import { Clipboard as CapacitorClipboard } from "@capacitor/clipboard";
import type { Terminal } from "@xterm/xterm";

type ClipboardShortcutAction = "copy" | "paste";
type ElectronClipboardApi = {
  readClipboardText?: () => Promise<string>;
  writeClipboardText?: (text: string) => Promise<void>;
};

function isModifierClipboardShortcut(event: KeyboardEvent, key: string): boolean {
  return event.key.toLocaleLowerCase() === key
    && (event.ctrlKey || event.metaKey)
    && !event.altKey
    && !event.shiftKey;
}

export function terminalClipboardShortcutAction(event: KeyboardEvent): ClipboardShortcutAction | null {
  if (event.type !== "keydown" || event.defaultPrevented) {
    return null;
  }

  if (isModifierClipboardShortcut(event, "c")) {
    return "copy";
  }

  if (isModifierClipboardShortcut(event, "v")) {
    return "paste";
  }

  return null;
}

export function prepareTerminalPasteText(text: string, bracketedPasteMode = false): string {
  const normalizedText = text.replace(/\r?\n/g, "\r");
  return bracketedPasteMode ? `\x1b[200~${normalizedText}\x1b[201~` : normalizedText;
}

function readElectronClipboardApi(): ElectronClipboardApi | null {
  return window.electronAPI ?? null;
}

export async function readClipboardText(): Promise<string> {
  let lastError: unknown = null;

  if (navigator.clipboard?.readText) {
    try {
      return await navigator.clipboard.readText();
    } catch (error) {
      lastError = error;
    }
  }

  const electronClipboard = readElectronClipboardApi();
  if (electronClipboard?.readClipboardText !== undefined) {
    try {
      return await electronClipboard.readClipboardText();
    } catch (error) {
      lastError = error;
    }
  }

  try {
    const result = await CapacitorClipboard.read();
    return result.value ?? "";
  } catch (error) {
    throw lastError ?? error;
  }
}

export async function writeClipboardText(text: string, allowDomFallback = false): Promise<void> {
  let lastError: unknown = null;

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (error) {
      lastError = error;
    }
  }

  const electronClipboard = readElectronClipboardApi();
  if (electronClipboard?.writeClipboardText !== undefined) {
    try {
      await electronClipboard.writeClipboardText(text);
      return;
    } catch (error) {
      lastError = error;
    }
  }

  const writeWithCapacitor = async () => {
    await CapacitorClipboard.write({ string: text });
  };

  try {
    await writeWithCapacitor();
    return;
  } catch (error) {
    lastError = error;
    if (!allowDomFallback) {
      throw lastError;
    }
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
      throw lastError ?? new Error("copy command failed");
    }
  } finally {
    textarea.remove();
  }
}

export async function copyTerminalSelection(terminal: Terminal): Promise<boolean> {
  if (!terminal.hasSelection()) {
    return false;
  }

  const selectedText = terminal.getSelection();
  if (selectedText.length === 0) {
    return false;
  }

  await writeClipboardText(selectedText, true);
  terminal.clearSelection();
  return true;
}

export async function pasteClipboardToTerminal(
  sendInput: (data: string) => void,
  bracketedPasteMode = false
): Promise<boolean> {
  const text = await readClipboardText();
  if (text.length === 0) {
    return false;
  }

  sendInput(prepareTerminalPasteText(text, bracketedPasteMode));
  return true;
}

export function pasteClipboardEventToTerminal(
  event: ClipboardEvent,
  sendInput: (data: string) => void,
  bracketedPasteMode = false
): boolean {
  const text = event.clipboardData?.getData("text/plain") ?? "";
  if (text.length === 0) {
    return false;
  }

  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation();
  sendInput(prepareTerminalPasteText(text, bracketedPasteMode));
  return true;
}
