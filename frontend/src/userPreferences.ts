export type SummaryOutputLanguage = "中文" | "English";

export type TerminalGroupingMode = "project-topic" | "topic";

const SUMMARY_LANGUAGE_KEY = "web-terminal-acp:summary-output-language";
const TERMINAL_GROUPING_KEY = "web-terminal-acp:terminal-grouping-mode";

export {
  desktopNotificationsSupported,
  readDesktopNotificationsEnabled,
  writeDesktopNotificationsEnabled
} from "./desktopNotifications";

export function readSummaryOutputLanguage(): SummaryOutputLanguage {
  if (typeof window === "undefined") {
    return "中文";
  }

  const stored = window.localStorage.getItem(SUMMARY_LANGUAGE_KEY);
  return stored === "English" ? "English" : "中文";
}

export function writeSummaryOutputLanguage(language: SummaryOutputLanguage): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(SUMMARY_LANGUAGE_KEY, language);
}

export function readTerminalGroupingMode(): TerminalGroupingMode {
  if (typeof window === "undefined") {
    return "project-topic";
  }

  const stored = window.localStorage.getItem(TERMINAL_GROUPING_KEY);
  return stored === "topic" ? "topic" : "project-topic";
}

export function writeTerminalGroupingMode(mode: TerminalGroupingMode): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(TERMINAL_GROUPING_KEY, mode);
}

export function isSettingsShortcut(event: KeyboardEvent): boolean {
  const key = event.key.toLocaleLowerCase();
  return event.altKey && !event.ctrlKey && !event.metaKey && (event.code === "Comma" || key === ",");
}
