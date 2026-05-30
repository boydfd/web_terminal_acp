import type { Client } from "../types";

type ClientListProps = {
  clients: Client[];
  selectedClientId: string | null;
  onSelectClient: (clientId: string) => void;
  onUpdateClient: (clientId: string) => void;
  updatingClientId: string | null;
};

function formatHostname(client: Client): string {
  return client.hostname ?? "No hostname";
}

function formatVersion(client: Client): string {
  if (!client.version) {
    return "No version";
  }
  return client.version.startsWith("v") ? client.version : `v${client.version}`;
}

function formatLastUpdate(client: Client): string {
  if (!client.last_update_at) {
    return "No update time";
  }

  const updatedAt = new Date(client.last_update_at);
  if (Number.isNaN(updatedAt.getTime())) {
    return "Unknown";
  }

  return updatedAt.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    hour12: false
  });
}

export function ClientList({
  clients,
  selectedClientId,
  onSelectClient,
  onUpdateClient,
  updatingClientId
}: ClientListProps) {
  if (clients.length === 0) {
    return <p className="muted client-empty">No clients registered.</p>;
  }

  return (
    <div className="client-cards">
      {clients.map((client) => {
        const isSelected = client.id === selectedClientId;
        return (
          <div
            key={client.id}
            aria-current={isSelected ? "true" : undefined}
            className={isSelected ? "client-card selected" : "client-card"}
          >
            <button type="button" className="client-card-main" onClick={() => onSelectClient(client.id)}>
              <span className="client-card-header">
                <strong>{client.name}</strong>
                <span className={`client-status ${client.status.toLowerCase()}`}>{client.status}</span>
              </span>
              {isSelected && (
                <span className="client-details">
                  <span className="client-meta">
                    <span>{client.runtime}</span>
                    <span>{formatHostname(client)}</span>
                  </span>
                  <span className="client-version">{formatVersion(client)}</span>
                  <span className="client-update-time">{formatLastUpdate(client)}</span>
                </span>
              )}
            </button>
            {isSelected && client.runtime === "remote" && (
              <button
                type="button"
                className="client-card-action"
                data-onboarding-id="remote-client-update"
                disabled={client.status !== "ONLINE" || updatingClientId === client.id}
                onClick={() => onUpdateClient(client.id)}
              >
                {updatingClientId === client.id ? "Starting..." : "Update"}
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
