import { readAgentCommandSettings, type AgentCommandSettings } from "./userPreferences";
import type {
  AgentConfig,
  AgentConfigSelection,
  AgentLaunchConfig,
  AgentConfigSelectionSection,
  AgentLaunchKind
} from "./types";

export type AgentLaunchMode = "shell" | AgentLaunchKind;

export const AGENT_LAUNCH_LABELS: Record<AgentLaunchKind | "shell", string> = {
  shell: "No Agent",
  codex: "Codex",
  claude: "Claude Code",
  cursor: "Cursor"
};

export const AGENT_LAUNCH_OPTIONS: Array<{ id: AgentLaunchMode; label: string }> = [
  { id: "shell", label: AGENT_LAUNCH_LABELS.shell },
  { id: "codex", label: AGENT_LAUNCH_LABELS.codex },
  { id: "claude", label: AGENT_LAUNCH_LABELS.claude },
  { id: "cursor", label: AGENT_LAUNCH_LABELS.cursor }
];

export const PROJECT_AGENT_OPTIONS: Array<{ id: AgentLaunchKind; label: string }> = [
  { id: "codex", label: AGENT_LAUNCH_LABELS.codex },
  { id: "claude", label: AGENT_LAUNCH_LABELS.claude },
  { id: "cursor", label: AGENT_LAUNCH_LABELS.cursor }
];

export function isAgentLaunchKind(value: AgentLaunchMode): value is AgentLaunchKind {
  return value !== "shell";
}

export function readDefaultAgentCommands(): AgentCommandSettings {
  return readAgentCommandSettings();
}

export function agentLaunchForKind(agent: AgentLaunchKind): AgentLaunchConfig {
  const command = readDefaultAgentCommands()[agent];
  return {
    agent,
    command: command.trim() || agent,
    config: null
  };
}

export function configToSelection(config: AgentConfig): AgentConfigSelection {
  return {
    agent: config.agent,
    sections: config.sections.map((section) => ({
      id: section.id,
      items: section.items.map((item) => ({
        id: item.id,
        enabled: item.enabled
      }))
    }))
  };
}

export function selectionItemCount(selection: AgentConfigSelection | null): number {
  return selection?.sections.reduce((count, section) => count + section.items.length, 0) ?? 0;
}

export function selectedEnabledCount(selection: AgentConfigSelection | null): number {
  return selection?.sections.reduce(
    (count, section) => count + section.items.filter((item) => item.enabled).length,
    0
  ) ?? 0;
}

export function updateSelectionItem(
  selection: AgentConfigSelection,
  sectionId: AgentConfigSelectionSection["id"],
  itemId: string,
  enabled: boolean
): AgentConfigSelection {
  return {
    ...selection,
    sections: selection.sections.map((section) => {
      if (section.id !== sectionId) {
        return section;
      }
      return {
        ...section,
        items: section.items.map((item) => (item.id === itemId ? { ...item, enabled } : item))
      };
    })
  };
}
