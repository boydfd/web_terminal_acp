import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsModal } from "../src/components/SettingsModal";
import type { KeyboardShortcutBindings } from "../src/keyboardShortcuts";
import type { CustomQuickKey } from "../src/terminalQuickKeys";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

function renderSettingsModal(options: {
  keyboardShortcutBindings?: KeyboardShortcutBindings;
  customQuickKeys?: CustomQuickKey[];
  onboardingEnabled?: boolean;
  onClose?: () => void;
  onKeyboardShortcutBindingsChange?: (bindings: KeyboardShortcutBindings) => void;
  onCustomQuickKeysChange?: (quickKeys: CustomQuickKey[]) => void;
} = {}) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <SettingsModal
        isOpen
        onClose={options.onClose ?? (() => {})}
        summaryOutputLanguage="中文"
        terminalGroupingMode="project-topic"
        themeSkin="default"
        desktopNotificationsEnabled
        keyboardShortcutBindings={options.keyboardShortcutBindings ?? {}}
        customQuickKeys={options.customQuickKeys ?? []}
        onSummaryOutputLanguageChange={() => {}}
        onTerminalGroupingModeChange={() => {}}
        onThemeSkinChange={() => {}}
        onDesktopNotificationsEnabledChange={() => {}}
        onKeyboardShortcutBindingsChange={options.onKeyboardShortcutBindingsChange ?? (() => {})}
        onCustomQuickKeysChange={options.onCustomQuickKeysChange ?? (() => {})}
        authEnabled
        registrationKey={null}
        registrationKeyPending={false}
        registrationKeyError={null}
        onGenerateRegistrationKey={() => {}}
        onboardingEnabled={options.onboardingEnabled ?? true}
        onStartOnboarding={() => {}}
        onLogout={() => {}}
      />
    );
  });
}

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  delete (window as Window & { __WEB_TERMINAL_API_BASE?: string }).__WEB_TERMINAL_API_BASE;
  root = null;
  container = null;
  window.localStorage.clear();
  vi.restoreAllMocks();
});

