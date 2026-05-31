const API_BASE_STORAGE_KEY = "web-terminal-acp:api-base-url";

type ApiBaseWindow = Window & {
  __WEB_TERMINAL_API_BASE?: string;
};

function readEnvApiBase(): string {
  const value = import.meta.env.VITE_API_BASE;
  return typeof value === "string" ? value.trim() : "";
}

function readEnvClientAgentServerUrl(): string {
  const value = import.meta.env.VITE_CLIENT_AGENT_SERVER_URL;
  return typeof value === "string" ? value.trim() : "";
}

function defaultApiBase(): string {
  if (typeof window === "undefined") {
    return "http://127.0.0.1:8001";
  }
  if (window.electronAPI?.isElectron) {
    return "http://127.0.0.1:8001";
  }
  if (import.meta.env.DEV) {
    const url = new URL(window.location.href);
    url.port = "8001";
    url.pathname = "";
    url.search = "";
    url.hash = "";
    return url.origin;
  }
  return window.location.origin;
}

export function normalizeApiBaseInput(value: string): string {
  const trimmed = value.trim();
  if (trimmed === "") {
    return "";
  }

  const withProtocol = /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)
    ? trimmed
    : `http://${trimmed}`;
  const url = new URL(withProtocol);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("Backend address must use HTTP or HTTPS.");
  }
  url.username = "";
  url.password = "";
  url.search = "";
  url.hash = "";
  if (url.pathname !== "/") {
    url.pathname = url.pathname.replace(/\/+$/, "");
  }
  return url.toString().replace(/\/$/, "");
}

export function readConfiguredApiBase(): string {
  if (typeof window === "undefined") {
    return "";
  }

  const stored = window.localStorage.getItem(API_BASE_STORAGE_KEY);
  if (stored === null) {
    return "";
  }
  try {
    return normalizeApiBaseInput(stored);
  } catch {
    return "";
  }
}

export function writeConfiguredApiBase(value: string): string {
  if (typeof window === "undefined") {
    return "";
  }

  const normalized = normalizeApiBaseInput(value);
  if (normalized === "") {
    window.localStorage.removeItem(API_BASE_STORAGE_KEY);
  } else {
    window.localStorage.setItem(API_BASE_STORAGE_KEY, normalized);
  }
  return normalized;
}

export function readApiBase(): string {
  if (typeof window !== "undefined") {
    const override = (window as ApiBaseWindow).__WEB_TERMINAL_API_BASE;
    if (typeof override === "string" && override.trim() !== "") {
      return normalizeApiBaseInput(override);
    }
  }

  const configured = readConfiguredApiBase();
  if (configured !== "") {
    return configured;
  }

  const envBase = readEnvApiBase();
  if (envBase !== "") {
    try {
      return normalizeApiBaseInput(envBase);
    } catch {
      return defaultApiBase();
    }
  }

  return defaultApiBase();
}

export function readClientAgentServerUrl(): string {
  const configured = readEnvClientAgentServerUrl();
  if (configured !== "") {
    try {
      return normalizeApiBaseInput(configured);
    } catch {
      return readApiBase();
    }
  }

  return readApiBase();
}
