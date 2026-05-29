import { afterEach, describe, expect, it } from "vitest";

import {
  decodeQuickKeyInput,
  filterCustomQuickKeys,
  normalizeCustomQuickKeys,
  readCustomQuickKeys,
  writeCustomQuickKeys
} from "../src/terminalQuickKeys";

afterEach(() => {
  window.localStorage.clear();
});

describe("terminalQuickKeys", () => {
  it("decodes special key tokens into terminal input bytes", () => {
    expect(decodeQuickKeyInput("git status{Enter}{Ctrl-C}{ArrowUp}")).toBe("git status\r\x03\x1b[A");
  });

  it("decodes custom Ctrl key tokens dynamically", () => {
    expect(decodeQuickKeyInput("{Ctrl-F}{control+x}{Ctrl-]}{Ctrl-?}{Ctrl-Space}")).toBe("\x06\x18\x1d\x7f\x00");
  });

  it("decodes function key tokens dynamically", () => {
    expect(decodeQuickKeyInput("{F1}{f12}")).toBe("\x1bOP\x1b[24~");
  });

  it("keeps unknown tokens literal", () => {
    expect(decodeQuickKeyInput("{Unknown}")).toBe("{Unknown}");
  });

  it("normalizes stored quick keys", () => {
    expect(normalizeCustomQuickKeys([
      { id: "one", label: "  One  ", input: "{Enter}" },
      { id: "empty-input", label: "Empty", input: "" },
      { id: "empty-label", label: "", input: "ls" },
      null
    ])).toEqual([{ id: "one", label: "One", input: "{Enter}" }]);
  });

  it("persists quick keys in local storage", () => {
    writeCustomQuickKeys([{ id: "one", label: "One", input: "pwd{Enter}" }]);

    expect(readCustomQuickKeys()).toEqual([{ id: "one", label: "One", input: "pwd{Enter}" }]);
  });

  it("filters quick keys by label and input", () => {
    const quickKeys = [
      { id: "status", label: "Git status", input: "git status{Enter}" },
      { id: "interrupt", label: "Interrupt", input: "{Ctrl-C}" }
    ];

    expect(filterCustomQuickKeys(quickKeys, "ctrl-c")).toEqual([quickKeys[1]]);
  });
});
