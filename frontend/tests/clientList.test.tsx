import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClientList } from "../src/components/ClientList";
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
    version: "2.14.1",
    last_update_at: "2026-05-30T18:05:00Z",
    runtime: "local",
    last_seen_at: null,
    connected_at: null,
    created_at: "2026-05-30T18:00:00Z",
    updated_at: "2026-05-30T18:00:00Z",
    ...overrides
  };
}

function renderClientList(selectedClientId: string | null): void {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);

  act(() => {
    root?.render(
      <ClientList
        clients={[
          makeClient({ id: "client-1", name: "Local client" }),
          makeClient({
            id: "client-2",
            name: "Remote client",
            runtime: "remote",
            hostname: "remote-host",
            last_update_at: "2026-05-30T05:15:00Z"
          })
        ]}
        selectedClientId={selectedClientId}
        updatingClientId={null}
        onSelectClient={() => {}}
        onUpdateClient={() => {}}
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

describe("ClientList", () => {
  it("shows only name and status for clients that are not selected", () => {
    renderClientList("client-1");

    const cards = Array.from(container?.querySelectorAll(".client-card") ?? []);
    expect(cards).toHaveLength(2);
    expect(cards[1].textContent).toContain("Remote client");
    expect(cards[1].textContent).toContain("ONLINE");
    expect(cards[1].textContent).not.toContain("remote-host");
    expect(cards[1].textContent).not.toContain("Update");
  });

  it("shows selected client details with a compact 24-hour update time", () => {
    renderClientList("client-2");

    const selectedCard = container?.querySelector(".client-card.selected");
    expect(selectedCard?.textContent).toContain("remote-host");
    expect(selectedCard?.textContent).toContain("Update");
    expect(selectedCard?.textContent).not.toContain("Updated");
    expect(selectedCard?.textContent).not.toMatch(/\b(?:AM|PM)\b/);
  });
});
