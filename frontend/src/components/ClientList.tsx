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
    return "Updated unknown";
  }

  return `Updated ${updatedAt.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short"
  })}`;
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
    <section className="client-list" aria-labelledby="client-list-heading">
      <h2 id="client-list-heading">Clients</h2>
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
                <span className="client-meta">
                  <span>{client.runtime}</span>
                  <span>{formatHostname(client)}</span>
                </span>
                <span className="client-version">{formatVersion(client)}</span>
                <span className="client-update-time">{formatLastUpdate(client)}</span>
              </button>
              {client.runtime === "remote" && (
                <button
                  type="button"
                  className="client-card-action"
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
    </section>
  );
}
