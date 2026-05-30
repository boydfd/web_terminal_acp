import { normalizeKeyboardShortcut, type KeyboardShortcut } from "./keyboardShortcuts";
import { createBrowserUuid } from "./uuid";

export type CustomQuickKey = {
  id: string;
  label: string;
  input: string;
  shortcut?: KeyboardShortcut | null;
};

export type TerminalSpecialKey = {
  token: string;
  label: string;
  value: string;
  aliases?: string[];
};

export const TERMINAL_SPECIAL_KEYS: TerminalSpecialKey[] = [
  { token: "Enter", label: "Enter", value: "\r", aliases: ["Return"] },
  { token: "Escape", label: "Esc", value: "\x1b", aliases: ["Esc"] },
  { token: "Tab", label: "Tab", value: "\t" },
  { token: "Backspace", label: "Backspace", value: "\x7f" },
  { token: "Delete", label: "Delete", value: "\x1b[3~", aliases: ["Del"] },
  { token: "Ctrl-C", label: "Ctrl-C", value: "\x03" },
  { token: "Ctrl-D", label: "Ctrl-D", value: "\x04" },
  { token: "Ctrl-L", label: "Ctrl-L", value: "\x0c" },
  { token: "Ctrl-A", label: "Ctrl-A", value: "\x01" },
  { token: "Ctrl-E", label: "Ctrl-E", value: "\x05" },
  { token: "Ctrl-U", label: "Ctrl-U", value: "\x15" },
  { token: "Ctrl-W", label: "Ctrl-W", value: "\x17" },
  { token: "Ctrl-K", label: "Ctrl-K", value: "\x0b" },
  { token: "Ctrl-Z", label: "Ctrl-Z", value: "\x1a" },
  { token: "ArrowUp", label: "Up", value: "\x1b[A", aliases: ["Up"] },
  { token: "ArrowDown", label: "Down", value: "\x1b[B", aliases: ["Down"] },
  { token: "ArrowLeft", label: "Left", value: "\x1b[D", aliases: ["Left"] },
  { token: "ArrowRight", label: "Right", value: "\x1b[C", aliases: ["Right"] },
  { token: "Home", label: "Home", value: "\x1b[H" },
  { token: "End", label: "End", value: "\x1b[F" },
  { token: "PageUp", label: "PgUp", value: "\x1b[5~", aliases: ["PgUp"] },
  { token: "PageDown", label: "PgDn", value: "\x1b[6~", aliases: ["PgDn"] }
];

const LEGACY_CUSTOM_QUICK_KEYS_STORAGE_KEY = "web-terminal-acp:custom-quick-keys";
const MAX_QUICK_KEYS = 100;
const MAX_LABEL_LENGTH = 80;
const MAX_INPUT_LENGTH = 4096;
const TOKEN_PATTERN = /\{([^{}]+)\}/g;
const FUNCTION_KEY_PATTERN = /^f([1-9]|1[0-2])$/i;
const CONTROL_KEY_ALIASES: Record<string, string> = {
  "2": "\x00",
  "@": "\x00",
  "3": "\x1b",
  "[": "\x1b",
  "4": "\x1c",
  "\\": "\x1c",
  "5": "\x1d",
  "]": "\x1d",
  "6": "\x1e",
  "^": "\x1e",
  "7": "\x1f",
  "_": "\x1f",
  "8": "\x7f",
  "?": "\x7f",
  space: "\x00"
};
const FUNCTION_KEY_VALUES: Record<string, string> = {
  f1: "\x1bOP",
  f2: "\x1bOQ",
  f3: "\x1bOR",
  f4: "\x1bOS",
  f5: "\x1b[15~",
  f6: "\x1b[17~",
  f7: "\x1b[18~",
  f8: "\x1b[19~",
  f9: "\x1b[20~",
  f10: "\x1b[21~",
  f11: "\x1b[23~",
  f12: "\x1b[24~"
};

const SPECIAL_KEY_BY_TOKEN = new Map<string, TerminalSpecialKey>();
for (const key of TERMINAL_SPECIAL_KEYS) {
  SPECIAL_KEY_BY_TOKEN.set(key.token.toLocaleLowerCase(), key);
  for (const alias of key.aliases ?? []) {
    SPECIAL_KEY_BY_TOKEN.set(alias.toLocaleLowerCase(), key);
  }
}

function cleanString(value: unknown, maxLength: number): string {
  return typeof value === "string" ? value.trim().slice(0, maxLength) : "";
}

