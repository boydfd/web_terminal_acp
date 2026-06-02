import { DEFAULT_THEME_SKIN, isThemeSkinId, type ThemeSkinId } from "./themeSkins";
import {
  DEFAULT_TERMINAL_TIME_RANGE,
  isTerminalTimeRange,
  type TerminalTimeRange
} from "./terminalTimeRange";

export type { ThemeSkinId } from "./themeSkins";
export type { TerminalTimeRange } from "./terminalTimeRange";

export type SummaryOutputLanguage = "中文" | "English";

export type TerminalGroupingMode = "project-topic" | "topic" | "time-topic" | "project-time-topic";

export type AgentCommandSettings = Record<string, string>;

const SUMMARY_LANGUAGE_KEY = "web-terminal-acp:summary-output-language";
const TERMINAL_GROUPING_KEY = "web-terminal-acp:terminal-grouping-mode";
const TERMINAL_TIME_RANGE_KEY = "web-terminal-acp:terminal-time-range";
const AGENT_COMMANDS_KEY = "web-terminal-acp:agent-commands";
const THEME_SKIN_KEY = "web-terminal-acp:theme-skin";
const DEFAULT_AGENT_COMMANDS: AgentCommandSettings = {
  codex: "codex",
  claude: "claude",
  cursor: "agent",
  antigravity: "agy-p"
};

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
  if (stored === "topic" || stored === "time-topic" || stored === "project-time-topic") {
    return stored;
  }

  return "project-topic";
}

export function writeTerminalGroupingMode(mode: TerminalGroupingMode): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(TERMINAL_GROUPING_KEY, mode);
}

export function readTerminalTimeRange(): TerminalTimeRange {
  if (typeof window === "undefined") {
    return DEFAULT_TERMINAL_TIME_RANGE;
  }

  const stored = window.localStorage.getItem(TERMINAL_TIME_RANGE_KEY);
  return isTerminalTimeRange(stored) ? stored : DEFAULT_TERMINAL_TIME_RANGE;
}

export function writeTerminalTimeRange(range: TerminalTimeRange): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(TERMINAL_TIME_RANGE_KEY, range);
}

export function readThemeSkin(): ThemeSkinId {
  if (typeof window === "undefined") {
    return DEFAULT_THEME_SKIN;
  }

  const stored = window.localStorage.getItem(THEME_SKIN_KEY);
  return isThemeSkinId(stored) ? stored : DEFAULT_THEME_SKIN;
}

export function writeThemeSkin(themeSkin: ThemeSkinId): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(THEME_SKIN_KEY, themeSkin);
}

export function readAgentCommandSettings(): AgentCommandSettings {
  const defaults = { ...DEFAULT_AGENT_COMMANDS };
  if (typeof window === "undefined") {
    return defaults;
  }

  try {
    const parsed = JSON.parse(window.localStorage.getItem(AGENT_COMMANDS_KEY) ?? "{}") as Partial<AgentCommandSettings>;
    const settings = { ...defaults };
    for (const [agent, command] of Object.entries(parsed)) {
      if (typeof command === "string" && command.trim()) {
        settings[agent] = command;
      }
    }
    return settings;
  } catch {
    return defaults;
  }
}

export function writeAgentCommandSettings(settings: AgentCommandSettings): void {
  if (typeof window === "undefined") {
    return;
  }

  window.localStorage.setItem(AGENT_COMMANDS_KEY, JSON.stringify(settings));
}
