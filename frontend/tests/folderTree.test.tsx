import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FolderTree } from "../src/components/FolderTree";
import { TERMINAL_TIME_RANGE_OPTIONS } from "../src/terminalTimeRange";
import type { TreeFolder } from "../src/types";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;
let queryClient: QueryClient | null = null;
let animationFrameCallbacks: Array<FrameRequestCallback | null> = [];
const originalScrollIntoView = window.HTMLElement.prototype.scrollIntoView;

const workStatus = {
  state: "RECENT_ACTIVE",
  label: "recent active",
  color: "green",
  last_activity_at: "2026-05-29T00:00:00Z",
  last_working_activity_at: "2026-05-29T00:00:00Z"
} as const;

function treeWithTitle(title: string): TreeFolder[] {
  return [{
    id: "folder-root",
    name: "Root",
    path: "/root",
    folders: [],
    windows: [{
      id: "window-1",
      title,
      status: "ACTIVE",
      title_tags: [],
      created_at: "2026-05-29T00:00:00Z",
      work_status: workStatus,
      runtime_tags: ["/repo"],
      last_agent_task_completed_at: null,
      git_worktree: null
    }]
  }];
}

function flushAnimationFrames(): void {
  const callbacks = animationFrameCallbacks;
  animationFrameCallbacks = [];
  callbacks.forEach((callback, index) => callback?.(index));
}

function renderFolderTree(props: {
  folders: TreeFolder[];
  selectedWindowId?: string | null;
  locateSelectedWindowSignal?: number;
  onTimeRangeChange?: (range: string) => void;
}): HTMLButtonElement {
  if (container === null || root === null || queryClient === null) {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    queryClient = new QueryClient({
      defaultOptions: {
        queries: {
          enabled: false,
          retry: false
        }
      }
    });
  }

  act(() => {
    root?.render(
      <QueryClientProvider client={queryClient as QueryClient}>
        <FolderTree
          clientId="client-1"
          folders={props.folders}
          groupingMode="topic"
          timeRange="7d"
          timeRangeOptions={TERMINAL_TIME_RANGE_OPTIONS}
          summaryOutputLanguage="中文"
          selectedWindowId={props.selectedWindowId ?? "window-1"}
          locateSelectedWindowSignal={props.locateSelectedWindowSignal}
          onSelectWindow={() => {}}
          onDeleteWindow={() => {}}
          onTimeRangeChange={props.onTimeRangeChange ?? (() => {})}
        />
      </QueryClientProvider>
    );
  });

  const button = container.querySelector(".tree-window");
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error("Tree window button was not rendered");
  }
  return button;
}

beforeEach(() => {
  animationFrameCallbacks = [];
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback: FrameRequestCallback) => {
    animationFrameCallbacks.push(callback);
    return animationFrameCallbacks.length;
  });
  vi.spyOn(window, "cancelAnimationFrame").mockImplementation((handle: number) => {
    animationFrameCallbacks[handle - 1] = null;
  });
});

afterEach(() => {
  act(() => {
    root?.unmount();
  });
  container?.remove();
  root = null;
  container = null;
  queryClient?.clear();
  queryClient = null;
  window.HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
  vi.restoreAllMocks();
});

describe("FolderTree", () => {
  it("locates the selected window without moving focus into the sidebar", () => {
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();
    const scrollIntoView = vi.fn();
    window.HTMLElement.prototype.scrollIntoView = scrollIntoView;

    const selectedButton = renderFolderTree({
      folders: treeWithTitle("Selected Terminal"),
      locateSelectedWindowSignal: 1
    });

    act(() => {
      flushAnimationFrames();
    });

    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(selectedButton.classList.contains("locating")).toBe(true);
    expect(document.activeElement).toBe(input);
  });

  it("does not reuse the same locate signal after the terminal tree refreshes", () => {
    const scrollIntoView = vi.fn();
    window.HTMLElement.prototype.scrollIntoView = scrollIntoView;

    renderFolderTree({
      folders: treeWithTitle("Selected Terminal"),
      locateSelectedWindowSignal: 1
    });
    act(() => {
      flushAnimationFrames();
    });

    renderFolderTree({
      folders: treeWithTitle("Selected Terminal refreshed"),
      locateSelectedWindowSignal: 1
    });
    act(() => {
      flushAnimationFrames();
    });

    expect(scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("renders the terminal time range selector with 7 days selected", () => {
    renderFolderTree({
      folders: treeWithTitle("Selected Terminal")
    });

    const select = container?.querySelector("select[aria-label='Terminal time range']");
    expect(select).toBeInstanceOf(HTMLSelectElement);
    expect((select as HTMLSelectElement).value).toBe("7d");
    expect(Array.from((select as HTMLSelectElement).options).map((option) => option.value)).toEqual([
      "1d",
      "3d",
      "5d",
      "7d",
      "14d",
      "30d",
      "all"
    ]);
  });
});
