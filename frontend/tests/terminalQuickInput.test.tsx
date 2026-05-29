import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TerminalQuickInput } from "../src/components/TerminalQuickInput";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

function renderQuickInput(
  props: Partial<Parameters<typeof TerminalQuickInput>[0]> = {}
): HTMLTextAreaElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <TerminalQuickInput
        value="hello"
        canSend
        onValueChange={() => {}}
        onSubmit={() => {}}
        {...props}
      />
    );
  });
  const textarea = container.querySelector("textarea");
  if (!(textarea instanceof HTMLTextAreaElement)) {
    throw new Error("Quick input textarea was not rendered");
  }
  return textarea;
}

function keyDown(target: HTMLTextAreaElement, init: KeyboardEventInit): void {
  act(() => {
    target.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }));
  });
}

function keyDownElement(target: Element, init: KeyboardEventInit): void {
  act(() => {
    target.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init }));
  });
}

function changeTextareaValue(target: HTMLTextAreaElement, value: string): void {
  const descriptor = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
  act(() => {
    descriptor?.set?.call(target, value);
    target.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function changeInputValue(target: HTMLInputElement, value: string): void {
  const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  act(() => {
    descriptor?.set?.call(target, value);
    target.dispatchEvent(new Event("input", { bubbles: true }));
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

describe("TerminalQuickInput", () => {
  it("submits plain Enter when submitOnEnter is enabled", () => {
    const onSubmit = vi.fn();
    const textarea = renderQuickInput({ onSubmit, submitOnEnter: true });

    keyDown(textarea, { key: "Enter" });

    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("keeps Shift+Enter available for new lines when submitOnEnter is enabled", () => {
    const onSubmit = vi.fn();
    const textarea = renderQuickInput({ onSubmit, submitOnEnter: true });

    keyDown(textarea, { key: "Enter", shiftKey: true });

    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("submits with Ctrl+Enter even when plain Enter is not a submit shortcut", () => {
    const onSubmit = vi.fn();
    const textarea = renderQuickInput({ onSubmit });

    keyDown(textarea, { key: "Enter", ctrlKey: true });

    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("keeps local typing visible while the parent draft update is delayed", () => {
    const onValueChange = vi.fn();
    const textarea = renderQuickInput({ value: "", onValueChange });

    changeTextareaValue(textarea, "pwd");

    act(() => {
      root?.render(
        <TerminalQuickInput
          value=""
          canSend
          onValueChange={onValueChange}
          onSubmit={() => {}}
        />
      );
    });

    expect(onValueChange).toHaveBeenCalledWith("pwd");
    expect(textarea.value).toBe("pwd");
  });

  it("submits the current textarea value before the controlled prop catches up", () => {
    const onSubmit = vi.fn(() => false);
    const textarea = renderQuickInput({
      value: "",
      onValueChange: () => {},
      onSubmit,
      submitOnEnter: true
    });

    changeTextareaValue(textarea, "echo android");
    keyDown(textarea, { key: "Enter" });

    expect(onSubmit).toHaveBeenCalledWith("echo android");
  });

  it("clears local typing after a successful submit even when the parent prop was already empty", () => {
    const textarea = renderQuickInput({
      value: "",
      onValueChange: () => {},
      onSubmit: vi.fn(() => true),
      submitOnEnter: true
    });

    changeTextareaValue(textarea, "date");
    keyDown(textarea, { key: "Enter" });

    expect(textarea.value).toBe("");
  });

  it("filters and submits custom quick keys", () => {
    const onCustomQuickKeySubmit = vi.fn(() => true);
    renderQuickInput({
      customQuickKeys: [
        { id: "status", label: "Git status", input: "git status{Enter}" },
        { id: "interrupt", label: "Interrupt", input: "{Ctrl-C}" }
      ],
      onCustomQuickKeySubmit
    });

    const toggle = container?.querySelector("button[aria-expanded]");
    if (!(toggle instanceof HTMLButtonElement)) {
      throw new Error("Quick key toggle was not rendered");
    }

    act(() => {
      toggle.click();
    });

    const search = container?.querySelector('input[type="search"]');
    if (!(search instanceof HTMLInputElement)) {
      throw new Error("Quick key search was not rendered");
    }

    changeInputValue(search, "interrupt");

    const quickKeyButtons = Array.from(container?.querySelectorAll(".terminal-quick-key-chip") ?? []);
    expect(quickKeyButtons).toHaveLength(1);
    expect(quickKeyButtons[0].textContent).toContain("Interrupt");

    act(() => {
      (quickKeyButtons[0] as HTMLButtonElement).click();
    });

    expect(onCustomQuickKeySubmit).toHaveBeenCalledWith({ id: "interrupt", label: "Interrupt", input: "{Ctrl-C}" });
  });

  it("keeps quick key search Enter from submitting the freeform input", () => {
    const onSubmit = vi.fn();
    renderQuickInput({
      onSubmit,
      customQuickKeys: [{ id: "status", label: "Git status", input: "git status{Enter}" }],
      onCustomQuickKeySubmit: vi.fn(() => true)
    });

    const toggle = container?.querySelector("button[aria-expanded]");
    if (!(toggle instanceof HTMLButtonElement)) {
      throw new Error("Quick key toggle was not rendered");
    }

    act(() => {
      toggle.click();
    });

    const search = container?.querySelector('input[type="search"]');
    if (!(search instanceof HTMLInputElement)) {
      throw new Error("Quick key search was not rendered");
    }

    keyDownElement(search, { key: "Enter" });

    expect(onSubmit).not.toHaveBeenCalled();
  });
});
