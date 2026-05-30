import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MobileShortcutFab, type MobileShortcutDirection } from "../src/components/MobileShortcutFab";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const FAB_POSITION_STORAGE_KEY = "web-terminal-acp:mobile-shortcut-fab-position";

let root: Root | null = null;
let container: HTMLDivElement | null = null;

class TestPointerEvent extends MouseEvent {
  pointerId: number;
  pointerType: string;
  isPrimary: boolean;

  constructor(type: string, init: PointerEventInit = {}) {
    super(type, init);
    this.pointerId = init.pointerId ?? 1;
    this.pointerType = init.pointerType ?? "touch";
    this.isPrimary = init.isPrimary ?? true;
  }
}

function renderFab(
  onPress = vi.fn(),
  onDirectionInput: (direction: MobileShortcutDirection) => void = vi.fn(),
  actionCount = 1
): HTMLButtonElement {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <MobileShortcutFab
        visible
        actions={Array.from({ length: actionCount }, (_, index) => ({
          id: `action-${index}`,
          label: index === 0 ? "快速输入" : `快捷操作 ${index}`,
          onPress
        }))}
        onDirectionInput={onDirectionInput}
      />
    );
  });

  const button = document.body.querySelector(".mobile-shortcut-fab-ball");
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error("Mobile shortcut FAB button was not rendered");
  }
  return button;
}

function dispatchTouchPointer(target: Element, type: string, x: number, y: number): void {
  act(() => {
    target.dispatchEvent(new TestPointerEvent(type, {
      bubbles: true,
      cancelable: true,
      button: 0,
      buttons: type === "pointerup" ? 0 : 1,
      clientX: x,
      clientY: y,
      pointerId: 7,
      pointerType: "touch",
      isPrimary: true
    }));
  });
}

beforeEach(() => {
  window.localStorage.clear();
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
  Object.defineProperty(window, "innerHeight", { configurable: true, value: 844 });
  Object.defineProperty(window, "PointerEvent", { configurable: true, value: TestPointerEvent });
  HTMLElement.prototype.setPointerCapture = vi.fn();
  HTMLElement.prototype.releasePointerCapture = vi.fn();
  HTMLElement.prototype.hasPointerCapture = vi.fn(() => true);
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  document.body.querySelector(".mobile-shortcut-fab")?.remove();
  document.body.querySelector(".mobile-shortcut-fab-drawer-layer")?.remove();
  root = null;
  container = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("MobileShortcutFab", () => {
  it("sends direction input on quick swipe without moving or opening the drawer", () => {
    const onPress = vi.fn();
    const onDirectionInput = vi.fn();
    const button = renderFab(onPress, onDirectionInput);

    dispatchTouchPointer(button, "pointerdown", 346, 800);
    dispatchTouchPointer(button, "pointermove", 346, 732);
    dispatchTouchPointer(button, "pointerup", 346, 732);

    expect(onDirectionInput).toHaveBeenCalledWith("up");
    expect(onPress).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(FAB_POSITION_STORAGE_KEY)).toBeNull();

    act(() => {
      button.click();
    });
    expect(document.body.querySelector(".mobile-shortcut-fab-drawer-layer")).toBeNull();
  });

  it("moves and persists position only after a long press", () => {
    vi.useFakeTimers();
    const onPress = vi.fn();
    const onDirectionInput = vi.fn();
    const button = renderFab(onPress, onDirectionInput);

    dispatchTouchPointer(button, "pointerdown", 346, 800);
    act(() => {
      vi.advanceTimersByTime(430);
    });
    dispatchTouchPointer(button, "pointermove", 112, 156);
    dispatchTouchPointer(button, "pointerup", 112, 156);

    const storedAfterDrag = window.localStorage.getItem(FAB_POSITION_STORAGE_KEY);
    expect(storedAfterDrag).toBe(JSON.stringify({ x: 84, y: 128 }));
    expect(onDirectionInput).not.toHaveBeenCalled();

    act(() => {
      button.click();
    });
    expect(onPress).not.toHaveBeenCalled();
    expect(document.body.querySelector(".mobile-shortcut-fab-drawer-layer")).toBeNull();

    act(() => {
      root?.unmount();
    });
    container?.remove();
    root = null;
    container = null;

    const remountedButton = renderFab(onPress, onDirectionInput);
    const rootElement = remountedButton.closest(".mobile-shortcut-fab");
    expect(rootElement).toBeInstanceOf(HTMLDivElement);
    expect((rootElement as HTMLDivElement).style.left).toBe("84px");
    expect((rootElement as HTMLDivElement).style.top).toBe("128px");
  });

  it("opens the right drawer on tap and closes it from the left scrim", () => {
    const onPress = vi.fn();
    const button = renderFab(onPress);

    act(() => {
      button.click();
    });

    expect(document.body.querySelector(".mobile-shortcut-fab-ball")).toBeNull();
    expect(document.body.querySelector(".mobile-shortcut-fab-drawer")).not.toBeNull();

    const scrim = document.body.querySelector(".mobile-shortcut-fab-scrim");
    expect(scrim).toBeInstanceOf(HTMLButtonElement);
    act(() => {
      (scrim as HTMLButtonElement).click();
    });

    expect(document.body.querySelector(".mobile-shortcut-fab-drawer-layer")).toBeNull();
    expect(document.body.querySelector(".mobile-shortcut-fab-ball")).not.toBeNull();
  });

  it("opens the action drawer on tap after a dragged click was suppressed", () => {
    vi.useFakeTimers();
    const button = renderFab();

    dispatchTouchPointer(button, "pointerdown", 346, 800);
    act(() => {
      vi.advanceTimersByTime(430);
    });
    dispatchTouchPointer(button, "pointermove", 112, 156);
    dispatchTouchPointer(button, "pointerup", 112, 156);
    act(() => {
      button.click();
      button.click();
    });

    expect(document.body.querySelector(".mobile-shortcut-fab-drawer")).not.toBeNull();
  });

  it("keeps a long action drawer scrollable", () => {
    const button = renderFab(vi.fn(), vi.fn(), 32);

    act(() => {
      button.click();
    });

    const drawerActions = document.body.querySelector(".mobile-shortcut-fab-drawer-actions");
    expect(drawerActions).toBeInstanceOf(HTMLDivElement);
    expect(drawerActions?.children).toHaveLength(32);
    expect((drawerActions as HTMLDivElement).className).toBe("mobile-shortcut-fab-drawer-actions");
  });
});
