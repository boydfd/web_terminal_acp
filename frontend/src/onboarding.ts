export const ONBOARDING_STORAGE_KEY = "web-terminal-acp:onboarding-complete:v1";

const ENABLED_ENV_VALUES = new Set(["1", "true", "yes", "on"]);

export function isOnboardingEnabled(): boolean {
  const value = import.meta.env.VITE_ENABLE_ONBOARDING;
  return typeof value === "string" && ENABLED_ENV_VALUES.has(value.trim().toLowerCase());
}

export function readOnboardingCompleted(): boolean {
  if (typeof window === "undefined") {
    return false;
  }

  try {
    return window.localStorage.getItem(ONBOARDING_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

export function writeOnboardingCompleted(completed = true): void {
  if (typeof window === "undefined") {
    return;
  }

  try {
    if (completed) {
      window.localStorage.setItem(ONBOARDING_STORAGE_KEY, "true");
      return;
    }
    window.localStorage.removeItem(ONBOARDING_STORAGE_KEY);
  } catch {
    return;
  }
}
