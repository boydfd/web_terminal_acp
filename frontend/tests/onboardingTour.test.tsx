import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OnboardingTour, type OnboardingStep } from "../src/components/OnboardingTour";
import { ONBOARDING_STORAGE_KEY } from "../src/onboarding";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

const steps: OnboardingStep[] = [
  {
    id: "bootstrap",
    title: "SSH Bootstrap",
    body: "Use Add client to install the remote client over SSH.",
    targetId: "remote-bootstrap-form",
    action: "remote-bootstrap"
  },
  {
    id: "registration",
    title: "Registration key",
    body: "Generate a one-time key and run the registration script on the target host.",
    path: ["Settings", "Client registration"],
    shortcutLabels: ["Alt+,"],
    targetId: "remote-registration-panel",
    action: "remote-registration"
  }
];

function renderTour(onStepAction = vi.fn(), enableOnboarding = true) {
  vi.stubEnv("VITE_ENABLE_ONBOARDING", enableOnboarding ? "true" : "");

  const target = document.createElement("div");
  target.dataset.onboardingId = "remote-bootstrap-form";
  target.getBoundingClientRect = () => ({
    x: 20,
    y: 30,
    top: 30,
    left: 20,
    right: 220,
    bottom: 110,
    width: 200,
    height: 80,
    toJSON: () => ({})
  });
  document.body.appendChild(target);

  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(<OnboardingTour steps={steps} onStepAction={onStepAction} />);
  });
  return onStepAction;
}

function buttonWithText(text: string): HTMLButtonElement {
  const button = Array.from(container?.querySelectorAll("button") ?? [])
    .find((candidate) => candidate.textContent === text);
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`button not found: ${text}`);
  }
  return button;
}

beforeEach(() => {
  vi.useFakeTimers();
  window.localStorage.clear();
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  document.body.replaceChildren();
  root = null;
  container = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("OnboardingTour", () => {
  it("does not render unless the environment variable explicitly enables it", () => {
    const onStepAction = renderTour(vi.fn(), false);

    expect(container?.textContent).not.toContain("SSH Bootstrap");
    expect(container?.querySelector(".onboarding-restart-button")).toBeNull();
    expect(onStepAction).not.toHaveBeenCalled();
  });

  it("starts on the first step and runs its action", () => {
    const onStepAction = renderTour();

    expect(container?.textContent).toContain("SSH Bootstrap");
    expect(onStepAction).toHaveBeenCalledWith("remote-bootstrap");
  });

  it("advances through remote client registration and completes", () => {
    const onStepAction = renderTour();

    act(() => {
      buttonWithText("下一步").click();
    });

    expect(container?.textContent).toContain("Registration key");
    expect(onStepAction).toHaveBeenLastCalledWith("remote-registration");

    act(() => {
      buttonWithText("完成").click();
    });

    expect(window.localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBe("true");
    expect(container?.textContent).not.toContain("新手引导");
    expect(container?.textContent).not.toContain("Registration key");
  });

  it("can be restarted from the settings event after being skipped", () => {
    renderTour();

    act(() => {
      buttonWithText("跳过").click();
    });
    expect(container?.textContent).not.toContain("新手引导");

    act(() => {
      window.dispatchEvent(new Event("web-terminal-acp:start-onboarding"));
    });

    expect(window.localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBeNull();
    expect(container?.textContent).toContain("SSH Bootstrap");
  });

  it("shows path and shortcut metadata for steps that need a nested route", () => {
    renderTour();

    act(() => {
      buttonWithText("下一步").click();
    });

    expect(container?.textContent).toContain("进入路径");
    expect(container?.textContent).toContain("Settings -> Client registration");
    expect(container?.textContent).toContain("快捷键");
    expect(container?.textContent).toContain("Alt+,");
  });
});
