export type KeyboardShortcutId =
  | "switch-terminal"
  | "switch-terminal-global"
  | "switch-client"
  | "new-terminal"
  | "new-terminal-project"
  | "quick-input"
  | "expand-record"
  | "locate-terminal"
  | "git-diff"
  | "settings";

export type KeyboardShortcut = {
  key: string;
  alt?: boolean;
  ctrl?: boolean;
  meta?: boolean;
  shift?: boolean;
};

export type KeyboardShortcutDefinition = {
  id: KeyboardShortcutId;
  label: string;
  defaultShortcut: KeyboardShortcut;
};

export type KeyboardShortcutBindings = Partial<Record<KeyboardShortcutId, KeyboardShortcut | null>>;

export const KEYBOARD_SHORTCUT_DEFINITIONS: KeyboardShortcutDefinition[] = [
  { id: "switch-terminal", label: "切换终端", defaultShortcut: { key: "w", alt: true } },
  { id: "switch-terminal-global", label: "跨 Client 切换终端", defaultShortcut: { key: "w", alt: true, shift: true } },
  { id: "switch-client", label: "切换 Client", defaultShortcut: { key: "c", alt: true, shift: true } },
  { id: "new-terminal", label: "新建终端", defaultShortcut: { key: "n", alt: true } },
  { id: "new-terminal-project", label: "按项目新建", defaultShortcut: { key: "n", alt: true, shift: true } },
  { id: "quick-input", label: "快速输入", defaultShortcut: { key: "i", alt: true } },
  { id: "expand-record", label: "展开 Agent 记录", defaultShortcut: { key: "r", alt: true } },
  { id: "locate-terminal", label: "定位当前终端", defaultShortcut: { key: "l", alt: true } },
  { id: "git-diff", label: "Git diff", defaultShortcut: { key: "g", alt: true } },
  { id: "settings", label: "设置", defaultShortcut: { key: ",", alt: true } }
];

const KEYBOARD_SHORTCUT_BINDINGS_KEY = "web-terminal-acp:keyboard-shortcut-bindings";

const NAMED_KEYS: Record<string, string> = {
  " ": "Space",
  spacebar: "Space",
  esc: "Escape",
  escape: "Escape",
  return: "Enter",
  enter: "Enter",
  arrowup: "ArrowUp",
  up: "ArrowUp",
  arrowdown: "ArrowDown",
  down: "ArrowDown",
  arrowleft: "ArrowLeft",
  left: "ArrowLeft",
  arrowright: "ArrowRight",
  right: "ArrowRight",
  del: "Delete",
  delete: "Delete",
  backspace: "Backspace",
  tab: "Tab",
  comma: ",",
  period: ".",
  slash: "/",
  backslash: "\\"
};

const KEY_LABELS: Record<string, string> = {
  " ": "Space",
  ArrowUp: "Up",
  ArrowDown: "Down",
  ArrowLeft: "Left",
  ArrowRight: "Right",
  Escape: "Esc"
};

const DEFINITION_BY_ID = new Map(KEYBOARD_SHORTCUT_DEFINITIONS.map((definition) => [definition.id, definition]));
const LETTER_KEY_PATTERN = /^[a-z]$/;

function shortcutKey(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length === 0) {
    return "";
  }

  const named = NAMED_KEYS[trimmed.toLocaleLowerCase()];
  if (named !== undefined) {
    return named;
  }

  if (/^f([1-9]|1[0-2])$/i.test(trimmed)) {
    return trimmed.toLocaleUpperCase();
  }

  return trimmed.length === 1 ? trimmed.toLocaleLowerCase() : trimmed;
}

function isModifierOnlyKey(key: string): boolean {
  return key === "Alt" || key === "Control" || key === "Meta" || key === "Shift";
}

export function normalizeKeyboardShortcut(value: unknown): KeyboardShortcut | null {
  if (value === null) {
    return null;
  }
  if (typeof value !== "object") {
    return null;
  }

  const record = value as Partial<KeyboardShortcut>;
  if (typeof record.key !== "string") {
    return null;
  }

  const key = shortcutKey(record.key);
  if (key.length === 0 || isModifierOnlyKey(key)) {
    return null;
  }

  return {
    key,
    alt: record.alt === true,
    ctrl: record.ctrl === true,
    meta: record.meta === true,
    shift: record.shift === true
  };
}

