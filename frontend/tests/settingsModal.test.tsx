import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsModal } from "../src/components/SettingsModal";
import type { KeyboardShortcutBindings } from "../src/keyboardShortcuts";
import type { CustomQuickKey } from "../src/terminalQuickKeys";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;
let queryClient: QueryClient | null = null;

beforeEach(() => {
  vi.useFakeTimers();
});

function renderSettingsModal(options: {
  keyboardShortcutBindings?: KeyboardShortcutBindings;
  customQuickKeys?: CustomQuickKey[];
  onboardingEnabled?: boolean;
  onClose?: () => void;
  onSummaryOutputLanguageChange?: (language: "中文" | "English") => void;
  onKeyboardShortcutBindingsChange?: (bindings: KeyboardShortcutBindings) => void;
  onCustomQuickKeysChange?: (quickKeys: CustomQuickKey[]) => void;
} = {}) {
  container = document.createElement("div");
  document.body.appendChild(container);
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false }
    }
  });
  root = createRoot(container);
  act(() => {
    root?.render(
      <QueryClientProvider client={queryClient as QueryClient}>
        <SettingsModal
          isOpen
          onClose={options.onClose ?? (() => {})}
          summaryOutputLanguage="中文"
          terminalGroupingMode="project-topic"
          themeSkin="default"
          desktopNotificationsEnabled
          keyboardShortcutBindings={options.keyboardShortcutBindings ?? {}}
          customQuickKeys={options.customQuickKeys ?? []}
          selectedClientId="client-1"
          onSummaryOutputLanguageChange={options.onSummaryOutputLanguageChange ?? (() => {})}
          onTerminalGroupingModeChange={() => {}}
          onThemeSkinChange={() => {}}
          onDesktopNotificationsEnabledChange={() => {}}
          onKeyboardShortcutBindingsChange={options.onKeyboardShortcutBindingsChange ?? (() => {})}
          onCustomQuickKeysChange={options.onCustomQuickKeysChange ?? (() => {})}
          authEnabled
          onboardingEnabled={options.onboardingEnabled ?? true}
          onStartOnboarding={() => {}}
          onLogout={() => {}}
        />
      </QueryClientProvider>
    );
  });
}

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  root = null;
  container = null;
  queryClient?.clear();
  queryClient = null;
  window.localStorage.clear();
  document.body.replaceChildren();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("SettingsModal", () => {
  it("takes focus from a focused terminal textarea and closes before terminal Escape handling", () => {
    const terminalTextarea = document.createElement("textarea");
    terminalTextarea.className = "xterm-helper-textarea";
    const terminalEscapeHandler = vi.fn((event: KeyboardEvent) => {
      event.preventDefault();
      event.stopPropagation();
    });
    terminalTextarea.addEventListener("keydown", terminalEscapeHandler);
    document.body.appendChild(terminalTextarea);
    terminalTextarea.focus();

    const onClose = vi.fn();
    renderSettingsModal({ onClose });

    act(() => {
      vi.advanceTimersByTime(16);
    });

    expect(document.activeElement).toBe(container?.querySelector(".settings-modal"));

    act(() => {
      terminalTextarea.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "Escape"
      }));
    });

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(terminalEscapeHandler).not.toHaveBeenCalled();
  });

  it("closes when Escape is pressed", () => {
    const onClose = vi.fn();
    renderSettingsModal({ onClose });

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "Escape"
      }));
    });

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("opens theme controls from a nested settings page", () => {
    renderSettingsModal();

    expect(container?.querySelector(".settings-skin-preview-grid")).toBeNull();
    expect(container?.textContent).toContain("界面");

    const themeTab = Array.from(container?.querySelectorAll(".settings-tabs button") ?? [])
      .find((button) => button.textContent?.includes("界面"));
    expect(themeTab).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      (themeTab as HTMLButtonElement).click();
    });

    expect(container?.querySelector(".settings-skin-preview-grid")).not.toBeNull();
    expect(container?.textContent).toContain("当前皮肤");
  });

  it("records a new built-in shortcut binding after save", () => {
    const onKeyboardShortcutBindingsChange = vi.fn();
    renderSettingsModal({ onKeyboardShortcutBindingsChange });

    const shortcutRow = Array.from(container?.querySelectorAll(".settings-tabs button") ?? [])
      .find((row) => row.textContent?.includes("快捷键"));
    act(() => {
      (shortcutRow as HTMLButtonElement).click();
    });

    const settingsBindButton = Array.from(container?.querySelectorAll(".shortcut-binding-item") ?? [])
      .find((item) => item.textContent?.includes("设置"))
      ?.querySelector(".shortcut-recorder-button");
    expect(settingsBindButton).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      (settingsBindButton as HTMLButtonElement).click();
    });
    act(() => {
      (settingsBindButton as HTMLButtonElement).dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "s",
        ctrlKey: true
      }));
    });

    expect(onKeyboardShortcutBindingsChange).not.toHaveBeenCalled();
    expect(container?.textContent).toContain("有未保存的修改");

    const saveButton = Array.from(container?.querySelectorAll("button") ?? [])
      .find((button) => button.textContent === "保存");
    expect(saveButton).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      (saveButton as HTMLButtonElement).click();
    });

    expect(onKeyboardShortcutBindingsChange).toHaveBeenCalledWith({
      settings: { key: "s", alt: false, ctrl: true, meta: false, shift: false }
    });
  });

  it("cancels shortcut recording with Escape without closing the settings modal", () => {
    const onClose = vi.fn();
    renderSettingsModal({ onClose });

    const shortcutRow = Array.from(container?.querySelectorAll(".settings-tabs button") ?? [])
      .find((row) => row.textContent?.includes("快捷键"));
    act(() => {
      (shortcutRow as HTMLButtonElement).click();
    });

    const settingsBindButton = Array.from(container?.querySelectorAll(".shortcut-binding-item") ?? [])
      .find((item) => item.textContent?.includes("设置"))
      ?.querySelector(".shortcut-recorder-button");
    expect(settingsBindButton).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      (settingsBindButton as HTMLButtonElement).click();
    });
    expect(settingsBindButton?.textContent).toContain("按下快捷键");

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "Escape"
      }));
    });

    expect(onClose).not.toHaveBeenCalled();
    expect(settingsBindButton?.textContent).toContain("Alt+,");
  });

  it("starts onboarding from settings", () => {
    const onStartOnboarding = vi.fn();
    container = document.createElement("div");
    document.body.appendChild(container);
    queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false }
      }
    });
    root = createRoot(container);
    act(() => {
      root?.render(
        <QueryClientProvider client={queryClient as QueryClient}>
          <SettingsModal
            isOpen
            onClose={() => {}}
            summaryOutputLanguage="中文"
            terminalGroupingMode="project-topic"
            themeSkin="default"
            desktopNotificationsEnabled
            keyboardShortcutBindings={{}}
            customQuickKeys={[]}
            selectedClientId="client-1"
            onSummaryOutputLanguageChange={() => {}}
            onTerminalGroupingModeChange={() => {}}
            onThemeSkinChange={() => {}}
            onDesktopNotificationsEnabledChange={() => {}}
            onKeyboardShortcutBindingsChange={() => {}}
            onCustomQuickKeysChange={() => {}}
            authEnabled
            onboardingEnabled
            onStartOnboarding={onStartOnboarding}
            onLogout={() => {}}
          />
        </QueryClientProvider>
      );
    });

    const accountTab = Array.from(container?.querySelectorAll(".settings-tabs button") ?? [])
      .find((button) => button.textContent?.includes("账号"));
    expect(accountTab).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      (accountTab as HTMLButtonElement).click();
    });

    const onboardingRow = Array.from(container?.querySelectorAll(".settings-nav-row") ?? [])
      .find((row) => row.textContent?.includes("新手引导"));
    expect(onboardingRow).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      (onboardingRow as HTMLButtonElement).click();
    });

    expect(onStartOnboarding).toHaveBeenCalledTimes(1);
  });

  it("hides onboarding entry when onboarding is disabled", () => {
    renderSettingsModal({ onboardingEnabled: false });

    expect(container?.textContent).not.toContain("新手引导");
  });

  it("records shortcut bindings for custom quick keys after save", () => {
    const onCustomQuickKeysChange = vi.fn();
    renderSettingsModal({
      customQuickKeys: [{ id: "interrupt", label: "Interrupt", input: "{Ctrl-C}" }],
      onCustomQuickKeysChange
    });

    const shortcutRow = Array.from(container?.querySelectorAll(".settings-tabs button") ?? [])
      .find((row) => row.textContent?.includes("快捷键"));
    act(() => {
      (shortcutRow as HTMLButtonElement).click();
    });

    const quickKeyBindButton = Array.from(container?.querySelectorAll(".shortcut-binding-item") ?? [])
      .find((item) => item.textContent?.includes("快捷按键：Interrupt"))
      ?.querySelector(".shortcut-recorder-button");
    expect(quickKeyBindButton).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      (quickKeyBindButton as HTMLButtonElement).click();
    });
    act(() => {
      (quickKeyBindButton as HTMLButtonElement).dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "c",
        altKey: true
      }));
    });

    expect(onCustomQuickKeysChange).not.toHaveBeenCalled();

    const saveButton = Array.from(container?.querySelectorAll("button") ?? [])
      .find((button) => button.textContent === "保存");
    expect(saveButton).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      (saveButton as HTMLButtonElement).click();
    });

    expect(onCustomQuickKeysChange).toHaveBeenCalledWith([
      {
        id: "interrupt",
        label: "Interrupt",
        input: "{Ctrl-C}",
        shortcut: { key: "c", alt: true, ctrl: false, meta: false, shift: false }
      }
    ]);
  });

  it("confirms before closing with unsaved changes", () => {
    const onClose = vi.fn();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderSettingsModal({ onClose });

    const languageSelect = Array.from(container?.querySelectorAll("select") ?? [])
      .find((select) => select.textContent?.includes("English"));
    expect(languageSelect).toBeInstanceOf(HTMLSelectElement);

    act(() => {
      (languageSelect as HTMLSelectElement).value = "English";
      (languageSelect as HTMLSelectElement).dispatchEvent(new Event("change", { bubbles: true }));
    });

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "Escape"
      }));
    });

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();

    confirmSpy.mockReturnValue(true);
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "Escape"
      }));
    });

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
