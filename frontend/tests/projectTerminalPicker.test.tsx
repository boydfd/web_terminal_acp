import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectTerminalPicker } from "../src/components/ProjectTerminalPicker";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

function renderPicker(props: Partial<Parameters<typeof ProjectTerminalPicker>[0]> = {}) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <ProjectTerminalPicker
        isOpen
        projectPaths={["/workspace/project"]}
        projectSummaries={[]}
        onClose={() => {}}
        onCreateTerminal={() => {}}
        {...props}
      />
    );
  });
}

function clickButtonWithText(text: string): void {
  const button = Array.from(container?.querySelectorAll("button") ?? []).find(
    (candidate) => candidate.textContent === text
  );
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`button not found: ${text}`);
  }
  act(() => {
    button.click();
  });
}

function clickProject(path: string): void {
  const button = container?.querySelector(`button[title="${path}"]`);
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`project button not found: ${path}`);
  }
  act(() => {
    button.click();
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

describe("ProjectTerminalPicker", () => {
  it("creates a shell terminal by default", () => {
    const onCreateTerminal = vi.fn();

    renderPicker({ onCreateTerminal });
    clickProject("/workspace/project");

    expect(onCreateTerminal).toHaveBeenCalledWith("/workspace/project", null);
  });

  it("creates an agent terminal only after selecting an agent", () => {
    const onCreateTerminal = vi.fn();

    renderPicker({ onCreateTerminal });
    clickButtonWithText("Codex");
    clickProject("/workspace/project");

    expect(onCreateTerminal).toHaveBeenCalledWith(
      "/workspace/project",
      expect.objectContaining({ agent: "codex", command: "codex" })
    );
  });
});
