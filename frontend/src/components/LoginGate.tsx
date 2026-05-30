import { useState } from "react";

type LoginGateProps = {
  onSubmit: (secret: string) => Promise<void>;
  error: string | null;
  isSubmitting: boolean;
};

export function LoginGate({ onSubmit, error, isSubmitting }: LoginGateProps) {
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
