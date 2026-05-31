import { useState } from "react";

import { readApiBase, readClientAgentServerUrl } from "../apiBase";

type ClientRegistrationKeyFormProps = {
  registrationKey: string | null;
  registrationKeyPending: boolean;
  registrationKeyError: string | null;
  onGenerateRegistrationKey: (label?: string | null) => void;
};

function apiPath(path: string): string {
  const base = new URL(readApiBase());
  if (!base.pathname.endsWith("/")) {
    base.pathname = `${base.pathname}/`;
  }
  return new URL(path.replace(/^\/+/, ""), base).toString();
}

function shellSingleQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

export function ClientRegistrationKeyForm({
  registrationKey,
  registrationKeyPending,
  registrationKeyError,
  onGenerateRegistrationKey
}: ClientRegistrationKeyFormProps) {
  const [registrationClientName, setRegistrationClientName] = useState("");
  const effectiveRegistrationClientName = registrationClientName.trim() || "<唯一 client 名称>";
  const registrationScript = [
    `curl -fsSL ${shellSingleQuote(apiPath("/api/clients/register-script"))} -o register-client-direct.sh`,
    "chmod +x register-client-direct.sh",
    `WEB_TERMINAL_SERVER_URL=${shellSingleQuote(readClientAgentServerUrl())} \\`,
    `WEB_TERMINAL_REGISTRATION_KEY=${shellSingleQuote(registrationKey ?? "<先生成 key>")} \\`,
    `WEB_TERMINAL_CLIENT_NAME=${shellSingleQuote(effectiveRegistrationClientName)} \\`,
    "./register-client-direct.sh"
  ].join("\n");

  return (
    <section className="client-registration-key-form" data-onboarding-id="remote-registration-panel">
      <h3>Registration key</h3>
      <p className="muted">
        一次性注册 Key 适合在目标机器上主动运行脚本接入 remote client；不用从本机 SSH 登录目标机器。
      </p>
      <label className="settings-field">
        <span>Client 唯一名称</span>
        <input
          value={registrationClientName}
          onChange={(event) => setRegistrationClientName(event.target.value)}
          placeholder="例如：office-mac-mini"
        />
      </label>
      <button
        type="button"
        disabled={registrationKeyPending || registrationClientName.trim().length === 0}
        onClick={() => onGenerateRegistrationKey(registrationClientName.trim() || null)}
      >
        {registrationKeyPending ? "生成中..." : "生成一次性注册 Key"}
      </button>
      {registrationKeyError && (
        <p className="error settings-error" role="alert">
          {registrationKeyError}
        </p>
      )}
      {registrationKey && (
        <label className="settings-field">
          <span>一次性注册 Key</span>
          <textarea readOnly rows={3} value={registrationKey} />
        </label>
      )}
      <label className="settings-field">
        <span>注册脚本</span>
        <textarea readOnly rows={7} value={registrationScript} />
      </label>
    </section>
  );
}
