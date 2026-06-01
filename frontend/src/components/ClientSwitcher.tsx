import { useEffect, useMemo, useRef, useState } from "react";

import type { Client } from "../types";

type ClientSwitcherProps = {
  clients: Client[];
  selectedClientId: string | null;
  recentClientIds: string[];
  isOpen: boolean;
  onClose: () => void;
  onSelectClient: (clientId: string) => void;
};

function clientStatusLabel(client: Client): string {
  if (client.runtime === "local") {
    return "local";
  }
  return client.status.toLocaleLowerCase();
}

function orderClientsByRecentUse(clients: Client[], recentClientIds: string[]): Client[] {
  const byId = new Map(clients.map((client) => [client.id, client]));
  const ordered: Client[] = [];
  const seen = new Set<string>();

  for (const clientId of recentClientIds) {
    const client = byId.get(clientId);
    if (client === undefined || seen.has(client.id)) {
      continue;
    }
    ordered.push(client);
    seen.add(client.id);
  }

  for (const client of clients) {
    if (!seen.has(client.id)) {
      ordered.push(client);
    }
  }

  return ordered;
}

export function ClientSwitcher({
  clients,
  selectedClientId,
  recentClientIds,
  isOpen,
  onClose,
  onSelectClient
}: ClientSwitcherProps) {
  const [query, setQuery] = useState("");
  const [activeClientId, setActiveClientId] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const orderedClients = useMemo(
    () => orderClientsByRecentUse(clients, recentClientIds),
    [clients, recentClientIds]
  );
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleClients = useMemo(() => {
    if (normalizedQuery.length === 0) {
      return orderedClients;
    }
    return orderedClients.filter((client) => {
      const fields = [
        client.name,
        client.hostname ?? "",
        client.runtime,
        client.status
      ];
      return fields.some((field) => field.toLocaleLowerCase().includes(normalizedQuery));
    });
  }, [normalizedQuery, orderedClients]);
  const visibleClientIds = useMemo(
    () => visibleClients.map((client) => client.id),
    [visibleClients]
  );

  useEffect(() => {
    if (!isOpen) {
      setQuery("");
      setActiveClientId(null);
      return;
    }

    requestAnimationFrame(() => inputRef.current?.focus());
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    setActiveClientId((currentClientId) => {
      if (currentClientId !== null && visibleClientIds.includes(currentClientId)) {
        return currentClientId;
      }
      if (selectedClientId !== null && visibleClientIds.includes(selectedClientId)) {
        return selectedClientId;
      }
      return visibleClientIds[0] ?? null;
    });
  }, [isOpen, selectedClientId, visibleClientIds]);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }

      if (visibleClientIds.length === 0) {
        return;
      }

      const activeIndex = activeClientId === null ? -1 : visibleClientIds.indexOf(activeClientId);
      if (event.key === "ArrowDown") {
        event.preventDefault();
        const nextIndex = activeIndex < 0 ? 0 : (activeIndex + 1) % visibleClientIds.length;
        setActiveClientId(visibleClientIds[nextIndex]);
        return;
      }

      if (event.key === "ArrowUp") {
        event.preventDefault();
        const nextIndex = activeIndex < 0
          ? visibleClientIds.length - 1
          : (activeIndex - 1 + visibleClientIds.length) % visibleClientIds.length;
        setActiveClientId(visibleClientIds[nextIndex]);
        return;
      }

      if (event.key === "Enter" && activeClientId !== null) {
        event.preventDefault();
        onSelectClient(activeClientId);
        onClose();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [activeClientId, isOpen, onClose, onSelectClient, visibleClientIds]);

  if (!isOpen) {
    return null;
  }

  return (
    <div
      className="client-switcher-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div aria-modal="true" className="client-switcher" role="dialog">
        <div className="client-switcher-header">
          <div>
            <h2>Switch client</h2>
            <p className="muted">最近使用的 Client 优先</p>
          </div>
          <button type="button" onClick={onClose}>
            Close
          </button>
        </div>

        <input
          ref={inputRef}
          aria-label="Search clients"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search clients..."
        />

        {visibleClients.length === 0 && (
          <p className="client-switcher-empty">No matching clients.</p>
        )}
        {visibleClients.length > 0 && (
          <ul className="client-switcher-results" role="listbox" aria-label="Clients">
            {visibleClients.map((client) => {
              const isActive = client.id === activeClientId;
              const isSelected = client.id === selectedClientId;
              return (
                <li key={client.id}>
                  <button
                    type="button"
                    aria-selected={isActive}
                    aria-current={isSelected ? "true" : undefined}
                    className={isActive ? "client-switcher-row active" : "client-switcher-row"}
                    onClick={() => {
                      onSelectClient(client.id);
                      onClose();
                    }}
                    role="option"
                  >
                    <span className="client-switcher-name">{client.name}</span>
                    <span className={`client-status ${client.status.toLowerCase()}`}>{client.status}</span>
                    <span className="client-switcher-meta">{clientStatusLabel(client)}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
