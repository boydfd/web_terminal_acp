import { useState } from "react";

import type { BootstrapClientInput } from "../types";

type BootstrapClientFormProps = {
  isSubmitting: boolean;
  onCancel: () => void;
  onSubmit: (payload: BootstrapClientInput) => void;
};

function defaultServerUrl(): string {
  const configured = import.meta.env.VITE_CLIENT_AGENT_SERVER_URL;
  if (typeof configured === "string" && configured.trim() !== "") {
    return configured.trim();
  }
  const url = new URL(window.location.href);
  if (url.port === "5173") {
    url.port = "8001";
    return url.origin;
  }
  return window.location.origin;
}

export function BootstrapClientForm({ isSubmitting, onCancel, onSubmit }: BootstrapClientFormProps) {
  const [name, setName] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("22");
  const [username, setUsername] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [serverUrl, setServerUrl] = useState(defaultServerUrl);

  return (
    <form
      className="bootstrap-form"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit({
          name: name.trim(),
          host: host.trim(),
          port: Number(port),
          username: username.trim(),
          private_key: privateKey,
          passphrase: passphrase === "" ? null : passphrase,
          server_url: serverUrl.trim()
        });
      }}
    >
      <h2>Add client</h2>
      <label htmlFor="bootstrap-client-name">Name</label>
      <input
        id="bootstrap-client-name"
        required
        value={name}
        onChange={(event) => setName(event.target.value)}
        placeholder="Production host"
      />

      <label htmlFor="bootstrap-client-host">Host</label>
      <input
        id="bootstrap-client-host"
        required
        value={host}
        onChange={(event) => setHost(event.target.value)}
        placeholder="example.com"
      />

      <label htmlFor="bootstrap-client-port">Port</label>
      <input
        id="bootstrap-client-port"
        required
        type="number"
        min="1"
        max="65535"
        value={port}
        onChange={(event) => setPort(event.target.value)}
      />

      <label htmlFor="bootstrap-client-username">Username</label>
      <input
        id="bootstrap-client-username"
        required
        value={username}
        onChange={(event) => setUsername(event.target.value)}
        placeholder="deploy"
      />

      <label htmlFor="bootstrap-client-private-key">Private key</label>
      <textarea
        id="bootstrap-client-private-key"
        required
        rows={8}
        value={privateKey}
        onChange={(event) => setPrivateKey(event.target.value)}
        placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
      />

      <label htmlFor="bootstrap-client-passphrase">Passphrase</label>
      <input
        id="bootstrap-client-passphrase"
        type="password"
        value={passphrase}
        onChange={(event) => setPassphrase(event.target.value)}
        placeholder="Optional"
      />

      <label htmlFor="bootstrap-client-server-url">Server URL</label>
      <input
        id="bootstrap-client-server-url"
        required
        value={serverUrl}
        onChange={(event) => setServerUrl(event.target.value)}
      />

      <div className="bootstrap-form-actions">
        <button type="submit" disabled={isSubmitting}>
          {isSubmitting ? "Bootstrapping..." : "Bootstrap client"}
        </button>
        <button type="button" onClick={onCancel} disabled={isSubmitting}>
          Cancel
        </button>
      </div>
    </form>
  );
}
