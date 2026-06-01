import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClientSwitcher } from "../src/components/ClientSwitcher";
import type { Client } from "../src/types";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let container: HTMLDivElement | null = null;

function makeClient(overrides: Partial<Client>): Client {
  return {
    id: "client-1",
    name: "Local client",
    status: "ONLINE",
    hostname: "workstation",
    install_path: null,
    version: "2.17.23",
    last_update_at: "2026-05-30T18:05:00Z",
    runtime: "local",
    last_seen_at: null,
    connected_at: null,
    created_at: "2026-05-30T18:00:00Z",
    updated_at: "2026-05-30T18:00:00Z",
    ...overrides
  };
}

function renderClientSwitcher(options: {
  selectedClientId?: string | null;
  recentClientIds?: string[];
  onSelectClient?: (clientId: string) => void;
} = {}) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <ClientSwitcher
        clients={[
          makeClient({ id: "client-1", name: "Local client" }),
          makeClient({ id: "client-2", name: "Remote alpha", runtime: "remote", hostname: "alpha" }),
          makeClient({ id: "client-3", name: "Remote beta", runtime: "remote", hostname: "beta" })
        ]}
        selectedClientId={options.selectedClientId ?? "client-1"}
        recentClientIds={options.recentClientIds ?? []}
        isOpen
        onClose={() => {}}
        onSelectClient={options.onSelectClient ?? (() => {})}
      />
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
  vi.restoreAllMocks();
});

describe("ClientSwitcher", () => {
  it("orders clients by recent use before the default list order", () => {
    renderClientSwitcher({ recentClientIds: ["client-3", "client-2"] });

    const rows = Array.from(container?.querySelectorAll(".client-switcher-row") ?? []);
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Remote beta"),
      expect.stringContaining("Remote alpha"),
      expect.stringContaining("Local client")
    ]);
  });

  it("selects the selected client with Enter", () => {
    const onSelectClient = vi.fn();
    renderClientSwitcher({ selectedClientId: "client-1", recentClientIds: ["client-3"], onSelectClient });

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "Enter"
      }));
    });

    expect(onSelectClient).toHaveBeenCalledWith("client-1");
  });

  it("moves through the visible client order with ArrowDown before selecting", async () => {
    const onSelectClient = vi.fn();
    renderClientSwitcher({ selectedClientId: "client-1", recentClientIds: ["client-3"], onSelectClient });

    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "ArrowDown"
      }));
    });
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", {
        bubbles: true,
        cancelable: true,
        key: "Enter"
      }));
    });

    expect(onSelectClient).toHaveBeenCalledWith("client-2");
  });
});
