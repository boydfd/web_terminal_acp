import { afterEach, describe, expect, it } from "vitest";

import {
  effectiveKeyboardShortcut,
  keyboardShortcutLabel,
  keyboardShortcutMatches,
  readKeyboardShortcutBindings,
  shortcutFromKeyboardEvent,
  writeKeyboardShortcutBindings
} from "../src/keyboardShortcuts";

afterEach(() => {
  window.localStorage.clear();
});

describe("keyboardShortcuts", () => {
  it("uses defaults until a binding is overridden", () => {
    expect(keyboardShortcutLabel(effectiveKeyboardShortcut("settings", {}))).toBe("Alt+,");

    writeKeyboardShortcutBindings({ settings: { key: "s", ctrl: true } });

    expect(readKeyboardShortcutBindings()).toEqual({
      settings: { key: "s", alt: false, ctrl: true, meta: false, shift: false }
    });
  });

  it("matches keyboard events against configured modifiers", () => {
    const shortcut = { key: "k", alt: true, shift: true };

    expect(keyboardShortcutMatches(new KeyboardEvent("keydown", {
      key: "K",
      altKey: true,
      shiftKey: true
    }), shortcut)).toBe(true);
    expect(keyboardShortcutMatches(new KeyboardEvent("keydown", {
      key: "K",
      altKey: true
    }), shortcut)).toBe(false);
  });

  it("matches default Alt shortcuts by physical code when Alt changes key output", () => {
    expect(keyboardShortcutMatches(new KeyboardEvent("keydown", {
      key: "∑",
      code: "KeyW",
      altKey: true
    }), effectiveKeyboardShortcut("switch-terminal", {}))).toBe(true);
    expect(keyboardShortcutMatches(new KeyboardEvent("keydown", {
      key: "≠",
      code: "Comma",
      altKey: true
    }), effectiveKeyboardShortcut("settings", {}))).toBe(true);
  });

  it("records physical keys from code when Alt changes key output", () => {
    expect(shortcutFromKeyboardEvent(new KeyboardEvent("keydown", {
      key: "∑",
      code: "KeyW",
      altKey: true
    }))).toEqual({ key: "w", alt: true, ctrl: false, meta: false, shift: false });
  });
});
