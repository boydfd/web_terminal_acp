import { useEffect, useState } from "react";

type LoginGateProps = {
  onSubmit: (secret: string) => Promise<void>;
  error: string | null;
  isSubmitting: boolean;
  backendAddress: string;
  backendAddressError: string | null;
  isCheckingBackend: boolean;
  onSaveBackendAddress: (value: string) => void;
};

type BackendAddressGateProps = {
  backendAddress: string;
  backendAddressError: string | null;
  connectionError?: string | null;
  isCheckingBackend: boolean;
  onSaveBackendAddress: (value: string) => void;
};

function BackendAddressFields({
  backendAddress,
  backendAddressError,
  isCheckingBackend,
  saveLabel,
  onSaveBackendAddress
}: {
  backendAddress: string;
  backendAddressError: string | null;
  isCheckingBackend: boolean;
  saveLabel: string;
  onSaveBackendAddress: (value: string) => void;
}) {
  const [draft, setDraft] = useState(backendAddress);

  useEffect(() => {
    setDraft(backendAddress);
  }, [backendAddress]);

  return (
    <>
      <label className="settings-field">
        <span>后端地址</span>
        <input
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              onSaveBackendAddress(draft);
            }
          }}
          placeholder="http://server.example.com:8001"
        />
      </label>
      <div className="settings-actions">
        <button type="button" disabled={isCheckingBackend} onClick={() => onSaveBackendAddress(draft)}>
          {saveLabel}
        </button>
        <button type="button" disabled={isCheckingBackend} onClick={() => onSaveBackendAddress("")}>
          恢复默认
        </button>
      </div>
      {backendAddressError && (
        <p className="error" role="alert">{backendAddressError}</p>
      )}
    </>
  );
}

export function BackendConnectionGate({
  backendAddress,
  backendAddressError,
  connectionError,
  isCheckingBackend,
  onSaveBackendAddress
}: BackendAddressGateProps) {
  return (
    <main className="login-shell">
      <section className="login-panel" aria-label="Backend connection">
        <h1>Web Terminal ACP</h1>
        <p className="error" role="alert">
          {connectionError ? `无法连接后端：${connectionError}` : "无法连接后端。"}
        </p>
        <BackendAddressFields
          backendAddress={backendAddress}
          backendAddressError={backendAddressError}
          isCheckingBackend={isCheckingBackend}
          saveLabel="保存并重试"
          onSaveBackendAddress={onSaveBackendAddress}
        />
      </section>
    </main>
  );
}

export function LoginGate({
  onSubmit,
  error,
  isSubmitting,
  backendAddress,
  backendAddressError,
  isCheckingBackend,
  onSaveBackendAddress
}: LoginGateProps) {
  const [secret, setSecret] = useState("");

  return (
    <main className="login-shell">
      <form
        className="login-panel"
        onSubmit={(event) => {
          event.preventDefault();
          void onSubmit(secret);
        }}
      >
        <h1>Web Terminal ACP</h1>
        <BackendAddressFields
          backendAddress={backendAddress}
          backendAddressError={backendAddressError}
          isCheckingBackend={isCheckingBackend}
          saveLabel="保存后端地址"
          onSaveBackendAddress={onSaveBackendAddress}
        />
        <label className="settings-field">
          <span>登录密钥</span>
          <input
            autoFocus
            type="password"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
          />
        </label>
        <button type="submit" disabled={isSubmitting || secret.length === 0}>
          登录
        </button>
        {error && <p className="error" role="alert">{error}</p>}
      </form>
    </main>
  );
}
