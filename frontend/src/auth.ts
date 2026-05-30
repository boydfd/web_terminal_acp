const AUTH_TOKEN_STORAGE_KEY = "web-terminal-acp:auth-token";
const AUTH_CHANGED_EVENT = "web-terminal-auth-changed";

export function readAuthToken(): string | null {
  if (typeof window === "undefined") {
    return null;
  }
  const token = window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY);
  return token && token.trim() !== "" ? token : null;
}

export function writeAuthToken(token: string): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, token);
  window.dispatchEvent(new Event(AUTH_CHANGED_EVENT));
}

export function clearAuthToken(): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
  window.dispatchEvent(new Event(AUTH_CHANGED_EVENT));
}

export function authChangedEventName(): string {
  return AUTH_CHANGED_EVENT;
}

export function appendAuthToken(url: URL): URL {
  const token = readAuthToken();
  if (token !== null) {
    url.searchParams.set("auth_token", token);
  }
  return url;
}
