import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AddClientModal } from "../src/components/AddClientModal";
import type { BootstrapClientInput } from "../src/types";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

function renderAddClientModal(options: {
  initialMode?: "bootstrap" | "registration";
  registrationKey?: string | null;
  onBootstrapSubmit?: (payload: BootstrapClientInput) => void;
  onGenerateRegistrationKey?: (label?: string | null) => void;
} = {}) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <AddClientModal
        isOpen
        initialMode={options.initialMode}
        bootstrapFailed={false}
        bootstrapPending={false}
        registrationKey={options.registrationKey ?? null}
        registrationKeyPending={false}
        registrationKeyError={null}
        onClose={() => {}}
        onBootstrapSubmit={options.onBootstrapSubmit ?? (() => {})}
        onGenerateRegistrationKey={options.onGenerateRegistrationKey ?? (() => {})}
      />
    );
  });
}

function setInputValue(target: HTMLInputElement | HTMLTextAreaElement, value: string): void {
  const prototype = target instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
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
  delete (window as Window & { __WEB_TERMINAL_API_BASE?: string }).__WEB_TERMINAL_API_BASE;
  root = null;
  container = null;
  vi.restoreAllMocks();
});

describe("AddClientModal", () => {
  it("opens on SSH bootstrap and submits bootstrap details", () => {
    const onBootstrapSubmit = vi.fn();
    renderAddClientModal({ onBootstrapSubmit });

    expect(container?.textContent).toContain("SSH bootstrap");
    setInputValue(container?.querySelector("#bootstrap-client-name") as HTMLInputElement, "Production host");
    setInputValue(container?.querySelector("#bootstrap-client-host") as HTMLInputElement, "example.com");
    setInputValue(container?.querySelector("#bootstrap-client-port") as HTMLInputElement, "2222");
    setInputValue(container?.querySelector("#bootstrap-client-username") as HTMLInputElement, "deploy");
    setInputValue(container?.querySelector("#bootstrap-client-private-key") as HTMLTextAreaElement, "private-key");
    setInputValue(container?.querySelector("#bootstrap-client-passphrase") as HTMLInputElement, "secret");
    setInputValue(container?.querySelector("#bootstrap-client-server-url") as HTMLInputElement, "http://control.example.com:5173");

    act(() => {
      container?.querySelector("form")?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(onBootstrapSubmit).toHaveBeenCalledWith({
      name: "Production host",
      host: "example.com",
      port: 2222,
      username: "deploy",
      private_key: "private-key",
      passphrase: "secret",
      server_url: "http://control.example.com:5173"
    });
  });

  it("switches to registration key mode and requests a one-time key", () => {
    const onGenerateRegistrationKey = vi.fn();
    (window as Window & { __WEB_TERMINAL_API_BASE?: string }).__WEB_TERMINAL_API_BASE = "http://control.example.com:5173";
    renderAddClientModal({
      registrationKey: "wtr_test_key",
      onGenerateRegistrationKey
    });

    const registrationTab = Array.from(container?.querySelectorAll("[role='tab']") ?? [])
      .find((button) => button.textContent?.includes("Registration Key"));
    expect(registrationTab).toBeInstanceOf(HTMLButtonElement);

    act(() => {
      (registrationTab as HTMLButtonElement).click();
    });

    expect(container?.querySelector("[data-onboarding-id='remote-registration-panel']")).not.toBeNull();
    expect(container?.textContent).toContain("一次性注册 Key");
    expect(container?.textContent).toContain("wtr_test_key");
    const clientNameInput = Array.from(container?.querySelectorAll("input") ?? [])
      .find((input) => input.placeholder === "例如：office-mac-mini");
    expect(clientNameInput).toBeInstanceOf(HTMLInputElement);
    setInputValue(clientNameInput as HTMLInputElement, "office-mac-mini");
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

  it("can open directly to registration key mode", () => {
    renderAddClientModal({ initialMode: "registration", registrationKey: "wtr_direct_key" });

    expect(container?.textContent).toContain("Registration key");
    expect(container?.textContent).toContain("wtr_direct_key");
    expect(container?.querySelector("[data-onboarding-id='remote-registration-panel']")).not.toBeNull();
  });
});