function changeInputValue(target: HTMLInputElement, value: string): void {
  const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  act(() => {
    descriptor?.set?.call(target, value);
    target.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("SettingsModal", () => {
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
    expect(container?.textContent).toContain("界面皮肤");
    expect(container?.textContent).toContain("Default");

    const themeRow = Array.from(container?.querySelectorAll(".settings-nav-row") ?? [])
      .find((row) => row.textContent?.includes("界面皮肤"));
    expect(themeRow).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      (themeRow as HTMLButtonElement).click();
    });

    expect(container?.querySelector(".settings-skin-preview-grid")).not.toBeNull();
    expect(container?.textContent).toContain("当前皮肤");
    expect(container?.textContent).toContain("返回");
  });

  it("opens client registration controls and requests a one-time key", () => {
    const onGenerateRegistrationKey = vi.fn();
    (window as Window & { __WEB_TERMINAL_API_BASE?: string }).__WEB_TERMINAL_API_BASE = "http://control.example.com:5173";
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root?.render(
        <SettingsModal
          isOpen
          onClose={() => {}}
          summaryOutputLanguage="中文"
          terminalGroupingMode="project-topic"
          themeSkin="default"
          desktopNotificationsEnabled
          keyboardShortcutBindings={{}}
          customQuickKeys={[]}
          onSummaryOutputLanguageChange={() => {}}
          onTerminalGroupingModeChange={() => {}}
          onThemeSkinChange={() => {}}
          onDesktopNotificationsEnabledChange={() => {}}
          onKeyboardShortcutBindingsChange={() => {}}
          onCustomQuickKeysChange={() => {}}
          authEnabled
          registrationKey="wtr_test_key"
          registrationKeyPending={false}
          registrationKeyError={null}
          onGenerateRegistrationKey={onGenerateRegistrationKey}
          onboardingEnabled
          onStartOnboarding={() => {}}
          onLogout={() => {}}
        />
      );
    });

    const registrationRow = Array.from(container?.querySelectorAll(".settings-nav-row") ?? [])
      .find((row) => row.textContent?.includes("Client 注册"));
    expect(registrationRow).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      (registrationRow as HTMLButtonElement).click();
    });

    expect(container?.textContent).toContain("一次性注册 Key");
    expect(container?.textContent).toContain("wtr_test_key");
    const clientNameInput = Array.from(container?.querySelectorAll("input") ?? [])
      .find((input) => input.placeholder === "例如：office-mac-mini");
    expect(clientNameInput).toBeInstanceOf(HTMLInputElement);
    changeInputValue(clientNameInput as HTMLInputElement, "office-mac-mini");
    const scriptTextarea = Array.from(container?.querySelectorAll("textarea") ?? [])
      .find((textarea) => textarea.value.includes("register-client-direct.sh"));
    expect(scriptTextarea?.value).toContain("http://control.example.com:5173/api/clients/register-script");
    expect(scriptTextarea?.value).toContain("WEB_TERMINAL_SERVER_URL='http://control.example.com:5173'");
    expect(scriptTextarea?.value).toContain("WEB_TERMINAL_REGISTRATION_KEY='wtr_test_key'");
    expect(scriptTextarea?.value).toContain("WEB_TERMINAL_CLIENT_NAME='office-mac-mini'");
    expect(scriptTextarea?.value).not.toContain("raw.githubusercontent.com");
    const generateButton = Array.from(container?.querySelectorAll("button") ?? [])
      .find((button) => button.textContent?.includes("生成一次性注册 Key"));
    act(() => {
      generateButton?.click();
    });
    expect(onGenerateRegistrationKey).toHaveBeenCalledWith("office-mac-mini");
  });

  it("can open directly to client registration controls", () => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root?.render(
        <SettingsModal
          isOpen
          onClose={() => {}}
          initialView="clients"
          summaryOutputLanguage="中文"
          terminalGroupingMode="project-topic"
          themeSkin="default"
          desktopNotificationsEnabled
          keyboardShortcutBindings={{}}
          customQuickKeys={[]}
          onSummaryOutputLanguageChange={() => {}}
          onTerminalGroupingModeChange={() => {}}
          onThemeSkinChange={() => {}}
          onDesktopNotificationsEnabledChange={() => {}}
          onKeyboardShortcutBindingsChange={() => {}}
          onCustomQuickKeysChange={() => {}}
          authEnabled
          registrationKey="wtr_direct_key"
          registrationKeyPending={false}
          registrationKeyError={null}
          onGenerateRegistrationKey={() => {}}
          onboardingEnabled
          onStartOnboarding={() => {}}
          onLogout={() => {}}
        />
      );
    });

    expect(container?.textContent).toContain("Client 注册");
    expect(container?.textContent).toContain("一次性注册 Key");
    expect(container?.querySelector("[data-onboarding-id='remote-registration-panel']")).not.toBeNull();
  });

  it("records a new built-in shortcut binding", () => {
    const onKeyboardShortcutBindingsChange = vi.fn();
    renderSettingsModal({ onKeyboardShortcutBindingsChange });

    const shortcutRow = Array.from(container?.querySelectorAll(".settings-nav-row") ?? [])
      .find((row) => row.textContent?.includes("快捷键绑定"));
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

    expect(onKeyboardShortcutBindingsChange).toHaveBeenCalledWith({
      settings: { key: "s", alt: false, ctrl: true, meta: false, shift: false }
    });
  });

  it("cancels shortcut recording with Escape without closing the settings modal", () => {
    const onClose = vi.fn();
    renderSettingsModal({ onClose });

    const shortcutRow = Array.from(container?.querySelectorAll(".settings-nav-row") ?? [])
      .find((row) => row.textContent?.includes("快捷键绑定"));
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
    root = createRoot(container);
    act(() => {
      root?.render(
        <SettingsModal
          isOpen
          onClose={() => {}}
          summaryOutputLanguage="中文"
          terminalGroupingMode="project-topic"
          themeSkin="default"
          desktopNotificationsEnabled
          keyboardShortcutBindings={{}}
          customQuickKeys={[]}
          onSummaryOutputLanguageChange={() => {}}
          onTerminalGroupingModeChange={() => {}}
          onThemeSkinChange={() => {}}
          onDesktopNotificationsEnabledChange={() => {}}
          onKeyboardShortcutBindingsChange={() => {}}
          onCustomQuickKeysChange={() => {}}
          authEnabled
          registrationKey={null}
          registrationKeyPending={false}
          registrationKeyError={null}
          onGenerateRegistrationKey={() => {}}
          onboardingEnabled
          onStartOnboarding={onStartOnboarding}
          onLogout={() => {}}
        />
      );
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

  it("records shortcut bindings for custom quick keys", () => {
    const onCustomQuickKeysChange = vi.fn();
    renderSettingsModal({
      customQuickKeys: [{ id: "interrupt", label: "Interrupt", input: "{Ctrl-C}" }],
      onCustomQuickKeysChange
    });

    const shortcutRow = Array.from(container?.querySelectorAll(".settings-nav-row") ?? [])
      .find((row) => row.textContent?.includes("快捷键绑定"));
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

    expect(onCustomQuickKeysChange).toHaveBeenCalledWith([
      {
        id: "interrupt",
        label: "Interrupt",
        input: "{Ctrl-C}",
        shortcut: { key: "c", alt: true, ctrl: false, meta: false, shift: false }
      }
    ]);
  });
});