function cleanInput(value: unknown): string {
  return typeof value === "string" ? value.slice(0, MAX_INPUT_LENGTH) : "";
}

export function quickKeyToken(token: string): string {
  return `{${token}}`;
}

export function createCustomQuickKey(label = "", input = ""): CustomQuickKey {
  return {
    id: createBrowserUuid(),
    label,
    input
  };
}

export function normalizeCustomQuickKeys(value: unknown): CustomQuickKey[] {
  if (!Array.isArray(value)) {
    return [];
  }

  const quickKeys: CustomQuickKey[] = [];
  for (const [index, item] of value.entries()) {
    if (item === null || typeof item !== "object") {
      continue;
    }

    const record = item as Record<string, unknown>;
    const label = cleanString(record.label, MAX_LABEL_LENGTH);
    const input = cleanInput(record.input);
    if (label.length === 0 || input.length === 0) {
      continue;
    }

    const rawId = cleanString(record.id, 128);
    const quickKey: CustomQuickKey = {
      id: rawId.length > 0 ? rawId : `quick-key-${index}`,
      label,
      input
    };
    const shortcut = normalizeKeyboardShortcut(record.shortcut);
    if (shortcut !== null) {
      quickKey.shortcut = shortcut;
    }
    quickKeys.push(quickKey);

    if (quickKeys.length >= MAX_QUICK_KEYS) {
      break;
    }
  }

  return quickKeys;
}

export function customQuickKeyForStorage(quickKey: CustomQuickKey): CustomQuickKey {
  const normalizedQuickKey: CustomQuickKey = {
    id: quickKey.id,
    label: quickKey.label,
    input: quickKey.input
  };
  const shortcut = normalizeKeyboardShortcut(quickKey.shortcut);
  if (shortcut !== null) {
    normalizedQuickKey.shortcut = shortcut;
  }
  return normalizedQuickKey;
}

export function readLegacyCustomQuickKeys(): CustomQuickKey[] {
  if (typeof window === "undefined") {
    return [];
  }

  try {
    const rawValue = window.localStorage.getItem(LEGACY_CUSTOM_QUICK_KEYS_STORAGE_KEY);
    if (rawValue === null) {
      return [];
    }
    return normalizeCustomQuickKeys(JSON.parse(rawValue));
  } catch {
    return [];
  }
}

export function clearLegacyCustomQuickKeys(): void {
  if (typeof window === "undefined") {
    return;
  }

  try {
    window.localStorage.removeItem(LEGACY_CUSTOM_QUICK_KEYS_STORAGE_KEY);
  } catch {
    return;
  }
}

function decodeControlToken(token: string): string | null {
  const match = /^(?:ctrl|control)[+-](.+)$/i.exec(token);
  if (match === null) {
    return null;
  }

  const key = match[1].trim();
  const normalizedKey = key.toLocaleLowerCase();
  const aliasedValue = CONTROL_KEY_ALIASES[normalizedKey] ?? CONTROL_KEY_ALIASES[key];
  if (aliasedValue !== undefined) {
    return aliasedValue;
  }

  if (key.length !== 1) {
    return null;
  }

  const code = key.toLocaleUpperCase().charCodeAt(0);
  if (code >= 65 && code <= 90) {
    return String.fromCharCode(code - 64);
  }

  return null;
}

function decodeFunctionKeyToken(token: string): string | null {
  if (!FUNCTION_KEY_PATTERN.test(token)) {
    return null;
  }

  return FUNCTION_KEY_VALUES[token.toLocaleLowerCase()] ?? null;
}

export function decodeQuickKeyInput(input: string): string {
  return input.replace(TOKEN_PATTERN, (match, token: string) => {
    const normalizedToken = token.trim();
    return SPECIAL_KEY_BY_TOKEN.get(normalizedToken.toLocaleLowerCase())?.value
      ?? decodeControlToken(normalizedToken)
      ?? decodeFunctionKeyToken(normalizedToken)
      ?? match;
  });
}

export function customQuickKeySearchText(quickKey: CustomQuickKey): string {
  return `${quickKey.label} ${quickKey.input}`.toLocaleLowerCase();
}

export function filterCustomQuickKeys(quickKeys: CustomQuickKey[], query: string): CustomQuickKey[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  if (normalizedQuery.length === 0) {
    return quickKeys;
  }

  return quickKeys.filter((quickKey) => customQuickKeySearchText(quickKey).includes(normalizedQuery));
}