function defaultShortcutFor(id: KeyboardShortcutId): KeyboardShortcut {
  return DEFINITION_BY_ID.get(id)?.defaultShortcut ?? { key: "" };
}

export function effectiveKeyboardShortcut(
  id: KeyboardShortcutId,
  bindings: KeyboardShortcutBindings
): KeyboardShortcut | null {
  if (Object.prototype.hasOwnProperty.call(bindings, id)) {
    return bindings[id] ?? null;
  }

  return defaultShortcutFor(id);
}

export function readKeyboardShortcutBindings(): KeyboardShortcutBindings {
  if (typeof window === "undefined") {
    return {};
  }

  try {
    const parsed = JSON.parse(window.localStorage.getItem(KEYBOARD_SHORTCUT_BINDINGS_KEY) ?? "{}") as Record<string, unknown>;
    const bindings: KeyboardShortcutBindings = {};
    for (const definition of KEYBOARD_SHORTCUT_DEFINITIONS) {
      if (!Object.prototype.hasOwnProperty.call(parsed, definition.id)) {
        continue;
      }
      bindings[definition.id] = normalizeKeyboardShortcut(parsed[definition.id]);
    }
    return bindings;
  } catch {
    return {};
  }
}

export function writeKeyboardShortcutBindings(bindings: KeyboardShortcutBindings): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(KEYBOARD_SHORTCUT_BINDINGS_KEY, JSON.stringify(bindings));
}

export function resetKeyboardShortcutBindings(): KeyboardShortcutBindings {
  writeKeyboardShortcutBindings({});
  return {};
}

export function shortcutFromKeyboardEvent(event: KeyboardEvent): KeyboardShortcut | null {
  const key = keyboardEventCodeKey(event) ?? shortcutKey(event.key);
  if (key.length === 0 || isModifierOnlyKey(key)) {
    return null;
  }

  return {
    key,
    alt: event.altKey,
    ctrl: event.ctrlKey,
    meta: event.metaKey,
    shift: event.shiftKey
  };
}

export function keyboardShortcutMatches(event: KeyboardEvent, shortcut: KeyboardShortcut | null): boolean {
  if (shortcut === null || event.defaultPrevented) {
    return false;
  }

  const eventKey = shortcutKey(event.key);
  const eventCodeKey = keyboardEventCodeKey(event);
  return (eventKey === shortcut.key || eventCodeKey === shortcut.key)
    && event.altKey === (shortcut.alt === true)
    && event.ctrlKey === (shortcut.ctrl === true)
    && event.metaKey === (shortcut.meta === true)
    && event.shiftKey === (shortcut.shift === true);
}

function keyboardEventCodeKey(event: KeyboardEvent): string | null {
  if (event.code.startsWith("Key")) {
    const letter = event.code.slice("Key".length).toLocaleLowerCase();
    return LETTER_KEY_PATTERN.test(letter) ? letter : null;
  }

  if (event.code === "Comma") {
    return ",";
  }

  if (event.code === "Period") {
    return ".";
  }

  if (event.code === "Slash") {
    return "/";
  }

  if (event.code === "Backslash") {
    return "\\";
  }

  return null;
}

export function keyboardShortcutLabel(shortcut: KeyboardShortcut | null): string {
  if (shortcut === null) {
    return "未绑定";
  }

  const parts: string[] = [];
  if (shortcut.ctrl === true) {
    parts.push("Ctrl");
  }
  if (shortcut.meta === true) {
    parts.push("Cmd");
  }
  if (shortcut.alt === true) {
    parts.push("Alt");
  }
  if (shortcut.shift === true) {
    parts.push("Shift");
  }

  parts.push(KEY_LABELS[shortcut.key] ?? shortcut.key.toLocaleUpperCase());
  return parts.join("+");
}

export function keyboardShortcutsEqual(first: KeyboardShortcut | null, second: KeyboardShortcut | null): boolean {
  if (first === null || second === null) {
    return first === second;
  }

  return first.key === second.key
    && (first.alt === true) === (second.alt === true)
    && (first.ctrl === true) === (second.ctrl === true)
    && (first.meta === true) === (second.meta === true)
    && (first.shift === true) === (second.shift === true);
}

export function shortcutForCapture(event: KeyboardEvent): KeyboardShortcut | null {
  if (event.key === "Escape") {
    return null;
  }

  return shortcutFromKeyboardEvent(event);
}
