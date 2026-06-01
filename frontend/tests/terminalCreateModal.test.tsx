import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TerminalCreateModal } from "../src/components/TerminalCreateModal";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;
let queryClient: QueryClient | null = null;

function renderTerminalCreateModal(options: {
  onClose?: () => void;
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
        <TerminalCreateModal
          isOpen
          clientId="client-1"
          context={{ title: "New terminal", description: "local" }}
          onClose={options.onClose ?? (() => {})}
          onSubmit={() => {}}
        />
      </QueryClientProvider>
    );
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  document.body.replaceChildren();
  queryClient?.clear();
  root = null;
  container = null;
  queryClient = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("TerminalCreateModal", () => {
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
    renderTerminalCreateModal({ onClose });

    act(() => {
      vi.advanceTimersByTime(16);
    });

    expect(document.activeElement).toBe(container?.querySelector(".terminal-create-modal"));

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
});
